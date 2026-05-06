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
class InstanceRef:
    """One per-instance refdes binding for a schematic symbol.

    A symbol placed in a sub-sheet referenced N times gets one InstanceRef per
    instance path; the per-path Reference can differ from the symbol's default
    Reference property.
    """
    path: str           # "/<root_uuid>/<sheet_uuid>/..."
    reference: str      # per-instance refdes (e.g. "J19" when default is "J5")
    unit: int = 1


@dataclass
class SchSymbol:
    """A symbol instance in a schematic (refdes + lib_id + position).

    v1.1 additions: uuid, unit (default; instance_refs may override per-path),
    instance_refs.
    """
    refdes: str
    lib_id: str
    sheet: str
    pos: tuple[float, float]
    rotation: float = 0.0
    mirror: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    uuid: str | None = None
    unit: int = 1
    instance_refs: list[InstanceRef] = field(default_factory=list)


@dataclass
class SchLabel:
    """A net label in a schematic (local, hierarchical, or global)."""
    name: str
    kind: str  # "local" | "hierarchical" | "global" | "power"
    sheet: str
    pos: tuple[float, float]


@dataclass
class SheetPin:
    """A pin on a hierarchical sheet (parent-side connection point)."""
    name: str
    shape: str    # "input" | "output" | "bidirectional" | "passive" | etc.
    pos: tuple[float, float]


@dataclass
class SchSheet:
    """A hierarchical sheet reference (v1.1: now carries uuid + sheet pins)."""
    name: str
    file: str
    parent: str | None  # parent sheet relative path
    uuid: str | None = None
    at_pos: tuple[float, float] | None = None
    pins: list[SheetPin] = field(default_factory=list)


@dataclass
class SymPinDef:
    """A pin definition extracted from a (lib_symbols ...) entry.

    primary_name is the pin's `(name "...")`. alternates contains any
    `(alternate "...")` siblings (alternate functions like ADC, PWM, etc.).
    pin_offset is the pin's (at X Y ROT) in the symbol-local coordinate frame
    — the connection-point coordinate where wires attach, before applying
    symbol-instance rotation/mirror/translation. Used by the Phase 2 BFS.
    """
    number: str
    primary_name: str
    alternates: list[str] = field(default_factory=list)
    unit: int = 1
    pin_offset: tuple[float, float] = (0.0, 0.0)


@dataclass
class Wire:
    """A wire segment between two coords on a sheet (Phase 2)."""
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class Junction:
    """A junction marker — 3+ wires meeting at one coord (Phase 2)."""
    pos: tuple[float, float]


@dataclass
class WireTraceStep:
    """One step in a wire-trace path (Phase 2): a wire segment, a junction,
    or a passive transit through a 2-pin component."""
    kind: str               # "wire" | "junction" | "transit"
    position: tuple[float, float] | None = None
    end_position: tuple[float, float] | None = None  # for "wire"
    refdes: str | None = None      # for "transit"
    value: str | None = None       # for "transit"
    transit_via: str | None = None # for "transit": which pin we entered/exited


@dataclass
class WireTraceEndpoint:
    """An endpoint reached by a wire-trace BFS (Phase 2)."""
    kind: str               # "label" | "pin" | "boundary"
    value: str              # label name, "<refdes>:<pin>", or "(unconnected)"
    position: tuple[float, float] | None = None
    sheet: str | None = None
    label_kind: str | None = None  # for kind="label": "local"|"hierarchical"|"global"|"power"


@dataclass
class WireTrace:
    """Result of a single BFS from a (refdes, pin) source (Phase 2)."""
    source: dict             # {"refdes", "pin", "sheet", "position"}
    path: list[WireTraceStep]
    endpoints: list[WireTraceEndpoint]
    confidence: str = "single-sheet"
    notes: list[str] = field(default_factory=list)


@dataclass
class SchematicInstance:
    """One resolved (instance_path, refdes, lib_id) tuple for the JSON output.

    The flat `schematic_instances[]` array enumerates these so consumers can
    iterate over per-instance refdeses without re-walking the sheet hierarchy.
    """
    instance_path: str
    file: str
    refdes: str
    lib_id: str
    unit: int = 1
    symbol_uuid: str | None = None


@dataclass
class Fact:
    """v1.1 additions: symbol_pin_name, pin_alternates, instance_confirmed."""
    category: str           # e.g. "spi_bus", "power_rail", "io"
    function: str           # e.g. "SCK_B0", "AIN0", "EINT"
    refdes: str | None = None
    pin: str | None = None
    net_name: str | None = None
    part: str | None = None
    confidence: str = "single-source"
    notes: list[str] = field(default_factory=list)
    # New in v1.1:
    symbol_pin_name: str | None = None       # e.g. "GPIO45_ADC5", "D14", "OUT"
    pin_alternates: list[str] = field(default_factory=list)
    instance_confirmed: bool = False         # True if refdes resolved via instance walker


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


# ---------------------------------------------------------------------------
# (lib_symbols ...) parser — v1.1
# ---------------------------------------------------------------------------
#
# KiCad ≥ 7 schematics embed a (lib_symbols ...) block at the top of each
# sheet caching every symbol used in that sheet. Each entry is:
#
#   (symbol "lib:Name"
#     (pin (number "X") (name "GP45_ADC5") (at ...) ...)
#     (pin ...) ...
#   )
#
# Multi-unit symbols nest sub-symbols whose names follow the convention
# "Name_M_N" (M = body style, N = unit number), and the pin definitions
# live inside those sub-symbols rather than the parent.
#
# The map we build: (lib_id, unit, pin_number) -> SymPinDef.

# Match library-cache symbol names. KiCad convention: <name>_<unit>_<body_style>
# where unit ≥ 1 selects the sub-unit (A/B/...) and body_style is 0 (common) or
# 1 (DeMorgan-equivalent). Unit 0 means "shared across all units" (drawn body
# graphics, optionally pins shared between units like power rails).
_LIB_SUB_NAME_RE = re.compile(r"^(?P<base>.+)_(?P<unit>\d+)_(?P<body>\d+)$")


