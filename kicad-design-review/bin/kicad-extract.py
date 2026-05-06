#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["sexpdata>=1.0.2"]
# ///
"""
kicad-extract: produce structured JSON facts from a KiCad project.

Inputs:
  --source <local-path>                  Local KiCad project directory
  --source <owner/repo[@ref][:path]>     GitHub-hosted KiCad project (uses gh CLI)
  --source <https://github.com/...>      GitHub URL (parsed to owner/repo[@ref][:path])

Outputs (stdout): structured JSON with source metadata, file inventory, BOM,
netlist (from IPC-D-356A), positions, schematic facts, cross-referenced facts
with confidence flags, and doc-quality observations.

Cache directory (default: ./.kicad-review/): keyed by resolved-SHA + path-hash;
re-runs against the same source are served from cache without re-fetching.

This is the deterministic core of the kicad-design-review skill. The skill
itself synthesizes the human-readable report from this JSON.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from base64 import b64decode
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import sexpdata

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

EXTRACTOR_VERSION = "0.2.0"

# Bumped whenever the JSON output shape changes in a way consumers can detect.
# Keep additive changes within the same MINOR; breaking changes bump MAJOR.
# v1.0 = baseline (no version field); v0.2.0 marks the first version with this
# field present, plus inventory.kicad_schematic_versions and instance fields.
OUTPUT_SCHEMA_VERSION = "0.2.0"

# Set of KiCad schema versions we've validated parsing against. Files with
# (version YYYYMMDD) outside this set still parse (we degrade gracefully) but
# emit a doc-quality finding so consumers know parsing fidelity is unverified.
# 20250114 = KiCad 9.x output; the G6 boards developed against v1 are this version.
KICAD_SCHEMA_VERSIONS_TESTED = ("20250114",)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Source:
    kind: str               # "local" or "github"
    path: str               # local path OR path-within-repo for github
    owner: str | None = None
    repo: str | None = None
    ref: str | None = None  # user-supplied ref (branch / tag / SHA)
    resolved_sha: str | None = None  # immutable commit SHA (after resolution)


@dataclass
class FileEntry:
    rel_path: str           # path relative to the project root
    size: int | None = None
    sha: str | None = None  # blob sha for github sources


@dataclass
class BOMEntry:
    refdes: str
    part: str | None = None
    description: str | None = None
    manufacturer: str | None = None
    manufacturer_pn: str | None = None
    lcsc: str | None = None
    quantity: int | None = None
    package: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class NetlistEntry:
    """One refdes-pin → net mapping from the IPC-D-356A netlist."""
    net: str
    refdes: str
    pin: str
    via: bool = False


@dataclass
class Position:
    refdes: str
    x: float
    y: float
    rotation: float
    side: str | None = None  # "top" or "bottom"
    package: str | None = None


@dataclass
class SchSymbol:
    """A symbol instance in a schematic (refdes + lib_id + position)."""
    refdes: str
    lib_id: str
    sheet: str
    pos: tuple[float, float]
    rotation: float = 0.0
    mirror: str = ""
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class SchLabel:
    """A net label in a schematic (local, hierarchical, or global)."""
    name: str
    kind: str  # "local" | "hierarchical" | "global" | "power"
    sheet: str
    pos: tuple[float, float]


@dataclass
class SchSheet:
    """A hierarchical sheet reference."""
    name: str
    file: str
    parent: str | None  # parent sheet relative path


@dataclass
class Fact:
    category: str           # e.g. "spi_bus", "power_rail", "io"
    function: str           # e.g. "SCK_B0", "AIN0", "EINT"
    refdes: str | None = None
    pin: str | None = None
    net_name: str | None = None
    part: str | None = None
    confidence: str = "single-source"
    notes: list[str] = field(default_factory=list)


@dataclass
class DocQualityFinding:
    finding: str
    severity: str           # "info" | "warn" | "error"
    details: str
    location: str | None = None


@dataclass
class OpenQuestion:
    question: str
    resolution_path: str
    confidence_if_resolved: str | None = None


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(/tree/(?P<ref>[^/]+)(?P<path>/.*)?)?/?$"
)


def parse_source(s: str) -> Source:
    """Parse a source string into a Source dataclass.

    Accepts:
      - local filesystem path
      - GitHub URL: https://github.com/owner/repo[/tree/ref[/path]]
      - shorthand: owner/repo[@ref][:path]
    """
    s = s.strip()
    p = Path(s).expanduser()
    if p.exists() and p.is_dir():
        return Source(kind="local", path=str(p.resolve()))

    m = GITHUB_URL_RE.match(s)
    if m:
        path = (m.group("path") or "").lstrip("/")
        return Source(
            kind="github",
            owner=m.group("owner"),
            repo=m.group("repo"),
            ref=m.group("ref") or "main",
            path=path,
        )

    # Shorthand: owner/repo[@ref][:path]
    if "/" in s and not s.startswith("/"):
        owner_repo, _, rest = s.partition("@")
        if "@" not in s:
            owner_repo, _, rest = s.partition(":")
        owner, _, repo = owner_repo.partition("/")
        ref, _, path = "", "", ""
        if "@" in s:
            ref_part, _, path_part = rest.partition(":")
            ref = ref_part
            path = path_part
        elif ":" in rest:
            ref = ""
            path = rest
        else:
            ref = ""
            path = ""
        if owner and repo:
            return Source(
                kind="github",
                owner=owner,
                repo=repo,
                ref=ref or "main",
                path=path,
            )

    raise ValueError(f"Could not parse source: {s!r}")


# ---------------------------------------------------------------------------
# gh CLI wrapper + cache
# ---------------------------------------------------------------------------

class Fetcher:
    """Fetches files via `gh api` with on-disk caching keyed by resolved SHA."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def auth_check(self) -> None:
        """Raise if gh CLI is not authenticated."""
        try:
            subprocess.run(
                ["gh", "auth", "status"],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise SystemExit(
                "ERROR: `gh` CLI not found. Install via `brew install gh` or "
                "https://cli.github.com/, then run `gh auth login`."
            )
        except subprocess.CalledProcessError as e:
            raise SystemExit(
                "ERROR: `gh` CLI not authenticated. Run `gh auth login` first.\n"
                f"gh stderr: {e.stderr}"
            )

    def gh(self, *args: str) -> str:
        """Run `gh` and return stdout. Raises on non-zero exit."""
        try:
            r = subprocess.run(
                ["gh", *args],
                check=True, capture_output=True, text=True,
            )
            return r.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"gh {' '.join(args)} failed (exit {e.returncode}): {e.stderr}"
            )

    def resolve_ref(self, owner: str, repo: str, ref: str) -> str:
        """Resolve a branch/tag/short-sha to a full immutable commit SHA."""
        out = self.gh(
            "api", f"repos/{owner}/{repo}/commits/{ref}",
            "--jq", ".sha",
        )
        return out.strip()

    def list_tree(
        self, owner: str, repo: str, sha: str, path: str = ""
    ) -> list[dict]:
        """Recursively list every file at `path` for the given immutable SHA.

        Uses the git trees API (no 1000-entry limit on the contents endpoint).
        Returns a list of dicts with keys {path, type, sha, size}.
        """
        # Get the root tree SHA from the commit, then resolve `path` to a tree SHA.
        commit = json.loads(
            self.gh("api", f"repos/{owner}/{repo}/commits/{sha}")
        )
        tree_sha = commit["commit"]["tree"]["sha"]

        # Walk the path component-by-component to find the subtree we want.
        for part in [p for p in path.split("/") if p]:
            tree = json.loads(
                self.gh("api", f"repos/{owner}/{repo}/git/trees/{tree_sha}")
            )
            entry = next(
                (e for e in tree["tree"] if e["path"] == part), None
            )
            if entry is None or entry["type"] != "tree":
                return []
            tree_sha = entry["sha"]

        # Now recursively list this subtree.
        tree = json.loads(
            self.gh(
                "api",
                f"repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1",
            )
        )
        return [
            {
                "path": e["path"],
                "type": e["type"],
                "sha": e.get("sha"),
                "size": e.get("size"),
            }
            for e in tree["tree"]
            if e["type"] == "blob"
        ]

    def fetch_blob(
        self, owner: str, repo: str, blob_sha: str, dest: Path
    ) -> Path:
        """Fetch a blob by SHA via `gh api`, decode, and cache to dest."""
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = self.gh(
            "api", f"repos/{owner}/{repo}/git/blobs/{blob_sha}",
            "--jq", ".content",
        )
        # gh api returns the base64-encoded content; can include whitespace.
        decoded = b64decode("".join(raw.split()))
        dest.write_bytes(decoded)
        return dest


