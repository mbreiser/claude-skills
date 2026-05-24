#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=4.0.0"]
# ///
"""
pdf-sidecar: extract a PDF into a cached sidecar directory of markdown + images.

The point: Claude reads `document.md` from the sidecar instead of paying the
token cost of re-reading the source PDF on every session.

Inputs
------
    pdf-sidecar.py <pdf-path> [--high-fidelity] [--text-only]
                              [--force] [--out DIR] [--quiet]

Outputs
-------
Sidecar directory at `<pdf>.extracted/` (or `~/.cache/pdf-sidecar/<sha256>/`
if the PDF's parent dir isn't writable, or `--out DIR` if passed):

    paper.pdf.extracted/
    ├── document.md      # full markdown, single file, page boundaries
    │                    #   marked as <!-- page N --> comments
    ├── metadata.json    # source sha256, backend, version, stats, verification
    ├── images/          # extracted figures (skipped in --text-only mode)
    │   ├── figure-001.png
    │   └── ...
    └── tables/          # extracted tables (skipped in --text-only mode)
        ├── table-001.md
        └── ...

A single JSON line is printed to stdout for the caller to parse:

    {"status": "cached"|"extracted", "sidecar": "...",
     "document_md": "...", "stats": {...}, "verification": {...}}

Exit codes
----------
    0  success (cached or freshly extracted)
    2  source PDF missing or unreadable
    3  backend crashed during extraction
    4  scanned PDF — no extractable text layer (no OCR in v1)
    5  argument error
    6  verification failed — sidecar NOT committed

Backends
--------
Default backend is **Marker** (MIT, fast, light). Pass `--high-fidelity` to use
**Docling** (Apache 2.0, better tables, ~2 GB of torch on first invocation).

This script declares only `pypdf` in its PEP 723 header (used for the cheap
baseline-pass that detects scanned PDFs and seeds verification). The heavier
backend deps are NOT in the header — invoke with `uv run --script` and an
explicit `--with`:

    uv run --script --with marker-pdf  bin/pdf-sidecar.py paper.pdf
    uv run --script --with docling     bin/pdf-sidecar.py paper.pdf --high-fidelity

A missing backend produces a clear error pointing the user at the right
`--with` invocation.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pypdf

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

TOOL_VERSION = "0.1.0"

# Bump when the sidecar layout or metadata.json shape changes in a way
# consumers can detect. Cache hits require the cached schema_version to match
# this; mismatches force re-extraction.
SCHEMA_VERSION = "0.1.0"

# Default verification thresholds (overridable via env vars).
DEFAULT_WORD_RATIO_OK = 0.85
DEFAULT_WORD_RATIO_FAIL = 0.50
DEFAULT_IMAGE_RATIO_OK = 0.70

# Below this many extracted characters across the entire PDF we assume the
# document has no text layer (scanned). Conservative — a one-page paper
# typically has >1000 chars.
SCANNED_TEXT_THRESHOLD_CHARS = 50

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Baseline:
    """Result of the cheap pre-extraction pass."""

    pages: int
    chars: int
    expected_words: int
    expected_images: int
    is_scanned: bool


@dataclass
class BackendResult:
    """What a backend returns. All paths are relative inside the sidecar."""

    document_md: str               # full markdown text (no page filename prefix)
    images: list[tuple[str, bytes]]    # [(filename, png_bytes), ...]
    tables_md: list[tuple[str, str]]   # [(filename, markdown_str), ...]
    backend_name: str
    backend_version: str


@dataclass
class Verification:
    expected_words: int
    actual_words: int
    word_ratio: float
    expected_images: int
    actual_images: int
    status: str            # "ok" | "warning" | "fail"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Errors with exit codes
# ---------------------------------------------------------------------------


class PdfSidecarError(Exception):
    exit_code: int = 1

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class SourceUnreadable(PdfSidecarError):
    exit_code = 2


class BackendFailure(PdfSidecarError):
    exit_code = 3


class ScannedPdf(PdfSidecarError):
    exit_code = 4


class ArgError(PdfSidecarError):
    exit_code = 5


class VerificationFailure(PdfSidecarError):
    exit_code = 6


# ---------------------------------------------------------------------------
# Hashing + paths
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def choose_sidecar_dir(pdf_path: Path, out: Path | None, sha256: str) -> Path:
    """Pick where the sidecar lives.

    Priority:
      1. --out DIR if passed
      2. <pdf>.extracted/ next to the PDF, if that directory is writable
      3. ~/.cache/pdf-sidecar/<sha256>/ as a fallback
    """
    if out is not None:
        return out.resolve()

    parent = pdf_path.parent
    if os.access(parent, os.W_OK):
        return (parent / (pdf_path.name + ".extracted")).resolve()

    return (Path.home() / ".cache" / "pdf-sidecar" / sha256).resolve()


# ---------------------------------------------------------------------------
# Baseline pass (cheap, always runs unless cache hits)
# ---------------------------------------------------------------------------


def baseline_pass(pdf_path: Path) -> Baseline:
    """Cheap pre-extraction pass over the source PDF.

    Uses pypdf — no heavy deps. Counts words from `page.extract_text()` and
    image XObjects from `page.images`. The numbers are *floors*: backends
    almost always recover at least this much.

    Also detects scanned PDFs (no text layer) so we can fail fast.
    """
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as exc:
        raise SourceUnreadable(
            f"pypdf could not open {pdf_path}: {exc}"
        ) from exc

    if getattr(reader, "is_encrypted", False):
        raise SourceUnreadable(
            f"{pdf_path} is encrypted; decrypt with the `pdf` skill first."
        )

    total_chars = 0
    total_words = 0
    total_images = 0
    pages = len(reader.pages)

    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        total_chars += len(text)
        total_words += count_words(text)
        try:
            total_images += len(list(page.images))
        except Exception:
            # Some PDFs raise on .images for malformed image XObjects;
            # don't fail the baseline — backends will catch real issues.
            pass

    is_scanned = total_chars < SCANNED_TEXT_THRESHOLD_CHARS and pages > 0

    return Baseline(
        pages=pages,
        chars=total_chars,
        expected_words=total_words,
        expected_images=total_images,
        is_scanned=is_scanned,
    )


_WORD_RE = re.compile(r"\w", re.UNICODE)


def count_words(text: str) -> int:
    """Count tokens containing at least one alphanumeric character.

    Filters out pure markdown punctuation (`#`, `---`, `|`, `*`, ...). Splits
    on whitespace only — does NOT try to tokenize CJK; for those the word
    count is approximate but the *ratio* (the actual signal) holds.
    """
    return sum(1 for tok in text.split() if _WORD_RE.search(tok))


# ---------------------------------------------------------------------------
# Cache check
# ---------------------------------------------------------------------------


def cache_satisfies(
    sidecar_dir: Path, *, source_sha: str, requested_mode: str
) -> tuple[bool, str | None]:
    """Decide whether the existing sidecar can serve the current request.

    Returns (satisfies, reason). `reason` is set when *not* satisfied, for
    diagnostic logging.

    Rules:
    - sidecar dir must exist with metadata.json
    - metadata.json must parse and carry a known schema_version
    - source.sha256 must match
    - a text-only cache cannot satisfy a full request (images missing)
    - a full cache CAN satisfy a text-only request (text is a subset)
    """
    md_path = sidecar_dir / "metadata.json"
    if not md_path.exists():
        return False, "no metadata.json"

    try:
        meta = json.loads(md_path.read_text())
    except Exception as exc:
        return False, f"metadata.json unparseable: {exc}"

    cached_schema = (meta.get("tool") or {}).get("schema_version")
    if cached_schema != SCHEMA_VERSION:
        return False, f"schema_version mismatch ({cached_schema!r} != {SCHEMA_VERSION!r})"

    cached_sha = (meta.get("source") or {}).get("sha256")
    if cached_sha != source_sha:
        return False, f"source sha256 mismatch"

    cached_mode = meta.get("mode")
    if cached_mode not in {"full", "text-only"}:
        return False, f"unrecognized cached mode {cached_mode!r}"

    if requested_mode == "full" and cached_mode == "text-only":
        return False, "text-only cache cannot satisfy full request"

    if not (sidecar_dir / "document.md").exists():
        return False, "document.md missing despite metadata.json present"

    return True, None


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------

Backend = Callable[[Path, str], BackendResult]
# A backend takes (pdf_path, mode) and returns a BackendResult.
# `mode` is "full" or "text-only"; backends may skip image/table extraction
# when mode == "text-only".


def extract_marker(pdf_path: Path, mode: str) -> BackendResult:
    """Marker backend (MIT). Default.

    Re-importable from tests so monkeypatching the module-level BACKENDS dict
    is enough to stub this out — these import statements aren't reached in
    tests.
    """
    try:
        from marker.converters.pdf import PdfConverter  # type: ignore
        from marker.models import create_model_dict  # type: ignore
        from marker.output import text_from_rendered  # type: ignore
        import marker as _marker  # type: ignore
    except ImportError as exc:
        raise BackendFailure(
            "marker not available. Re-invoke with:\n"
            "    uv run --script --with marker-pdf bin/pdf-sidecar.py ...\n"
            f"(import error: {exc})"
        ) from exc

    try:
        converter = PdfConverter(artifact_dict=create_model_dict())
        rendered = converter(str(pdf_path))
        text, _meta, images = text_from_rendered(rendered)
    except Exception as exc:
        raise BackendFailure(f"marker crashed on {pdf_path}: {exc}") from exc

    image_blobs: list[tuple[str, bytes]] = []
    if mode == "full" and images:
        import io

        for idx, (_name, pil_image) in enumerate(sorted(images.items()), start=1):
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            image_blobs.append((f"figure-{idx:03d}.png", buf.getvalue()))

    # Marker emits tables inline in markdown; we don't currently split them
    # into a separate tables/ dir from marker. (Docling will.)
    return BackendResult(
        document_md=text,
        images=image_blobs,
        tables_md=[],
        backend_name="marker",
        backend_version=getattr(_marker, "__version__", "unknown"),
    )


def extract_docling(pdf_path: Path, mode: str) -> BackendResult:
    """Docling backend (Apache 2.0). Opt-in via --high-fidelity.

    Heavy first-run install (~2 GB of torch). Better tables than Marker.
    """
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
        import docling as _docling  # type: ignore
    except ImportError as exc:
        raise BackendFailure(
            "docling not available. Re-invoke with:\n"
            "    uv run --script --with docling bin/pdf-sidecar.py ... --high-fidelity\n"
            f"(import error: {exc})"
        ) from exc

    try:
        converter = DocumentConverter()
        result = converter.convert(str(pdf_path))
        doc = result.document
        md = doc.export_to_markdown()
    except Exception as exc:
        raise BackendFailure(f"docling crashed on {pdf_path}: {exc}") from exc

    image_blobs: list[tuple[str, bytes]] = []
    tables: list[tuple[str, str]] = []

    if mode == "full":
        import io

        # Pictures
        for idx, picture in enumerate(getattr(doc, "pictures", []) or [], start=1):
            try:
                pil_image = picture.get_image(doc)
                if pil_image is None:
                    continue
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                image_blobs.append((f"figure-{idx:03d}.png", buf.getvalue()))
            except Exception:
                # Skip individual figure failures; backend already gave us
                # the markdown body.
                continue

        # Tables — Docling exposes table_df / export_to_markdown per table.
        for idx, table in enumerate(getattr(doc, "tables", []) or [], start=1):
            try:
                table_md = table.export_to_markdown(doc)
                tables.append((f"table-{idx:03d}.md", table_md))
            except Exception:
                continue

    return BackendResult(
        document_md=md,
        images=image_blobs,
        tables_md=tables,
        backend_name="docling",
        backend_version=getattr(_docling, "__version__", "unknown"),
    )


# Module-level dispatch table — tests monkeypatch entries here to swap in
# stubbed backends without installing torch/marker in CI.
BACKENDS: dict[str, Backend] = {
    "marker": extract_marker,
    "docling": extract_docling,
}


# ---------------------------------------------------------------------------
# Writing the sidecar
# ---------------------------------------------------------------------------


def write_sidecar(
    staging: Path,
    *,
    result: BackendResult,
    mode: str,
) -> tuple[int, int]:
    """Write BackendResult into a staging directory.

    Returns (actual_words, actual_images). `staging` should be empty.
    """
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "document.md").write_text(result.document_md, encoding="utf-8")

    actual_images = 0
    if mode == "full" and result.images:
        images_dir = staging / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for filename, blob in result.images:
            (images_dir / filename).write_bytes(blob)
            actual_images += 1

    if mode == "full" and result.tables_md:
        tables_dir = staging / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        for filename, text in result.tables_md:
            (tables_dir / filename).write_text(text, encoding="utf-8")

    actual_words = count_words(result.document_md)
    return actual_words, actual_images


def verify(
    *, baseline: Baseline, actual_words: int, actual_images: int, mode: str
) -> Verification:
    """Compute verification status from baseline + post-extraction counts."""
    word_ratio_ok = _env_float("PDF_SIDECAR_WORD_RATIO_OK", DEFAULT_WORD_RATIO_OK)
    word_ratio_fail = _env_float("PDF_SIDECAR_WORD_RATIO_FAIL", DEFAULT_WORD_RATIO_FAIL)
    image_ratio_ok = _env_float("PDF_SIDECAR_IMAGE_RATIO_OK", DEFAULT_IMAGE_RATIO_OK)

    expected_words = baseline.expected_words
    expected_images = baseline.expected_images

    denom = max(expected_words, 1)
    word_ratio = actual_words / denom

    notes: list[str] = []

    if expected_words > 0 and actual_words == 0:
        notes.append("zero words extracted from a non-empty PDF")
        status = "fail"
    elif word_ratio < word_ratio_fail:
        notes.append(
            f"word_ratio {word_ratio:.2f} below fail threshold {word_ratio_fail:.2f}"
        )
        status = "fail"
    elif word_ratio < word_ratio_ok:
        notes.append(
            f"word_ratio {word_ratio:.2f} below ok threshold {word_ratio_ok:.2f}"
        )
        status = "warning"
    else:
        status = "ok"

    if mode == "full" and expected_images > 0:
        denom_img = max(expected_images, 1)
        image_ratio = actual_images / denom_img
        if image_ratio < image_ratio_ok and status != "fail":
            notes.append(
                f"image_ratio {image_ratio:.2f} below ok threshold {image_ratio_ok:.2f}"
            )
            if status == "ok":
                status = "warning"

    return Verification(
        expected_words=expected_words,
        actual_words=actual_words,
        word_ratio=round(word_ratio, 4),
        expected_images=expected_images if mode == "full" else 0,
        actual_images=actual_images,
        status=status,
        notes=notes,
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Atomic commit
# ---------------------------------------------------------------------------


def commit_sidecar(staging: Path, final: Path) -> None:
    """Atomically replace `final` with `staging`.

    If `final` already exists, it's removed first (cache miss path). The
    rename is atomic on POSIX when src and dst live on the same filesystem;
    `staging` is created as a sibling of `final` to keep that guarantee.
    """
    if final.exists():
        shutil.rmtree(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pdf-sidecar",
        description=(
            "Extract a PDF into a cached sidecar of markdown + images. "
            "Re-runs are served from cache; mismatched hashes trigger re-extraction."
        ),
    )
    p.add_argument("pdf", help="Path to the source PDF.")
    p.add_argument(
        "--high-fidelity",
        action="store_true",
        help="Use Docling backend (better tables, heavier install).",
    )
    p.add_argument(
        "--text-only",
        action="store_true",
        help="Skip image and table extraction. Faster, smaller sidecar.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if a valid cache exists.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override sidecar location. Default: <pdf>.extracted/ next to "
            "the file, falling back to ~/.cache/pdf-sidecar/<sha256>/."
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-JSON output on stderr.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return _run(args)
    except PdfSidecarError as exc:
        print(f"pdf-sidecar: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        # Anything we didn't classify is a backend/internal bug. Surface
        # the trace and exit 3.
        traceback.print_exc()
        return 3


def _run(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        raise SourceUnreadable(f"{pdf_path} does not exist")
    if not pdf_path.is_file():
        raise SourceUnreadable(f"{pdf_path} is not a regular file")

    mode = "text-only" if args.text_only else "full"
    backend_name = "docling" if args.high_fidelity else "marker"

    source_sha = sha256_file(pdf_path)
    sidecar_dir = choose_sidecar_dir(pdf_path, args.out, source_sha)

    # ---- Cache hit?
    if not args.force:
        ok, _reason = cache_satisfies(
            sidecar_dir, source_sha=source_sha, requested_mode=mode
        )
        if ok:
            meta = json.loads((sidecar_dir / "metadata.json").read_text())
            _emit_status(
                "cached", sidecar_dir, meta, quiet=args.quiet
            )
            return 0

    # ---- Baseline pass (also detects scanned PDFs)
    baseline = baseline_pass(pdf_path)
    if baseline.is_scanned:
        raise ScannedPdf(
            f"{pdf_path.name} has only {baseline.chars} chars of extractable text "
            f"across {baseline.pages} pages — likely scanned. v1 doesn't OCR; use "
            f"the `pdf` skill's pytesseract recipe and re-run pdf-sidecar on the "
            f"OCR'd output."
        )

    # ---- Run backend
    backend = BACKENDS.get(backend_name)
    if backend is None:
        raise ArgError(f"unknown backend {backend_name!r}")

    if not args.quiet:
        print(
            f"pdf-sidecar: extracting {pdf_path.name} via {backend_name} "
            f"({mode} mode, sha={source_sha[:12]}…)",
            file=sys.stderr,
        )

    result = backend(pdf_path, mode)

    # ---- Stage + write + verify
    # Use a sibling tempdir so the final atomic rename stays on the same FS.
    sidecar_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".pdf-sidecar-staging-", dir=sidecar_dir.parent
    ) as staging_str:
        staging = Path(staging_str)
        # tempfile.TemporaryDirectory pre-creates the dir; write_sidecar wants
        # to populate it directly.
        actual_words, actual_images = write_sidecar(
            staging, result=result, mode=mode
        )

        verification = verify(
            baseline=baseline,
            actual_words=actual_words,
            actual_images=actual_images,
            mode=mode,
        )

        if verification.status == "fail":
            # Don't commit. Print the comparison to stderr for diagnosis.
            print(
                f"pdf-sidecar: verification FAILED for {pdf_path.name}: "
                f"{verification.notes}",
                file=sys.stderr,
            )
            raise VerificationFailure(
                f"verification failed (word_ratio={verification.word_ratio}); "
                f"sidecar not committed"
            )

        meta = build_metadata(
            pdf_path=pdf_path,
            source_sha=source_sha,
            baseline=baseline,
            result=result,
            mode=mode,
            verification=verification,
            actual_words=actual_words,
            actual_images=actual_images,
        )
        (staging / "metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        if verification.status == "warning" and not args.quiet:
            print(
                f"pdf-sidecar: verification WARNING for {pdf_path.name}: "
                f"{verification.notes}",
                file=sys.stderr,
            )

        commit_sidecar(staging, sidecar_dir)

        # tempfile.TemporaryDirectory.__exit__ will try to rmtree the staging
        # path, but commit_sidecar moved it. Recreate an empty dir so cleanup
        # doesn't raise.
        staging.mkdir(exist_ok=True)

    final_meta = json.loads((sidecar_dir / "metadata.json").read_text())
    _emit_status("extracted", sidecar_dir, final_meta, quiet=args.quiet)
    return 0


def build_metadata(
    *,
    pdf_path: Path,
    source_sha: str,
    baseline: Baseline,
    result: BackendResult,
    mode: str,
    verification: Verification,
    actual_words: int,
    actual_images: int,
) -> dict:
    return {
        "tool": {
            "name": "pdf-sidecar",
            "version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
        "backend": {
            "name": result.backend_name,
            "version": result.backend_version,
        },
        "mode": mode,
        "source": {
            "path": str(pdf_path),
            "sha256": source_sha,
            "size_bytes": pdf_path.stat().st_size,
        },
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "pages": baseline.pages,
            "figures": actual_images,
            "tables": len(result.tables_md),
            "chars": len(result.document_md),
            "words": actual_words,
        },
        "verification": asdict(verification),
    }


def _emit_status(
    status: str, sidecar_dir: Path, meta: dict, *, quiet: bool
) -> None:
    payload = {
        "status": status,
        "sidecar": str(sidecar_dir),
        "document_md": str(sidecar_dir / "document.md"),
        "stats": meta.get("stats", {}),
        "verification": meta.get("verification", {}),
        "mode": meta.get("mode"),
    }
    print(json.dumps(payload))


if __name__ == "__main__":
    raise SystemExit(main())