def _extract_pin_def(pin_node: list, default_unit: int) -> SymPinDef | None:
    """Extract a SymPinDef from a (pin ...) node inside lib_symbols.

    Pin form (KiCad ≥ 7):
      (pin OUTPUT line (at X Y ROT) (length L)
        (name "PrimaryName" ...)
        (number "X" ...)
        (alternate "AltName" line OUTPUT) ...
      )
    """
    number_node = _sexp_get(pin_node, "number")
    name_node = _sexp_get(pin_node, "name")
    if not number_node or len(number_node) < 2:
        return None
    if not name_node or len(name_node) < 2:
        return None
    number = _sexp_str(number_node[1])
    primary = _sexp_str(name_node[1])
    alternates: list[str] = []
    for child in pin_node:
        if (isinstance(child, list) and child
                and isinstance(child[0], sexpdata.Symbol)
                and str(child[0]) == "alternate"
                and len(child) >= 2):
            alternates.append(_sexp_str(child[1]))
    # Pin offset (symbol-local frame) — the connection-point coord where
    # wires attach before instance rotation/mirror/translation are applied.
    offset_x, offset_y, _ = _at_xy(pin_node)
    return SymPinDef(
        number=number,
        primary_name=primary,
        alternates=alternates,
        unit=default_unit,
        pin_offset=(offset_x, offset_y),
    )


def parse_lib_symbols(sch_root: Any) -> dict[tuple[str, int, str], SymPinDef]:
    """Walk top-level children of (lib_symbols ...) in a schematic root.

    Returns: dict keyed by (lib_id, unit, pin_number) -> SymPinDef.

    Design notes:
      - Walk only top-level (lib_symbols ...) children, NOT recursive — we
        deliberately avoid _sexp_walk on the whole tree because it would also
        match nested (symbol ...) blocks elsewhere (instances, etc.).
      - Multi-unit: when a top-level symbol contains nested (symbol "Name_M_N"
        ...) children, recurse one level into them and tag pins with unit N.
      - When pins live directly under the top-level symbol (single-unit case),
        tag them as unit 1.
    """
    pin_map: dict[tuple[str, int, str], SymPinDef] = {}

    lib_symbols_node = _sexp_get(sch_root, "lib_symbols")
    if not lib_symbols_node:
        return pin_map

    # Iterate top-level (symbol "lib:Name" ...) entries only.
    for child in lib_symbols_node[1:]:  # skip the head symbol "lib_symbols"
        if not (isinstance(child, list) and child
                and isinstance(child[0], sexpdata.Symbol)
                and str(child[0]) == "symbol"):
            continue
        if len(child) < 2:
            continue
        lib_id = _sexp_str(child[1])

        # Pins directly at top-level (single-unit symbols) → unit 1.
        for grandchild in child[2:]:
            if not isinstance(grandchild, list) or not grandchild:
                continue
            if not isinstance(grandchild[0], sexpdata.Symbol):
                continue
            head = str(grandchild[0])
            if head == "pin":
                pin_def = _extract_pin_def(grandchild, default_unit=1)
                if pin_def is not None:
                    pin_map[(lib_id, pin_def.unit, pin_def.number)] = pin_def
            elif head == "symbol" and len(grandchild) >= 2:
                # Multi-unit: nested (symbol "Name_M_N" ...) — extract unit.
                sub_name = _sexp_str(grandchild[1])
                m = _LIB_SUB_NAME_RE.match(sub_name)
                unit = int(m.group("unit")) if m else 1
                # Pins live inside the sub-symbol.
                for great in grandchild[2:]:
                    if (isinstance(great, list) and great
                            and isinstance(great[0], sexpdata.Symbol)
                            and str(great[0]) == "pin"):
                        pin_def = _extract_pin_def(great, default_unit=unit)
                        if pin_def is not None:
                            # Keep one entry per (lib_id, unit, pin_number).
                            # Body-style 0 (common) and N (per-unit) can both
                            # contain pins; if body-style 0 and per-unit
                            # sub-symbols both define the same pin, prefer
                            # the per-unit definition (more specific).
                            existing = pin_map.get((lib_id, unit, pin_def.number))
                            if existing is None or unit > 0:
                                pin_map[(lib_id, unit, pin_def.number)] = pin_def
    return pin_map


def parse_kicad_sym_file(path: Path) -> dict[tuple[str, int, str], SymPinDef]:
    """Parse a project-local .kicad_sym file.

    Used as a fallback when a refdes's lib_id isn't found in any sheet's
    embedded lib_symbols. The file's top-level form is (kicad_symbol_lib ...);
    its symbol entries follow the same shape as lib_symbols entries.

    Returns: dict keyed by (lib_id, unit, pin_number) -> SymPinDef. The lib_id
    in the returned keys is keyed on the *symbol name* alone (no nickname
    prefix) — caller must match against either the bare name or the full
    "nickname:Name" lib_id of the schematic instance.
    """
    pin_map: dict[tuple[str, int, str], SymPinDef] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        root = sexpdata.loads(raw)
    except Exception:
        return pin_map

    # The root is (kicad_symbol_lib (version ...) (symbol "Name" ...) ...).
    if not (isinstance(root, list) and root
            and isinstance(root[0], sexpdata.Symbol)
            and str(root[0]) == "kicad_symbol_lib"):
        return pin_map

    for child in root[1:]:
        if not (isinstance(child, list) and child
                and isinstance(child[0], sexpdata.Symbol)
                and str(child[0]) == "symbol"):
            continue
        if len(child) < 2:
            continue
        sym_name = _sexp_str(child[1])

        for grandchild in child[2:]:
            if not isinstance(grandchild, list) or not grandchild:
                continue
            if not isinstance(grandchild[0], sexpdata.Symbol):
                continue
            head = str(grandchild[0])
            if head == "pin":
                pd = _extract_pin_def(grandchild, default_unit=1)
                if pd is not None:
                    pin_map[(sym_name, pd.unit, pd.number)] = pd
            elif head == "symbol" and len(grandchild) >= 2:
                sub_name = _sexp_str(grandchild[1])
                m = _LIB_SUB_NAME_RE.match(sub_name)
                unit = int(m.group("unit")) if m else 1
                for great in grandchild[2:]:
                    if (isinstance(great, list) and great
                            and isinstance(great[0], sexpdata.Symbol)
                            and str(great[0]) == "pin"):
                        pd = _extract_pin_def(great, default_unit=unit)
                        if pd is not None:
                            existing = pin_map.get((sym_name, unit, pd.number))
                            if existing is None or unit > 0:
                                pin_map[(sym_name, unit, pd.number)] = pd
    return pin_map