def cache_key(source: Source) -> str:
    """Stable cache directory name for a given Source."""
    if source.kind == "local":
        h = hashlib.sha256(source.path.encode("utf-8")).hexdigest()[:12]
        return f"local__{h}"
    assert source.owner and source.repo and source.resolved_sha
    path_hash = hashlib.sha256(
        (source.path or "").encode("utf-8")
    ).hexdigest()[:8]
    return f"{source.owner}__{source.repo}__{source.resolved_sha[:12]}__{path_hash}"


def populate_cache(
    fetcher: Fetcher, source: Source, cache_subdir: Path
) -> list[FileEntry]:
    """Fetch every file under the project path into the cache. Returns inventory."""
    assert source.kind == "github" and source.resolved_sha
    blobs = fetcher.list_tree(
        source.owner, source.repo, source.resolved_sha, source.path
    )
    inventory: list[FileEntry] = []
    # Filter to the file types we care about — extracts + schematic + project.
    relevant_suffix = (
        ".kicad_sch", ".kicad_sym", ".kicad_pro", ".kicad_pcb",
        ".csv", ".ipc", ".pos", ".net",
    )
    relevant_name = (
        "netlist.ipc", "bom.csv", "positions.csv", "designators.csv",
        # sym-lib-table is a KiCad project-config file (no extension) describing
        # external symbol library nicknames. Lean v1.1 doesn't parse it (deferred
        # to v1.2), but include it in the cache so v1.2 doesn't need a re-fetch.
        "sym-lib-table",
    )
    for entry in blobs:
        rel = entry["path"]
        if not (rel.endswith(relevant_suffix) or any(
            rel.endswith(n) for n in relevant_name
        )):
            continue
        dest = cache_subdir / rel
        try:
            fetcher.fetch_blob(
                source.owner, source.repo, entry["sha"], dest
            )
        except Exception as e:
            # Don't bail on a single file fetch failure; continue with what we have.
            print(
                f"WARN: failed to fetch {rel}: {e}", file=sys.stderr
            )
            continue
        inventory.append(
            FileEntry(
                rel_path=rel, size=entry.get("size"), sha=entry.get("sha")
            )
        )
    return inventory