def lookup_pin_name(
    pin_maps: dict[tuple[str, int, str], SymPinDef],
    lib_id: str,
    unit: int,
    pin_number: str,
) -> SymPinDef | None:
    """Look up a pin definition. Falls back gracefully across unit / lib_id
    forms a real KiCad project might use.

    Lookup order:
      1. (lib_id, unit, pin) exact
      2. (lib_id, 0, pin) — unit 0 = pins shared across all units (e.g. power)
      3. (lib_id, 1, pin) — single-unit symbols default
      4. Bare symbol name (split on ':') with the same unit fallbacks —
         covers project-local .kicad_sym matches where the lib_id includes a
         nickname prefix the .kicad_sym file doesn't have.
    """
    candidate_lib_ids = [lib_id]
    bare = lib_id.split(":", 1)[-1]
    if bare != lib_id:
        candidate_lib_ids.append(bare)

    candidate_units = [unit]
    if unit != 0:
        candidate_units.append(0)
    if unit != 1:
        candidate_units.append(1)

    for lid in candidate_lib_ids:
        for u in candidate_units:
            key = (lid, u, pin_number)
            if key in pin_maps:
                return pin_maps[key]
    return None


# ---------------------------------------------------------------------------
# Pin-coordinate transform (v1.1)
# ---------------------------------------------------------------------------
#
# Compute absolute pin coordinates from a symbol instance's position +
# rotation + mirror, given the pin's offset from the symbol origin (the
# offset is read from the symbol's pin definition in lib_symbols).
#
# Used by the Phase 2 BFS engine. Defined here in Phase 1 so the formula
# is documented in the same module as the data it operates on.
#
# Math:
#   1. Apply symbol rotation around symbol origin to (px, py) offset.
#   2. Translate by symbol_origin to get absolute position.
#   3. Apply mirror flip per (mirror x|y) — done in symbol-local frame
#      BEFORE rotation, since KiCad applies mirror first then rotation.
#
# KiCad rotations are CCW, in degrees, applied in the symbol's local frame.

def absolute_pin_position(
    symbol_origin: tuple[float, float],
    symbol_rotation: float,
    symbol_mirror: str,
    pin_offset: tuple[float, float],
) -> tuple[float, float]:
    """Compute the absolute (x, y) of a pin given symbol placement + pin offset.

    Args:
      symbol_origin: (X, Y) — symbol instance position from `(at X Y ROT)`.
      symbol_rotation: degrees CCW — from same `(at)` block.
      symbol_mirror: "" | "x" | "y" — KiCad `(mirror x|y)` token; "" if absent.
      pin_offset: (px, py) — pin position relative to symbol origin (from
        `(pin ... (at px py prot))` in lib_symbols).

    Returns: absolute (x, y) of the pin.

    NOTE: KiCad applies mirror in the symbol's local frame BEFORE rotation.
    """
    import math
    px, py = pin_offset
    if symbol_mirror == "x":
        # Mirror across X axis (negate Y in local frame)
        py = -py
    elif symbol_mirror == "y":
        # Mirror across Y axis (negate X)
        px = -px
    rad = math.radians(symbol_rotation)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    rx = px * cos_r - py * sin_r
    ry = px * sin_r + py * cos_r
    return (symbol_origin[0] + rx, symbol_origin[1] + ry)


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


def _extract_uuid(node: list) -> str | None:
    """Extract `(uuid "...")` from a node's immediate children."""
    u = _sexp_get(node, "uuid")
    if u and len(u) >= 2:
        return _sexp_str(u[1])
    return None


def _extract_instance_refs(sym_node: list) -> list[InstanceRef]:
    """Extract per-instance refdes bindings from a (symbol ...) node.

    KiCad schematics store these in:
      (instances
        (project "..."
          (path "/<root_uuid>/<sheet_uuid>/..."
            (reference "J19") (unit 1))
          ...
        )
        ...
      )

    Returns one InstanceRef per (path, reference) tuple found.
    """
    out: list[InstanceRef] = []
    instances = _sexp_get(sym_node, "instances")
    if not instances:
        return out
    for proj in instances[1:]:
        if not (isinstance(proj, list) and proj
                and isinstance(proj[0], sexpdata.Symbol)
                and str(proj[0]) == "project"):
            continue
        for path_node in proj[1:]:
            if not (isinstance(path_node, list) and path_node
                    and isinstance(path_node[0], sexpdata.Symbol)
                    and str(path_node[0]) == "path"):
                continue
            if len(path_node) < 2:
                continue
            inst_path = _sexp_str(path_node[1])
            ref_node = _sexp_get(path_node, "reference")
            unit_node = _sexp_get(path_node, "unit")
            if not ref_node or len(ref_node) < 2:
                continue
            reference = _sexp_str(ref_node[1])
            unit = 1
            if unit_node and len(unit_node) >= 2:
                try:
                    unit = int(_sexp_str(unit_node[1]))
                except (ValueError, TypeError):
                    unit = 1
            out.append(InstanceRef(
                path=inst_path, reference=reference, unit=unit,
            ))
    return out


def parse_schematic(
    path: Path, sheet_name: str,
) -> tuple[
    list[SchSymbol], list[SchLabel], list[SchSheet],
    dict[tuple[str, int, str], SymPinDef],
    list[Wire], list[Junction],
]:
    """Parse a single .kicad_sch file.

    Returns (symbols, labels, sheets, lib_symbols_pin_map, wires, junctions).

    v1.1: also extracts symbol UUIDs, units, instance_refs, sheet UUIDs +
    sheet pins, the per-sheet lib_symbols pin map, plus wires and junctions
    for the Phase 2 BFS engine.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        root = sexpdata.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Failed to parse {path}: {e}")

    symbols: list[SchSymbol] = []
    labels: list[SchLabel] = []
    sheets: list[SchSheet] = []
    wires: list[Wire] = []
    junctions: list[Junction] = []

    # v1.1: parse the per-sheet lib_symbols block once.
    lib_pin_map = parse_lib_symbols(root)

    # Walk symbol instances. Note: in KiCad ≥ 7 schematics, the structure is
    #   (symbol (lib_id "...") (at X Y ROT) (mirror ...) (property "Reference" "U1" ...) ...)
    # We avoid `(lib_symbols ...)` (the symbol cache) by only walking
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

        # v1.1: symbol UUID, unit, instance_refs.
        sym_uuid = _extract_uuid(sym)
        unit_node = _sexp_get(sym, "unit")
        unit = 1
        if unit_node and len(unit_node) >= 2:
            try:
                unit = int(_sexp_str(unit_node[1]))
            except (ValueError, TypeError):
                unit = 1
        instance_refs = _extract_instance_refs(sym)

        symbols.append(SchSymbol(
            refdes=refdes,
            lib_id=lib_id,
            sheet=sheet_name,
            pos=(x, y),
            rotation=rot,
            mirror=mirror,
            properties=properties,
            uuid=sym_uuid,
            unit=unit,
            instance_refs=instance_refs,
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
            # v1.1: extract sheet UUID + position + sheet pins.
            sh_uuid = _extract_uuid(sh)
            sh_x, sh_y, _ = _at_xy(sh)
            sheet_pins: list[SheetPin] = []
            for child in sh:
                if not (isinstance(child, list) and child
                        and isinstance(child[0], sexpdata.Symbol)
                        and str(child[0]) == "pin"):
                    continue
                if len(child) < 3:
                    continue
                pin_name = _sexp_str(child[1])
                shape = _sexp_str(child[2]) if isinstance(child[2], sexpdata.Symbol) else "passive"
                px, py, _ = _at_xy(child)
                sheet_pins.append(SheetPin(
                    name=pin_name, shape=shape, pos=(px, py),
                ))
            sheets.append(SchSheet(
                name=sheet_name_val or sheet_file,
                file=sheet_file,
                parent=sheet_name,
                uuid=sh_uuid,
                at_pos=(sh_x, sh_y),
                pins=sheet_pins,
            ))

    # v1.1 Phase 2: wires + junctions for the BFS engine.
    # (wire (pts (xy X1 Y1) (xy X2 Y2)) ...) — segments are 2-point in KiCad ≥ 7.
    for w in _sexp_walk(root, "wire"):
        pts = _sexp_get(w, "pts")
        if not pts:
            continue
        coords: list[tuple[float, float]] = []
        for child in pts[1:]:
            if (isinstance(child, list) and child
                    and isinstance(child[0], sexpdata.Symbol)
                    and str(child[0]) == "xy"
                    and len(child) >= 3):
                try:
                    coords.append((float(child[1]), float(child[2])))
                except (ValueError, TypeError):
                    continue
        # Emit one Wire per consecutive pair of points (2 points = 1 segment;
        # KiCad ≥ 7 always uses 2-point wires but we tolerate longer pts lists).
        for i in range(len(coords) - 1):
            wires.append(Wire(start=coords[i], end=coords[i + 1]))

    # (junction (at X Y) (diameter D) ...)
    for j in _sexp_walk(root, "junction"):
        if not _sexp_get(j, "at"):
            continue
        x, y, _ = _at_xy(j)
        junctions.append(Junction(pos=(x, y)))

    return symbols, labels, sheets, lib_pin_map, wires, junctions


def walk_schematic_tree(root_sch: Path) -> tuple[
    dict[str, dict],
    dict[tuple[str, int, str], SymPinDef],
    list[SchematicInstance],
    dict[str, dict],
]:
    """Walk the schematic hierarchy starting at root_sch.

    Returns (file_keyed_schematic, merged_lib_pin_map, schematic_instances, sheet_geometry):
      file_keyed_schematic: dict mapping sheet rel-path → {symbols, labels, sheets}.
        Backward-compat with v1's shape; symbols entries now include uuid /
        unit / instance_refs fields per v1.1 dataclass changes.
      merged_lib_pin_map: union of every per-sheet lib_pin_map keyed by
        (lib_id, unit, pin_number).
      schematic_instances: flat list of resolved (instance_path, refdes) pairs.
      sheet_geometry: dict per-sheet → {wires, junctions, symbols} carrying the
        raw dataclass instances (NOT asdicted) — used by the Phase 2 BFS to
        avoid re-parsing the schematic.
    """
    base = root_sch.parent
    result: dict[str, dict] = {}
    merged_pin_map: dict[tuple[str, int, str], SymPinDef] = {}
    schematic_instances: list[SchematicInstance] = []
    sheet_geometry: dict[str, dict] = {}
    pending: list[tuple[Path, str]] = [(root_sch, root_sch.name)]
    visited: set[str] = set()
    while pending:
        path, name = pending.pop()
        rel = str(path.relative_to(base))
        if rel in visited:
            continue
        visited.add(rel)
        try:
            syms, lbls, sheets, lib_pin_map, wires, junctions = parse_schematic(
                path, rel,
            )
        except Exception as e:
            print(f"WARN: failed parsing {rel}: {e}", file=sys.stderr)
            continue
        sheet_geometry[rel] = {
            "wires": wires,
            "junctions": junctions,
            "symbols": syms,
            "labels": lbls,
            "sheets": sheets,
        }
        # Merge per-sheet lib_pin_map; later entries don't overwrite earlier
        # (the same lib_id resolves consistently across sheets in practice).
        for k, v in lib_pin_map.items():
            merged_pin_map.setdefault(k, v)
        # Emit one SchematicInstance per (symbol, instance_ref).
        for sym in syms:
            if sym.instance_refs:
                for iref in sym.instance_refs:
                    schematic_instances.append(SchematicInstance(
                        instance_path=iref.path,
                        file=rel,
                        refdes=iref.reference,
                        lib_id=sym.lib_id,
                        unit=iref.unit,
                        symbol_uuid=sym.uuid,
                    ))
            else:
                # Fallback: no instances block — emit single pseudo-instance.
                schematic_instances.append(SchematicInstance(
                    instance_path=f"/<no-instance>/{sym.uuid or sym.refdes}",
                    file=rel,
                    refdes=sym.refdes,
                    lib_id=sym.lib_id,
                    unit=sym.unit,
                    symbol_uuid=sym.uuid,
                ))
        result[rel] = {
            "symbols": [asdict(s) for s in syms],
            "labels": [asdict(l) for l in lbls],
            "sheets": [asdict(s) for s in sheets],
        }
        for sh in sheets:
            sub = base / sh.file
            if sub.exists() and str(sub.relative_to(base)) not in visited:
                pending.append((sub, sh.file))
    return result, merged_pin_map, schematic_instances, sheet_geometry


# ---------------------------------------------------------------------------
# Wire-trace BFS engine (Phase 2 — _experimental)
# ---------------------------------------------------------------------------
#
# Single-sheet BFS through wires + junctions, with optional transit through
# tightly-defaulted 2-pin passives (resistors / ferrites only by default).
#
# v1.1 limitations explicitly documented in --help:
#   - Single-sheet only (no cross-sheet hierarchical labels via sheet pins)
#   - No bus alias expansion
#   - No active-component signal flow (BFS terminates at active IC pins)
#   - Refuses paths through GND/power terminals
#   - Output is _experimental.wire_traces[] — shape may change in v1.2

# Coord-key precision: KiCad rounds positions to 0.0001 mm internally.
# Normalizing to 4 decimal places gives stable graph keys without losing
# precision. (Codex catch: floats as graph keys cause subtle mismatches.)
COORD_PRECISION = 4

# Default refdes prefixes whose 2-pin instances are transit-able. Codex catch:
# capacitors NOT transit-able by default (decoupling, RC filter, shunt all
# produce wrong paths). User can override via --transit-prefix.
DEFAULT_TRANSIT_PREFIXES = ("R", "FB")

# Net names (or label names) that signal "this is a power/ground rail; don't
# trace through". BFS terminates at these without traversing further.
POWER_NET_PATTERNS = (
    re.compile(r"^GND$"),
    re.compile(r"^GNDA$"),
    re.compile(r"^\+?\d+(\.\d+)?V\d*$"),  # +3V3, +5V, -15V, +1V1, etc.
    re.compile(r"^V(IN|BUS|DD|CC|SS|EE)$"),
    re.compile(r"^Earth$|^EARTH$|^PE$"),
)


def _norm_coord(p: tuple[float, float]) -> tuple[str, str]:
    """Normalize a (x, y) pair to fixed-precision string keys for graph hashing."""
    return (f"{p[0]:.{COORD_PRECISION}f}", f"{p[1]:.{COORD_PRECISION}f}")


def _is_power_net(name: str) -> bool:
    """True if a label/net name looks like a power or ground rail."""
    s = name.strip()
    return any(rx.match(s) for rx in POWER_NET_PATTERNS)


def _on_segment(p: tuple[float, float], a: tuple[float, float],
                b: tuple[float, float], tol: float = 1e-3) -> bool:
    """True if point p is on segment a-b (collinear AND between endpoints)."""
    # Reject if p coincides with either endpoint — endpoints are already nodes.
    if (abs(p[0] - a[0]) < tol and abs(p[1] - a[1]) < tol):
        return False
    if (abs(p[0] - b[0]) < tol and abs(p[1] - b[1]) < tol):
        return False
    # Cross product == 0 → collinear; bounding box check confirms between.
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > tol:
        return False
    minx, maxx = sorted((a[0], b[0]))
    miny, maxy = sorted((a[1], b[1]))
    return minx - tol <= p[0] <= maxx + tol and miny - tol <= p[1] <= maxy + tol


def _split_wires_at_contacts(
    wires: list[Wire],
    contact_points: list[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Codex catch: KiCad doesn't always emit explicit junctions for T-junctions
    where a label / pin / wire endpoint lies on the midpoint of another wire.

    For each wire segment, find any contact_points on its midpoint and split
    the segment so each contact gets its own graph node. Returns a list of
    (start, end) tuples representing the post-split wire segments.
    """
    out: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for w in wires:
        # Find every contact strictly between start and end.
        midpoints = [p for p in contact_points if _on_segment(p, w.start, w.end)]
        if not midpoints:
            out.append((w.start, w.end))
            continue
        # Sort midpoints along the segment so we emit contiguous sub-segments.
        # Use distance-from-start as the sort key.
        def dist_from_start(p: tuple[float, float]) -> float:
            return ((p[0] - w.start[0]) ** 2 + (p[1] - w.start[1]) ** 2) ** 0.5
        sorted_mids = sorted(midpoints, key=dist_from_start)
        # Emit start → mid_1 → mid_2 → ... → end.
        prev = w.start
        for m in sorted_mids:
            out.append((prev, m))
            prev = m
        out.append((prev, w.end))
    return out