def local_inventory(root: Path) -> list[FileEntry]:
    """Inventory a local project directory."""
    relevant_suffix = (
        ".kicad_sch", ".kicad_sym", ".kicad_pro", ".kicad_pcb",
        ".csv", ".ipc", ".pos", ".net",
    )
    relevant_name = (
        "netlist.ipc", "bom.csv", "positions.csv", "designators.csv",
        # sym-lib-table is a KiCad project-config file (no extension) describing
        # external symbol library nicknames. Lean v1.1 doesn't parse it (deferred
        # to v1.2), but include it in the cache so v1.2 doesn't need a re-fetch.
        "sym-lib-table",
    )
    out: list[FileEntry] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        rel_s = str(rel)
        if rel_s.endswith(relevant_suffix) or any(
            rel_s.endswith(n) for n in relevant_name
        ):
            out.append(FileEntry(rel_path=rel_s, size=p.stat().st_size))
    return out


# ---------------------------------------------------------------------------
# BOM parser (with header detection)
# ---------------------------------------------------------------------------

# Column-name aliases (lowercased) for header detection.
BOM_COL_ALIASES = {
    "refdes": [
        "refdes", "reference", "designator", "designators", "ref",
    ],
    "part": ["part", "value", "partvalue", "footprint"],
    "description": ["description", "desc"],
    "manufacturer": ["manufacturer", "mfr", "mfg"],
    "manufacturer_pn": [
        "mfr part", "manufacturer part number", "mpn", "mfr pn",
        "manufacturer pn", "manufacturerpartnumber",
    ],
    "lcsc": ["lcsc", "lcsc part #", "lcsc part", "lcsc pn", "lcscpn"],
    "quantity": ["quantity", "qty", "count"],
    "package": ["package", "footprint", "package/footprint"],
}