def build_sheet_graph(
    sheet_geom: dict,
    pin_maps: dict[tuple[str, int, str], SymPinDef],
    instance_units_for_refdes: dict[str, list[int]],
) -> tuple[
    dict[tuple[str, str], list[tuple[str, str]]],   # adjacency: norm_coord -> [norm_coord]
    dict[tuple[str, str], list[dict]],              # tags: norm_coord -> [{kind, ...}]
]:
    """Build a single-sheet wire/junction/label graph for BFS.

    Returns (adjacency, tags):
      adjacency: dict from coord-key → list of neighbor coord-keys.
      tags: dict from coord-key → list of {kind, ...} dicts describing what
        lives at that coord (component pin, label, junction, sheet pin).
    """
    syms: list[SchSymbol] = sheet_geom["symbols"]
    labels: list[SchLabel] = sheet_geom["labels"]
    sheets: list[SchSheet] = sheet_geom["sheets"]
    wires: list[Wire] = sheet_geom["wires"]
    junctions: list[Junction] = sheet_geom["junctions"]

    tags: dict[tuple[str, str], list[dict]] = defaultdict(list)
    contact_points: list[tuple[float, float]] = []

    # Tag component pins (per-instance refdes; we use sym.refdes as the
    # default since instance walker resolves it for each sheet).
    for sym in syms:
        units = instance_units_for_refdes.get(sym.refdes, [sym.unit, 1, 0])
        # For each pin in the symbol's lib_symbols entry, compute absolute pos.
        for u in units:
            for (lib_id, unit, pin_num), pin_def in pin_maps.items():
                if lib_id != sym.lib_id and not (
                    sym.lib_id.endswith(":" + lib_id)
                    or lib_id.endswith(":" + sym.lib_id)
                ):
                    continue
                if unit != u:
                    continue
                abs_pos = absolute_pin_position(
                    sym.pos, sym.rotation, sym.mirror, pin_def.pin_offset,
                )
                key = _norm_coord(abs_pos)
                tags[key].append({
                    "kind": "pin",
                    "refdes": sym.refdes,
                    "pin_number": pin_num,
                    "pin_name": pin_def.primary_name,
                    "lib_id": sym.lib_id,
                    "abs_pos": abs_pos,
                })
                contact_points.append(abs_pos)

    # Tag labels.
    for lbl in labels:
        key = _norm_coord(lbl.pos)
        tags[key].append({
            "kind": "label", "name": lbl.name, "label_kind": lbl.kind,
            "abs_pos": lbl.pos,
        })
        contact_points.append(lbl.pos)

    # Tag junctions.
    for j in junctions:
        key = _norm_coord(j.pos)
        tags[key].append({"kind": "junction", "abs_pos": j.pos})
        contact_points.append(j.pos)

    # Tag sheet pins.
    for sh in sheets:
        for sp in sh.pins:
            key = _norm_coord(sp.pos)
            tags[key].append({
                "kind": "sheet_pin",
                "name": sp.name,
                "shape": sp.shape,
                "sheet_file": sh.file,
                "abs_pos": sp.pos,
            })
            contact_points.append(sp.pos)

    # Split wires at midpoint contacts before building adjacency.
    wire_segments = _split_wires_at_contacts(wires, contact_points)

    adjacency: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for start, end in wire_segments:
        ks, ke = _norm_coord(start), _norm_coord(end)
        if ks == ke:
            continue
        adjacency[ks].append(ke)
        adjacency[ke].append(ks)

    return dict(adjacency), dict(tags)


def find_pin_position_for_refdes(
    refdes: str, pin_number: str,
    syms: list[SchSymbol],
    pin_maps: dict[tuple[str, int, str], SymPinDef],
    instance_units_for_refdes: dict[str, list[int]],
) -> tuple[float, float] | None:
    """Look up the absolute coordinate of a (refdes, pin) on its sheet."""
    for sym in syms:
        if sym.refdes != refdes:
            continue
        units = instance_units_for_refdes.get(refdes, [sym.unit, 1, 0])
        for u in units:
            pin_def = lookup_pin_name(pin_maps, sym.lib_id, u, pin_number)
            if pin_def is not None:
                return absolute_pin_position(
                    sym.pos, sym.rotation, sym.mirror, pin_def.pin_offset,
                )
    return None


def bfs_trace(
    start_pos: tuple[float, float],
    start_refdes: str,
    start_pin: str,
    adjacency: dict[tuple[str, str], list[tuple[str, str]]],
    tags: dict[tuple[str, str], list[dict]],
    sheet_name: str,
    bom_by_refdes: dict[str, BOMEntry],
    transit_prefixes: tuple[str, ...] = DEFAULT_TRANSIT_PREFIXES,
    max_steps: int = 5000,
) -> WireTrace:
    """Single-sheet BFS from a pin coord to nearest labels / other-component-pins.

    Stop conditions (per branch):
      - Hit a label → record as endpoint
      - Hit a power label (matched by POWER_NET_PATTERNS) → record + stop
      - Hit another component pin (passive transit allowed only for refdes
        prefixes in transit_prefixes; non-passive pins terminate the branch)
      - Hit a sheet pin → record (as a "boundary"; cross-sheet not in v1.1)
      - Step budget exhausted → emit "boundary" with note
    """
    start_key = _norm_coord(start_pos)
    if start_key not in adjacency and start_key not in tags:
        # Pin coord doesn't appear in the graph — likely the wire endpoints
        # don't exactly hit the pin. Try a small search radius. v1.1: just
        # report unconnected.
        return WireTrace(
            source={"refdes": start_refdes, "pin": start_pin,
                    "sheet": sheet_name, "position": list(start_pos)},
            path=[],
            endpoints=[WireTraceEndpoint(
                kind="boundary", value="(no wire at pin)",
                position=list(start_pos), sheet=sheet_name,
            )],
            confidence="single-sheet",
            notes=["pin coord not in wire graph; pin may be unconnected or "
                   "wire endpoints don't match (check transform math)"],
        )

    visited: set[tuple[str, str]] = {start_key}
    endpoints: list[WireTraceEndpoint] = []
    path_steps: list[WireTraceStep] = []
    queue: list[tuple[tuple[str, str], tuple[float, float]]] = [(start_key, start_pos)]
    steps = 0

    while queue and steps < max_steps:
        key, pos = queue.pop(0)
        steps += 1

        # Check what's at this coord — labels, other pins, junctions, sheet pins.
        node_tags = tags.get(key, [])
        terminate_branch = False
        for t in node_tags:
            kind = t["kind"]
            if kind == "label":
                # Power-rail labels stop the branch.
                if _is_power_net(t["name"]):
                    endpoints.append(WireTraceEndpoint(
                        kind="label", value=t["name"],
                        position=list(t["abs_pos"]), sheet=sheet_name,
                        label_kind=t["label_kind"],
                    ))
                    terminate_branch = True
                else:
                    # Don't terminate — labels just annotate; keep walking
                    # to discover all destinations on this net.
                    endpoints.append(WireTraceEndpoint(
                        kind="label", value=t["name"],
                        position=list(t["abs_pos"]), sheet=sheet_name,
                        label_kind=t["label_kind"],
                    ))
            elif kind == "pin":
                if t["refdes"] == start_refdes and t["pin_number"] == start_pin:
                    # Don't treat the source pin as an endpoint.
                    continue
                # Check whether this is a transit-allowed passive.
                refdes_prefix = re.match(r"^([A-Za-z]+)", t["refdes"])
                prefix_str = refdes_prefix.group(1) if refdes_prefix else ""
                bom_entry = bom_by_refdes.get(t["refdes"])
                # Transit only for 2-pin components with allowed prefix.
                # We approximate "2-pin" via prefix + lookup count of pins for
                # this refdes; lean v1.1 just trusts the prefix.
                is_transit_allowed = prefix_str in transit_prefixes
                if is_transit_allowed:
                    # Transit: find the OTHER pin of this refdes and add it
                    # to the queue (if we haven't visited it).
                    other_pin_keys = [
                        (k, t2) for k, lst in tags.items() for t2 in lst
                        if t2.get("kind") == "pin"
                        and t2.get("refdes") == t["refdes"]
                        and t2.get("pin_number") != t["pin_number"]
                    ]
                    value_str = bom_entry.raw.get("value", "?") if bom_entry else "?"
                    path_steps.append(WireTraceStep(
                        kind="transit",
                        refdes=t["refdes"],
                        value=value_str,
                        transit_via=f"{t['refdes']}:{t['pin_number']}",
                        position=list(t["abs_pos"]),
                    ))
                    for ok, ot in other_pin_keys:
                        if ok not in visited:
                            visited.add(ok)
                            queue.append((ok, ot["abs_pos"]))
                else:
                    # Active-component pin: BFS terminates here.
                    endpoints.append(WireTraceEndpoint(
                        kind="pin",
                        value=f"{t['refdes']}:{t['pin_number']} ({t['pin_name']})",
                        position=list(t["abs_pos"]), sheet=sheet_name,
                    ))
                    terminate_branch = True
            elif kind == "sheet_pin":
                endpoints.append(WireTraceEndpoint(
                    kind="boundary",
                    value=f"sheet-pin {t['name']} → {t['sheet_file']}",
                    position=list(t["abs_pos"]), sheet=sheet_name,
                ))
                terminate_branch = True
            elif kind == "junction":
                path_steps.append(WireTraceStep(
                    kind="junction", position=list(t["abs_pos"]),
                ))

        if terminate_branch:
            continue

        # Walk wire neighbors.
        for nkey in adjacency.get(key, []):
            if nkey in visited:
                continue
            visited.add(nkey)
            # Find the nominal coord of nkey from any tag at nkey.
            n_pos = None
            for t in tags.get(nkey, []):
                n_pos = t.get("abs_pos")
                break
            if n_pos is None:
                # Coord might be a pure wire endpoint (no tag).
                try:
                    n_pos = (float(nkey[0]), float(nkey[1]))
                except (ValueError, TypeError):
                    n_pos = (0.0, 0.0)
            path_steps.append(WireTraceStep(
                kind="wire",
                position=list(pos),
                end_position=list(n_pos),
            ))
            queue.append((nkey, n_pos))

    if not endpoints and steps >= max_steps:
        endpoints.append(WireTraceEndpoint(
            kind="boundary", value="(step budget exhausted)",
            sheet=sheet_name,
        ))
    elif not endpoints:
        endpoints.append(WireTraceEndpoint(
            kind="boundary", value="(unconnected)",
            sheet=sheet_name,
        ))

    return WireTrace(
        source={"refdes": start_refdes, "pin": start_pin,
                "sheet": sheet_name, "position": list(start_pos)},
        path=path_steps,
        endpoints=endpoints,
        confidence="single-sheet",
    )