def parse_bom(text: str) -> list[BOMEntry]:
    """Parse a BOM CSV with flexible header detection.

    Multi-refdes rows (e.g. "R1,R2,R3" in one row) are split into one
    entry per refdes. Returns an empty list if no recognizable header.
    Strips UTF-8 BOM if present.
    """
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if not rows:
        return []

    # Find the header row: the first row that contains a refdes-like column.
    header_idx = -1
    header: list[str] = []
    for i, row in enumerate(rows[:20]):  # search first 20 lines
        lower = [c.strip().lower() for c in row]
        if any(c in BOM_COL_ALIASES["refdes"] for c in lower):
            header_idx = i
            header = lower
            break

    if header_idx < 0:
        return []

    # Build column index map from canonical names → column index.
    col_idx: dict[str, int] = {}
    for canonical, aliases in BOM_COL_ALIASES.items():
        for j, h in enumerate(header):
            if h in aliases:
                col_idx[canonical] = j
                break

    entries: list[BOMEntry] = []
    for row in rows[header_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        raw = {h: row[j] if j < len(row) else "" for j, h in enumerate(header)}

        def get(canonical: str) -> str | None:
            j = col_idx.get(canonical)
            if j is None or j >= len(row):
                return None
            v = row[j].strip()
            return v or None

        refdes_str = get("refdes")
        if not refdes_str:
            continue
        # Split multi-refdes cells (separated by comma, semicolon, or whitespace).
        for refdes in re.split(r"[\s,;]+", refdes_str):
            refdes = refdes.strip()
            if not refdes:
                continue
            qty_raw = get("quantity")
            try:
                qty = int(qty_raw) if qty_raw else None
            except ValueError:
                qty = None
            entries.append(
                BOMEntry(
                    refdes=refdes,
                    part=get("part"),
                    description=get("description"),
                    manufacturer=get("manufacturer"),
                    manufacturer_pn=get("manufacturer_pn"),
                    lcsc=get("lcsc"),
                    quantity=qty,
                    package=get("package"),
                    raw=raw,
                )
            )
    return entries


# ---------------------------------------------------------------------------
# Positions CSV parser
# ---------------------------------------------------------------------------

POSITIONS_ALIASES = {
    "refdes": ["refdes", "reference", "designator", "designators", "ref"],
    "x": ["posx", "x", "centerx", "x (mm)", "mid x", "midx"],
    "y": ["posy", "y", "centery", "y (mm)", "mid y", "midy"],
    "rotation": ["rot", "rotation", "angle"],
    "side": ["side", "layer"],
    "package": ["package", "footprint", "value"],
}


def parse_positions(text: str) -> list[Position]:
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if not rows:
        return []

    header_idx = -1
    header: list[str] = []
    for i, row in enumerate(rows[:20]):
        lower = [c.strip().lower() for c in row]
        if any(c in POSITIONS_ALIASES["refdes"] for c in lower):
            header_idx = i
            header = lower
            break

    if header_idx < 0:
        return []

    col_idx: dict[str, int] = {}
    for canonical, aliases in POSITIONS_ALIASES.items():
        for j, h in enumerate(header):
            if h in aliases:
                col_idx[canonical] = j
                break

    out: list[Position] = []
    for row in rows[header_idx + 1:]:
        if not any(cell.strip() for cell in row):
            continue

        def getf(canonical: str) -> float | None:
            j = col_idx.get(canonical)
            if j is None or j >= len(row):
                return None
            try:
                return float(row[j])
            except (ValueError, AttributeError):
                return None

        def gets(canonical: str) -> str | None:
            j = col_idx.get(canonical)
            if j is None or j >= len(row):
                return None
            return row[j].strip() or None

        refdes = gets("refdes")
        if not refdes:
            continue
        x = getf("x")
        y = getf("y")
        rot = getf("rotation") or 0.0
        if x is None or y is None:
            continue
        out.append(
            Position(
                refdes=refdes,
                x=x, y=y, rotation=rot,
                side=gets("side"),
                package=gets("package"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# IPC-D-356A netlist parser
# ---------------------------------------------------------------------------

# IPC-D-356A is a fixed-column format. The relevant column layout:
#   col 1:    Record code ('317' = component pad/via record, '378' = test point)
#   col 4-17: Net name (right-padded with spaces, may start with N/C for no-connect)
#   col 21-26: Refdes (6 chars, left-justified, padded with spaces; no - separator)
#   col 27-30: Pin number (4 chars, left-justified)
#   col 32:   D' for via (vs blank for component pad)
#
# The exact column boundaries vary slightly between KiCad versions; we use
# regex to be tolerant. Format reference: https://www.ipc.org/

# IPC-D-356A is fixed-column. Approximate column boundaries (1-indexed):
#   1-3:    record code (317 = via, 327 = component pad, 378 = test point, etc.)
#   4-17:   net name (right-padded with spaces); 14 chars wide
#   18-20:  feature designation (e.g. spaces or VIA)
#   21-26:  refdes (left-justified, 6 chars)
#   27:     '-' separator before pin number
#   28-31:  pin number (4 chars)
# Source format reference: IPC-D-356A standard, KiCad's PCB > Fabrication Outputs > IPC-D-356.

# We extract by Python slicing; this is more robust than whitespace-tolerant regex
# because nets and refdeses can contain dashes / underscores / hierarchical paths.

def parse_ipc_netlist(text: str) -> list[NetlistEntry]:
    """Parse IPC-D-356A netlist output (KiCad's `netlist.ipc`).

    Returns one entry per (refdes, pin) → net mapping for component-pad
    records (327). Vias (317) and no-connects (net name 'N/C') are filtered.
    """
    entries: list[NetlistEntry] = []
    for line in text.splitlines():
        if len(line) < 31:
            continue
        code = line[0:3]
        if code != "327":  # component-pad records only; skip vias and metadata
            continue
        net = line[3:17].strip()
        refdes = line[20:26].strip()
        # Pin is preceded by '-' at col 27; pin runs from 28 to ~31.
        if line[26] != "-":
            continue
        pin = line[27:31].strip()
        if not net or not refdes or not pin:
            continue
        if net.upper().startswith("N/C"):
            continue
        entries.append(
            NetlistEntry(net=net, refdes=refdes, pin=pin, via=False)
        )
    return entries


# ---------------------------------------------------------------------------
# Cache manifest (v1.1)
# ---------------------------------------------------------------------------

def write_cache_manifest(cache_subdir: Path, source: Source) -> None:
    """Write manifest.json into a cache dir so re-runs can detect stale caches."""
    manifest = {
        "extractor_version": EXTRACTOR_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": asdict(source),
    }
    (cache_subdir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def read_cache_manifest(cache_subdir: Path) -> dict | None:
    """Read manifest.json from a cache dir; return None if missing or unreadable."""
    p = cache_subdir / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def cache_is_stale(cache_subdir: Path) -> tuple[bool, str]:
    """Return (is_stale, reason). Cache is stale if manifest is missing or
    extractor_version / output_schema_version don't match current values."""
    m = read_cache_manifest(cache_subdir)
    if m is None:
        # Cache exists but pre-dates manifest writing (v1 cache). Treat as stale.
        if cache_subdir.exists() and any(cache_subdir.iterdir()):
            return (True, "no manifest.json (v1-shaped cache)")
        return (False, "empty cache; will populate")
    ev = m.get("extractor_version")
    sv = m.get("output_schema_version")
    if ev != EXTRACTOR_VERSION:
        return (True, f"extractor_version {ev} != {EXTRACTOR_VERSION}")
    if sv != OUTPUT_SCHEMA_VERSION:
        return (True, f"output_schema_version {sv} != {OUTPUT_SCHEMA_VERSION}")
    return (False, "manifest matches")


# ---------------------------------------------------------------------------
# Schematic parser (sexpdata-based)
# ---------------------------------------------------------------------------

def _sexp_walk(node: Any, head: str) -> Iterator[list]:
    """Yield every sub-list whose head symbol equals `head`."""
    if isinstance(node, list):
        if node and isinstance(node[0], sexpdata.Symbol) and str(node[0]) == head:
            yield node
        for child in node:
            yield from _sexp_walk(child, head)


def _sexp_get(node: list, head: str) -> list | None:
    """Return the first immediate child that's a list starting with `head`."""
    for child in node:
        if isinstance(child, list) and child and isinstance(child[0], sexpdata.Symbol) \
                and str(child[0]) == head:
            return child
    return None


def _sexp_str(value: Any) -> str:
    if isinstance(value, sexpdata.Symbol):
        return str(value)
    if isinstance(value, str):
        return value
    return repr(value)


def _at_xy(node: list) -> tuple[float, float, float]:
    """Extract (x, y, rotation) from an (at X Y [ROT]) node."""
    pos = _sexp_get(node, "at")
    if not pos or len(pos) < 3:
        return (0.0, 0.0, 0.0)
    x = float(pos[1])
    y = float(pos[2])
    rot = float(pos[3]) if len(pos) > 3 else 0.0
    return (x, y, rot)


def probe_schematic_version(path: Path) -> tuple[str | None, str | None]:
    """Read (version YYYYMMDD) and (generator ...) from a .kicad_sch root.

    Returns (version, generator). Either may be None if not found / parse fails.
    Reads only the first ~2 KB; (version) and (generator) are emitted near the
    top of every KiCad schematic, so a full parse isn't needed for the probe.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            head = fp.read(2048)
    except Exception:
        return (None, None)
    ver_match = re.search(r"\(version\s+(\d+)\)", head)
    gen_match = re.search(r'\(generator\s+(?:"([^"]+)"|(\S+?))\s*\)', head)
    version = ver_match.group(1) if ver_match else None
    generator = (gen_match.group(1) or gen_match.group(2)) if gen_match else None
    return (version, generator)


def parse_schematic(path: Path, sheet_name: str) -> tuple[list[SchSymbol], list[SchLabel], list[SchSheet]]:
    """Parse a single .kicad_sch file. Returns (symbols, labels, sheets)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        root = sexpdata.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Failed to parse {path}: {e}")

    symbols: list[SchSymbol] = []
    labels: list[SchLabel] = []
    sheets: list[SchSheet] = []

    # Walk symbol instances. Note: in KiCad ≥ 7 schematics, the structure is
    #   (symbol (lib_id "...") (at X Y ROT) (mirror ...) (property "Reference" "U1" ...) ...)
    # We avoid `(library_symbols ...)` (the symbol cache) by only walking
    # top-level (symbol ...) entries that have a (lib_id) child *and* a
    # (property "Reference" ...) child.
    for sym in _sexp_walk(root, "symbol"):
        lib_id_node = _sexp_get(sym, "lib_id")
        if not lib_id_node or len(lib_id_node) < 2:
            continue
        lib_id = _sexp_str(lib_id_node[1])
        # Library cache symbols don't have at — instances do.
        if not _sexp_get(sym, "at"):
            continue
        x, y, rot = _at_xy(sym)
        mirror_node = _sexp_get(sym, "mirror")
        mirror = _sexp_str(mirror_node[1]) if mirror_node and len(mirror_node) > 1 else ""

        properties: dict[str, str] = {}
        for prop in sym:
            if not (isinstance(prop, list) and prop and isinstance(prop[0], sexpdata.Symbol)
                    and str(prop[0]) == "property"):
                continue
            if len(prop) < 3:
                continue
            properties[_sexp_str(prop[1])] = _sexp_str(prop[2])

        refdes = properties.get("Reference", "")
        if not refdes:
            continue
        symbols.append(SchSymbol(
            refdes=refdes,
            lib_id=lib_id,
            sheet=sheet_name,
            pos=(x, y),
            rotation=rot,
            mirror=mirror,
            properties=properties,
        ))

    # Labels: local, hierarchical, global, power.
    label_kinds = {
        "label": "local",
        "hierarchical_label": "hierarchical",
        "global_label": "global",
    }
    for kind_head, kind_name in label_kinds.items():
        for lbl in _sexp_walk(root, kind_head):
            if len(lbl) < 2:
                continue
            name = _sexp_str(lbl[1])
            x, y, _ = _at_xy(lbl)
            labels.append(SchLabel(
                name=name, kind=kind_name, sheet=sheet_name,
                pos=(x, y),
            ))

    # Hierarchical sheet references: (sheet ... (property "Sheetname" ...) (property "Sheetfile" "X.kicad_sch") ...)
    for sh in _sexp_walk(root, "sheet"):
        if not _sexp_get(sh, "at"):
            continue
        sheet_name_val = ""
        sheet_file = ""
        for prop in sh:
            if not (isinstance(prop, list) and prop and isinstance(prop[0], sexpdata.Symbol)
                    and str(prop[0]) == "property"):
                continue
            if len(prop) < 3:
                continue
            k = _sexp_str(prop[1])
            v = _sexp_str(prop[2])
            if k == "Sheetname":
                sheet_name_val = v
            elif k == "Sheetfile":
                sheet_file = v
        if sheet_file:
            sheets.append(SchSheet(
                name=sheet_name_val or sheet_file,
                file=sheet_file,
                parent=sheet_name,
            ))

    return symbols, labels, sheets


def walk_schematic_tree(root_sch: Path) -> dict[str, dict]:
    """Walk the schematic hierarchy starting at root_sch.

    Returns dict mapping sheet rel-path → {symbols, labels, sheets}.
    """
    base = root_sch.parent
    result: dict[str, dict] = {}
    pending: list[tuple[Path, str]] = [(root_sch, root_sch.name)]
    visited: set[str] = set()
    while pending:
        path, name = pending.pop()
        rel = str(path.relative_to(base))
        if rel in visited:
            continue
        visited.add(rel)
        try:
            syms, lbls, sheets = parse_schematic(path, rel)
        except Exception as e:
            print(f"WARN: failed parsing {rel}: {e}", file=sys.stderr)
            continue
        result[rel] = {
            "symbols": [asdict(s) for s in syms],
            "labels": [asdict(l) for l in lbls],
            "sheets": [asdict(s) for s in sheets],
        }
        for sh in sheets:
            sub = base / sh.file
            if sub.exists() and str(sub.relative_to(base)) not in visited:
                pending.append((sub, sh.file))
    return result


# ---------------------------------------------------------------------------
# Cross-reference + facts
# ---------------------------------------------------------------------------

def find_root_schematic(file_paths: list[str]) -> str | None:
    """Heuristic: the root schematic is the .kicad_sch with the same stem as
    the .kicad_pro, or — failing that — the .kicad_sch at the shallowest
    directory depth not referenced by any other schematic as a sub-sheet.
    """
    pro_files = [p for p in file_paths if p.endswith(".kicad_pro")]
    if pro_files:
        # Pick the shallowest .kicad_pro; the matching .kicad_sch is the root.
        pro_files.sort(key=lambda p: (p.count("/"), p))
        root_pro = pro_files[0]
        root_dir = "/".join(root_pro.split("/")[:-1])
        root_stem = Path(root_pro).stem
        cand = f"{root_dir}/{root_stem}.kicad_sch" if root_dir else f"{root_stem}.kicad_sch"
        if cand in file_paths:
            return cand
    # Fallback: shallowest .kicad_sch.
    sch_files = [p for p in file_paths if p.endswith(".kicad_sch")]
    if not sch_files:
        return None
    sch_files.sort(key=lambda p: (p.count("/"), p))
    return sch_files[0]


def find_production_extracts(
    file_paths: list[str],
) -> dict[str, list[str]]:
    """Group production extracts by directory. Returns {dir: [files]}."""
    extracts: dict[str, list[str]] = defaultdict(list)
    for p in file_paths:
        name = p.split("/")[-1].lower()
        if name.endswith(("bom.csv", "netlist.ipc", "positions.csv", "designators.csv")):
            d = "/".join(p.split("/")[:-1])
            extracts[d].append(p)
    return dict(extracts)


def select_production_dir(extracts: dict[str, list[str]]) -> str | None:
    """Pick the most-recent / preferred production directory.

    Heuristic: prefer dirs containing the most extract types (bom + netlist +
    positions); tiebreak by lexicographic max (typical for vXpYrZ naming).
    """
    if not extracts:
        return None
    scored = []
    for d, files in extracts.items():
        names = [f.split("/")[-1].lower() for f in files]
        score = sum([
            any(n.endswith("bom.csv") for n in names),
            any(n.endswith("netlist.ipc") for n in names),
            any(n.endswith("positions.csv") for n in names),
        ])
        scored.append((score, d))
    scored.sort(reverse=True)
    return scored[0][1]


def build_facts(
    bom: list[BOMEntry],
    netlist: list[NetlistEntry],
    schematics: dict[str, dict],
) -> list[Fact]:
    """Cross-reference BOM × netlist × schematic into structured facts.

    For v1, this emits one fact per BOM entry that's also referenced in the
    netlist. Confidence is bom+netlist or bom+netlist+schematic depending on
    whether the refdes also appears in the parsed schematic.
    """
    bom_by_refdes = {e.refdes: e for e in bom}

    # Group netlist by refdes for fast lookup.
    netlist_by_refdes: dict[str, list[NetlistEntry]] = defaultdict(list)
    for e in netlist:
        netlist_by_refdes[e.refdes].append(e)

    # Build a set of refdeses present in the schematic.
    schematic_refdeses: set[str] = set()
    for sheet in schematics.values():
        for s in sheet["symbols"]:
            schematic_refdeses.add(s["refdes"])

    facts: list[Fact] = []
    # Emit one fact per refdes-pin in the netlist, joined to BOM.
    for refdes, entries in netlist_by_refdes.items():
        bom_entry = bom_by_refdes.get(refdes)
        confidence_base: list[str] = ["netlist"]
        if bom_entry is not None:
            confidence_base.append("bom")
        if refdes in schematic_refdeses:
            confidence_base.append("schematic")
        confidence = "+".join(confidence_base)

        for ne in entries:
            f = Fact(
                category=_categorize(refdes, ne.net),
                function=ne.net,
                refdes=refdes,
                pin=ne.pin,
                net_name=ne.net,
                part=(bom_entry.part if bom_entry else None) or
                     (bom_entry.manufacturer_pn if bom_entry else None) or None,
                confidence=confidence,
                notes=[],
            )
            if bom_entry and bom_entry.lcsc:
                f.notes.append(f"LCSC: {bom_entry.lcsc}")
            if bom_entry and bom_entry.description:
                f.notes.append(bom_entry.description)
            facts.append(f)

    return facts


# Categorizer: rough peripheral grouping based on net name and refdes prefix.
_CATEGORY_RULES = [
    (re.compile(r"^GND$|^GND\b"), "ground"),
    (re.compile(r"\+?(\d+\.?\d*V\d*|VIN|VBUS|VDD|VCC|VREG)", re.I), "power_rail"),
    (re.compile(r"SPI|MOSI|MISO|SCK|CS_?\d|SS\b", re.I), "spi_bus"),
    (re.compile(r"I2C|SDA|SCL", re.I), "i2c_bus"),
    (re.compile(r"USB|D\+|D-", re.I), "usb"),
    (re.compile(r"AIN|AI\d", re.I), "analog_in"),
    (re.compile(r"AOUT|AO\d", re.I), "analog_out"),
    (re.compile(r"EINT|TRIG", re.I), "external_interrupt"),
    (re.compile(r"DIO|D\d+_?5V|D\d+_0_3V3", re.I), "digital_io"),
    (re.compile(r"^COL_?\d|^ROW_?\d", re.I), "led_matrix"),
    (re.compile(r"^GP\d+|^D\d+\b", re.I), "gpio"),
    (re.compile(r"ETH", re.I), "ethernet"),
    (re.compile(r"XIP|QSPI", re.I), "qspi"),
]

_PREFIX_CATEGORIES = {
    "U": "ic",
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "Q": "transistor",
    "D": "diode",
    "J": "connector",
    "SW": "switch",
    "Y": "crystal",
    "BT": "battery",
    "F": "fuse",
}


def _categorize(refdes: str, net: str) -> str:
    for rx, cat in _CATEGORY_RULES:
        if rx.search(net):
            return cat
    # Fall back to refdes prefix.
    m = re.match(r"^([A-Z]+)", refdes)
    if m:
        prefix = m.group(1)
        return _PREFIX_CATEGORIES.get(prefix, "other")
    return "other"


# ---------------------------------------------------------------------------
# Doc-quality flags
# ---------------------------------------------------------------------------

def find_doc_quality(
    file_paths: list[str],
    bom: list[BOMEntry],
    netlist: list[NetlistEntry],
    schematics: dict[str, dict],
    extracts_dirs: dict[str, list[str]],
) -> tuple[list[DocQualityFinding], list[OpenQuestion]]:
    findings: list[DocQualityFinding] = []
    questions: list[OpenQuestion] = []

    # Missing extract types?
    if not bom:
        findings.append(DocQualityFinding(
            finding="missing_or_unparseable_bom",
            severity="warn",
            details=(
                "No BOM CSV with a recognizable header was found. "
                "Without BOM, refdes → part mapping is unavailable; "
                "facts will lack part/manufacturer/LCSC information."
            ),
        ))
    if not netlist:
        findings.append(DocQualityFinding(
            finding="missing_or_unparseable_netlist",
            severity="warn",
            details=(
                "No IPC-D-356A netlist (`netlist.ipc`) was found or it could "
                "not be parsed. Without it, refdes-pin → net mapping must "
                "rely on schematic-only resolution (not implemented in v1)."
            ),
        ))
    if not extracts_dirs:
        findings.append(DocQualityFinding(
            finding="no_production_extracts",
            severity="info",
            details=(
                "No `production/` directory with BOM / netlist / positions "
                "was found. Schema-only review is degraded; many firmware-"
                "relevant facts may be unresolvable."
            ),
        ))

    # Refdeses in schematic that aren't in BOM
    sch_refdeses = {
        s["refdes"]
        for sheet in schematics.values()
        for s in sheet["symbols"]
    }
    bom_refdeses = {e.refdes for e in bom}
    sch_only = sch_refdeses - bom_refdeses
    if bom and sch_only:
        findings.append(DocQualityFinding(
            finding="refdeses_in_schematic_but_not_bom",
            severity="info",
            details=(
                f"{len(sch_only)} refdeses appear in the schematic but not "
                f"the BOM (often DNP / mechanical / virtual). "
                f"Sample: {sorted(sch_only)[:10]}"
            ),
        ))
    # Refdeses in BOM that aren't in schematic
    bom_only = bom_refdeses - sch_refdeses
    if sch_refdeses and bom_only:
        findings.append(DocQualityFinding(
            finding="refdeses_in_bom_but_not_schematic",
            severity="warn",
            details=(
                f"{len(bom_only)} refdeses appear in the BOM but not the "
                f"parsed schematic — possible parser miss or BOM staleness. "
                f"Sample: {sorted(bom_only)[:10]}"
            ),
        ))

    # Schematic version probe
    # (We don't bail; just surface the version per file.)
    versions: dict[str, str] = {}
    # We can't probe here because we're operating on parsed dicts; this would
    # need re-parsing the raw .kicad_sch — skipped for v1.

    return findings, questions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract structured KiCad design facts to JSON.",
    )
    ap.add_argument("--source", required=True,
                    help="Local path | owner/repo[@ref][:path] | GitHub URL")
    ap.add_argument("--cache", default=".kicad-review",
                    help="Cache directory (default: .kicad-review)")
    ap.add_argument("--no-cache", action="store_true",
                    help="Skip cache; re-fetch all files")
    ap.add_argument("--schema-version", default=None,
                    help="Force-allow a specific KiCad schema version")
    args = ap.parse_args()

    source = parse_source(args.source)
    cache_root = Path(args.cache).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    fetcher = Fetcher(cache_root)
    file_inventory: list[FileEntry]
    project_root: Path

    cache_status: dict[str, str] = {}  # surfaced into JSON output

    if source.kind == "github":
        fetcher.auth_check()
        source.resolved_sha = fetcher.resolve_ref(
            source.owner, source.repo, source.ref
        )
        cache_subdir = cache_root / cache_key(source)

        # v1.1: stale-cache detection. If the cache exists but predates the
        # current extractor / output-schema version, warn loudly and refetch.
        stale, reason = cache_is_stale(cache_subdir)
        if stale and not args.no_cache:
            print(
                f"WARN: stale cache at {cache_subdir} ({reason}); refetching.",
                file=sys.stderr,
            )
            import shutil
            shutil.rmtree(cache_subdir)
            cache_status["action"] = "stale-refetched"
            cache_status["reason"] = reason
        elif args.no_cache and cache_subdir.exists():
            import shutil
            shutil.rmtree(cache_subdir)
            cache_status["action"] = "force-refetched"
        else:
            cache_status["action"] = "populated-or-served-from-cache"
        cache_status["reason"] = cache_status.get("reason", reason)

        cache_subdir.mkdir(parents=True, exist_ok=True)
        # Fetch into the cache mirroring the path-within-repo. Files land at
        # cache_subdir/<rel-path-within-source.path> directly (populate_cache
        # uses paths relative to source.path), so project_root == cache_subdir.
        file_inventory = populate_cache(fetcher, source, cache_subdir)
        project_root = cache_subdir
        # Stamp manifest after a successful population so a later run can detect
        # the version this cache was built with.
        write_cache_manifest(cache_subdir, source)
    else:
        project_root = Path(source.path)
        file_inventory = local_inventory(project_root)
        cache_status["action"] = "local-source-no-cache"

    # Build path list (relative to project_root)
    file_paths = [e.rel_path for e in file_inventory]

    # ---- BOM / netlist / positions ----
    extracts_dirs = find_production_extracts(file_paths)
    chosen_extract_dir = select_production_dir(extracts_dirs)

    bom: list[BOMEntry] = []
    netlist: list[NetlistEntry] = []
    positions: list[Position] = []

    if chosen_extract_dir is not None:
        for f in extracts_dirs[chosen_extract_dir]:
            full = project_root / f
            if not full.exists():
                continue
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"WARN: failed reading {f}: {e}", file=sys.stderr)
                continue
            name = f.split("/")[-1].lower()
            if name.endswith("bom.csv"):
                bom.extend(parse_bom(text))
            elif name.endswith("netlist.ipc"):
                netlist.extend(parse_ipc_netlist(text))
            elif name.endswith("positions.csv"):
                positions.extend(parse_positions(text))

    # ---- Schematic-version probe (v1.1) ----
    # Probe every .kicad_sch file for (version YYYYMMDD) + (generator ...).
    # Recorded into inventory.kicad_schematic_versions so consumers can detect
    # KiCad version drift without re-parsing the schematics.
    sch_versions: dict[str, dict] = {}
    for f in file_paths:
        if not f.endswith(".kicad_sch"):
            continue
        full = project_root / f
        if not full.exists():
            continue
        ver, gen = probe_schematic_version(full)
        sch_versions[f] = {"version": ver, "generator": gen}
        if ver and ver not in KICAD_SCHEMA_VERSIONS_TESTED:
            # Don't bail; warn once via doc_quality (added later in this fn).
            sch_versions[f]["untested"] = True

    # ---- Schematic walk ----
    root_sch_rel = find_root_schematic(file_paths)
    schematics: dict[str, dict] = {}
    if root_sch_rel:
        root_sch = project_root / root_sch_rel
        if root_sch.exists():
            schematics = walk_schematic_tree(root_sch)

    # ---- Facts ----
    facts = build_facts(bom, netlist, schematics)

    # ---- Doc quality ----
    findings, questions = find_doc_quality(
        file_paths, bom, netlist, schematics, extracts_dirs
    )

    # ---- Emit JSON ----
    untested_schema_files = [
        f for f, info in sch_versions.items() if info.get("untested")
    ]
    if untested_schema_files:
        findings.append(DocQualityFinding(
            finding="kicad_schema_version_untested",
            severity="info",
            details=(
                f"{len(untested_schema_files)} .kicad_sch file(s) have a "
                f"(version YYYYMMDD) outside the tested range "
                f"{KICAD_SCHEMA_VERSIONS_TESTED}. "
                f"Output may be partial. Sample: {untested_schema_files[:3]}"
            ),
        ))

    out = {
        "tool": {"name": "kicad-extract", "version": EXTRACTOR_VERSION},
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": asdict(source),
        "cache_dir": str(cache_root),
        "cache_status": cache_status,
        "inventory": {
            "all_files": [asdict(e) for e in file_inventory],
            "production_extract_dir": chosen_extract_dir,
            "root_schematic": root_sch_rel,
            "kicad_schematic_versions": sch_versions,
            "kicad_schema_versions_tested": list(KICAD_SCHEMA_VERSIONS_TESTED),
        },
        "bom": [asdict(b) for b in bom],
        "netlist": [asdict(n) for n in netlist],
        "positions": [asdict(p) for p in positions],
        "schematic": schematics,
        "facts": [asdict(f) for f in facts],
        "doc_quality": [asdict(f) for f in findings],
        "open_questions": [asdict(q) for q in questions],
        "stats": {
            "n_files": len(file_inventory),
            "n_bom": len(bom),
            "n_netlist": len(netlist),
            "n_positions": len(positions),
            "n_schematic_sheets": len(schematics),
            "n_facts": len(facts),
            "n_doc_quality": len(findings),
            "n_kicad_sch_files": len(sch_versions),
        },
    }

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