def parse_trace_pin_arg(arg: str | None) -> list[tuple[str, str]]:
    """Parse `--trace-pin REF:PIN[,REF:PIN]...` into a list of (refdes, pin)."""
    if not arg:
        return []
    out: list[tuple[str, str]] = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        ref, pin = tok.split(":", 1)
        out.append((ref.strip(), pin.strip()))
    return out


def run_traces(
    trace_pins: list[tuple[str, str]],
    sheet_geometry: dict[str, dict],
    schematic_instances: list[SchematicInstance],
    pin_maps: dict[tuple[str, int, str], SymPinDef],
    bom: list[BOMEntry],
    transit_prefixes: tuple[str, ...] = DEFAULT_TRANSIT_PREFIXES,
) -> list[WireTrace]:
    """Run BFS for each --trace-pin entry. Each trace is single-sheet:
    we locate which sheet the refdes lives in via schematic_instances, then
    BFS in that sheet's graph.
    """
    if not trace_pins:
        return []

    # Map refdes → list of (sheet_file, instance_unit) we've seen.
    refdes_to_sheets: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for inst in schematic_instances:
        refdes_to_sheets[inst.refdes].append((inst.file, inst.unit))

    # Per-refdes set of units (for pin-name resolution fallback).
    instance_units: dict[str, list[int]] = {}
    for inst in schematic_instances:
        instance_units.setdefault(inst.refdes, [])
        if inst.unit not in instance_units[inst.refdes]:
            instance_units[inst.refdes].append(inst.unit)

    bom_by_refdes = {b.refdes: b for b in bom}

    # Cache built graphs per sheet (BFS is repeated work otherwise).
    graph_cache: dict[str, tuple[dict, dict]] = {}

    traces: list[WireTrace] = []
    for ref, pin in trace_pins:
        sheets = refdes_to_sheets.get(ref, [])
        if not sheets:
            traces.append(WireTrace(
                source={"refdes": ref, "pin": pin, "sheet": None, "position": None},
                path=[],
                endpoints=[WireTraceEndpoint(
                    kind="boundary",
                    value=f"refdes {ref} not in schematic_instances",
                )],
                confidence="single-sheet",
                notes=["refdes not found"],
            ))
            continue
        # Use first sheet (lean v1.1; multi-instance refdeses just take first).
        sheet_file, _ = sheets[0]
        if sheet_file not in sheet_geometry:
            traces.append(WireTrace(
                source={"refdes": ref, "pin": pin, "sheet": sheet_file, "position": None},
                path=[],
                endpoints=[WireTraceEndpoint(
                    kind="boundary",
                    value=f"sheet {sheet_file} not in geometry cache",
                )],
                confidence="single-sheet",
            ))
            continue

        sheet_geom = sheet_geometry[sheet_file]

        if sheet_file not in graph_cache:
            graph_cache[sheet_file] = build_sheet_graph(
                sheet_geom, pin_maps, instance_units,
            )
        adjacency, tags = graph_cache[sheet_file]

        start_pos = find_pin_position_for_refdes(
            ref, pin, sheet_geom["symbols"], pin_maps, instance_units,
        )
        if start_pos is None:
            traces.append(WireTrace(
                source={"refdes": ref, "pin": pin, "sheet": sheet_file, "position": None},
                path=[],
                endpoints=[WireTraceEndpoint(
                    kind="boundary",
                    value="pin coordinate could not be resolved",
                )],
                confidence="single-sheet",
                notes=["lib_symbols missing pin offset, or symbol-instance "
                       "rotation/mirror produced unexpected coords"],
            ))
            continue

        traces.append(bfs_trace(
            start_pos=start_pos,
            start_refdes=ref,
            start_pin=pin,
            adjacency=adjacency,
            tags=tags,
            sheet_name=sheet_file,
            bom_by_refdes=bom_by_refdes,
            transit_prefixes=transit_prefixes,
        ))
    return traces


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
    schematic_instances: list[SchematicInstance] | None = None,
    pin_maps: dict[tuple[str, int, str], SymPinDef] | None = None,
) -> list[Fact]:
    """Cross-reference BOM × netlist × schematic into structured facts.

    v1.1: when schematic_instances and pin_maps are provided, also resolves
    `symbol_pin_name` + `pin_alternates` per fact and sets
    `instance_confirmed = True` when the refdes is found in any
    schematic_instance.

    Confidence string format remains v1-compatible (`netlist`, `netlist+bom`,
    `netlist+bom+schematic`) so existing consumers don't break. Instance-walk
    resolution surfaces via the new `instance_confirmed` boolean field.
    """
    bom_by_refdes = {e.refdes: e for e in bom}

    # Group netlist by refdes for fast lookup.
    netlist_by_refdes: dict[str, list[NetlistEntry]] = defaultdict(list)
    for e in netlist:
        netlist_by_refdes[e.refdes].append(e)

    # Build a set of refdeses present in the schematic (file-level walk).
    schematic_refdeses: set[str] = set()
    for sheet in schematics.values():
        for s in sheet["symbols"]:
            schematic_refdeses.add(s["refdes"])

    # v1.1: build per-refdes lib_id and the *set* of units seen across all
    # instances of that refdes. Multi-unit symbols (e.g. dual op-amps with
    # U83 unit A and U83 unit B) need the lookup to try every unit since
    # different pins live on different units.
    instance_refdes_libid: dict[str, str] = {}
    instance_refdes_units: dict[str, set[int]] = defaultdict(set)
    instance_refdes_set: set[str] = set()
    if schematic_instances:
        for inst in schematic_instances:
            instance_refdes_set.add(inst.refdes)
            instance_refdes_libid.setdefault(inst.refdes, inst.lib_id)
            instance_refdes_units[inst.refdes].add(inst.unit)

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

        instance_confirmed = refdes in instance_refdes_set
        lib_id = instance_refdes_libid.get(refdes)
        # Try units in the order: smallest first, so unit 1 → unit 2 → ...
        # plus 0 (shared across units). lookup_pin_name's internal fallbacks
        # cover the rest.
        units_to_try = sorted(instance_refdes_units.get(refdes, {1})) or [1]

        for ne in entries:
            # v1.1: resolve symbol_pin_name + alternates if we have a lib_id.
            symbol_pin_name = None
            pin_alternates: list[str] = []
            if pin_maps and lib_id is not None:
                for u in units_to_try:
                    pin_def = lookup_pin_name(pin_maps, lib_id, u, ne.pin)
                    if pin_def is not None:
                        symbol_pin_name = pin_def.primary_name
                        pin_alternates = list(pin_def.alternates)
                        break

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
                symbol_pin_name=symbol_pin_name,
                pin_alternates=pin_alternates,
                instance_confirmed=instance_confirmed,
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
    # Phase 2 — wire-trace BFS prototype (experimental).
    ap.add_argument(
        "--trace-pin", default=None,
        help=(
            "Comma-separated list of REF:PIN sources to wire-trace via "
            "single-sheet BFS. Output goes to _experimental.wire_traces[]. "
            "Example: --trace-pin U2:44,U1:36. v1.1 limits: single-sheet "
            "only; no cross-sheet labels; no bus expansion; resistors+ferrites "
            "passive transit only by default; refuses paths through "
            "GND/power; terminates at active-IC pins."
        ),
    )
    ap.add_argument(
        "--transit-prefix", default=",".join(DEFAULT_TRANSIT_PREFIXES),
        help=(
            f"Comma-separated refdes prefixes whose 2-pin instances are "
            f"transit-able by the BFS. Default: {','.join(DEFAULT_TRANSIT_PREFIXES)} "
            f"(resistors + ferrites). Capacitors are deliberately NOT "
            f"transit-able by default (decoupling/filter false-positive risk)."
        ),
    )
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

    # ---- Schematic walk (v1.1: also returns lib_pin_map + instances + geometry) ----
    root_sch_rel = find_root_schematic(file_paths)
    schematics: dict[str, dict] = {}
    pin_maps: dict[tuple[str, int, str], SymPinDef] = {}
    schematic_instances: list[SchematicInstance] = []
    sheet_geometry: dict[str, dict] = {}
    if root_sch_rel:
        root_sch = project_root / root_sch_rel
        if root_sch.exists():
            (schematics, pin_maps, schematic_instances,
             sheet_geometry) = walk_schematic_tree(root_sch)

    # ---- Project-local .kicad_sym fallback (v1.1) ----
    # If schematic instances reference lib_ids that aren't in the embedded
    # lib_symbols cache, scan project-local .kicad_sym files and merge their
    # pin definitions in.
    embedded_lib_ids = {k[0] for k in pin_maps.keys()}
    referenced_lib_ids = {inst.lib_id for inst in schematic_instances}
    unresolved_lib_ids = referenced_lib_ids - embedded_lib_ids
    # Also try bare-name match (lib_id "panel_custom:Foo" → bare "Foo").
    bare_resolved = {lib_id.split(":", 1)[-1] for lib_id in embedded_lib_ids}
    truly_unresolved = {
        lib_id for lib_id in unresolved_lib_ids
        if lib_id.split(":", 1)[-1] not in bare_resolved
    }
    if truly_unresolved:
        for f in file_paths:
            if not f.endswith(".kicad_sym"):
                continue
            sym_full = project_root / f
            if not sym_full.exists():
                continue
            local_pins = parse_kicad_sym_file(sym_full)
            for k, v in local_pins.items():
                pin_maps.setdefault(k, v)

    # ---- Facts (v1.1: pass pin_maps + schematic_instances) ----
    facts = build_facts(bom, netlist, schematics, schematic_instances, pin_maps)

    # ---- Phase 2 wire-trace BFS (experimental, behind --trace-pin) ----
    trace_pins = parse_trace_pin_arg(args.trace_pin)
    transit_prefixes = tuple(
        s.strip() for s in args.transit_prefix.split(",") if s.strip()
    ) or DEFAULT_TRANSIT_PREFIXES
    wire_traces: list[WireTrace] = []
    if trace_pins:
        wire_traces = run_traces(
            trace_pins=trace_pins,
            sheet_geometry=sheet_geometry,
            schematic_instances=schematic_instances,
            pin_maps=pin_maps,
            bom=bom,
            transit_prefixes=transit_prefixes,
        )

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
        "schematic_instances": [asdict(i) for i in schematic_instances],
        "facts": [asdict(f) for f in facts],
        "doc_quality": [asdict(f) for f in findings],
        "open_questions": [asdict(q) for q in questions],
        # _experimental: shape may change in v1.2. Currently single-sheet
        # only; populated only when --trace-pin is passed.
        "_experimental": {
            "wire_traces": [asdict(t) for t in wire_traces],
            "_notes": (
                "Phase 2 BFS prototype. Single-sheet only; no cross-sheet "
                "label propagation; no bus alias expansion; resistors+ferrites "
                "passive-transit only by default; refuses paths through "
                "GND/power rails; terminates at active-IC pins. Field shape "
                "may change in v1.2."
            ),
        },
        "stats": {
            "n_files": len(file_inventory),
            "n_bom": len(bom),
            "n_netlist": len(netlist),
            "n_positions": len(positions),
            "n_schematic_sheets": len(schematics),
            "n_facts": len(facts),
            "n_doc_quality": len(findings),
            "n_kicad_sch_files": len(sch_versions),
            "n_lib_pin_defs": len(pin_maps),
            "n_schematic_instances": len(schematic_instances),
            "n_facts_with_pin_name": sum(
                1 for f in facts if f.symbol_pin_name is not None
            ),
            "n_facts_instance_confirmed": sum(
                1 for f in facts if f.instance_confirmed
            ),
            "n_wire_traces": len(wire_traces),
        },
    }

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
