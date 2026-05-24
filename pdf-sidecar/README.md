# pdf-sidecar

A Claude Code skill that converts a PDF into a **cached sidecar directory** of markdown and images. Once a PDF is extracted, every future Claude session reads the cheap `document.md` instead of paying the token cost of re-reading the raw PDF.

Two pieces:

- **`bin/pdf-sidecar.py`** — deterministic Python extractor. Cheap baseline pass with `pypdf` (counts expected words/images, detects scanned PDFs), then dispatches to a markdown-native backend (Marker by default, Docling on `--high-fidelity`). Verifies the result against the baseline before committing the sidecar. Idempotent — re-runs against the same PDF are served from cache.
- **`SKILL.md`** — Claude Code skill body. Tells the model when to check for a sidecar, when to invoke the extractor, and how to read the output. Scope is deliberately disjoint from the built-in `pdf` skill (which handles PDF *manipulation* — merge/split/forms/OCR).

## Quick start

```bash
# Default (Marker backend — MIT, fast, light deps)
uv run --script --with marker-pdf bin/pdf-sidecar.py paper.pdf

# High-fidelity tables (Docling — pulls ~2 GB of torch on first run)
uv run --script --with docling bin/pdf-sidecar.py datasheet.pdf --high-fidelity

# Text-only (skip image + table extraction; faster, smaller sidecar)
uv run --script --with marker-pdf bin/pdf-sidecar.py paper.pdf --text-only

# Force re-extraction even if the cache is valid
uv run --script --with marker-pdf bin/pdf-sidecar.py paper.pdf --force
```

Stdout is a single JSON line:

```json
{"status": "extracted", "sidecar": "/path/paper.pdf.extracted", "document_md": "...", "stats": {...}, "verification": {...}, "mode": "full"}
```

## Sidecar layout

```
paper.pdf.extracted/
├── document.md       # full markdown, single file
├── metadata.json     # source sha256, backend, version, stats, verification
├── images/           # full mode only
│   ├── figure-001.png
│   └── ...
└── tables/           # full mode only
    ├── table-001.md
    └── ...
```

In `--text-only` mode only `document.md` and `metadata.json` are written.

If the PDF's parent directory isn't writable (network mount, read-only volume), the sidecar falls back to `~/.cache/pdf-sidecar/<sha256>/`. `--out DIR` overrides both.

## Cache contract

Re-extraction is triggered when **any** of these hold:

1. Sidecar directory doesn't exist
2. `metadata.json` is missing or unparseable
3. `metadata.json.tool.schema_version` doesn't match the current extractor
4. `metadata.json.source.sha256` doesn't match `sha256sum <pdf>`
5. Cached `mode` is `text-only` but the request is `full` (text-only cache can't satisfy a full request; the reverse is fine — a full cache satisfies a text-only request)
6. `--force` was passed

Otherwise it's a cache hit and the script exits 0 in under a second with `"status": "cached"`.

## Verification

Every fresh extraction runs a verification gate before committing the sidecar:

1. **Baseline pass** (cheap, via `pypdf`) records `expected_words` (words extractable from the source PDF's text layer) and `expected_images` (count of image XObjects).
2. **Post-extraction comparison** counts what landed in `document.md` and `images/`.
3. **Status**:
   - `ok` — `word_ratio >= 0.85` AND (text-only OR `image_ratio >= 0.70`)
   - `warning` — `word_ratio in [0.50, 0.85)` or image deficit >30%. Sidecar still committed; warning logged to stderr and `metadata.json`.
   - `fail` — `word_ratio < 0.50`, or zero words from a non-empty PDF. **Sidecar NOT committed.** Exit code 6.

Thresholds are env-var-tunable:

| Var | Default |
|---|---|
| `PDF_SIDECAR_WORD_RATIO_OK` | 0.85 |
| `PDF_SIDECAR_WORD_RATIO_FAIL` | 0.50 |
| `PDF_SIDECAR_IMAGE_RATIO_OK` | 0.70 |

On a cache hit, the cached `verification` block is trusted (the source sha matched).

## Backend trade-offs

| Backend | License | Speed | Tables | First-run cost |
|---|---|---|---|---|
| **Marker** (default) | MIT | Fast (~25 pp/s on H100, much slower on CPU but usable) | Decent | A few hundred MB of model weights |
| **Docling** (`--high-fidelity`) | Apache 2.0 | Slower | Best | ~2 GB torch download |

Both produce the same sidecar layout. Choose Docling when table fidelity is the critical thing (datasheets, financial reports) and you're willing to pay the install cost.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (cached or extracted) |
| `2` | Source PDF missing, unreadable, or encrypted |
| `3` | Backend crashed |
| `4` | Scanned PDF — no text layer. Use the built-in `pdf` skill's pytesseract recipe. |
| `5` | Argument error |
| `6` | Verification failed — sidecar not committed |

## Tests

```bash
cd pdf-sidecar
uv run --with pytest --with reportlab --with pypdf pytest tests/ -v
```

Tests use synthetic PDFs generated at runtime via reportlab (no fixtures checked in for the unit tests; one tiny real-world PDF lives under `tests/fixtures/` and is gated by `PDF_SIDECAR_REAL_BACKEND=1` so CI doesn't need Marker/Docling installed). Backend dispatch tests monkey-patch the `BACKENDS` dict with stubs, so neither torch nor Marker are needed to validate the rest of the pipeline.

## Permissions

For zero-prompt invocation, suggest adding to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run:*)",
      "Bash(pdf-sidecar.py:*)",
      "Bash(./bin/pdf-sidecar.py:*)",
      "Bash(shasum:*)",
      "Bash(sha256sum:*)",
      "Bash(jq:*)"
    ]
  }
}
```

## v1 known limitations

- No OCR; scanned PDFs exit 4 and point at the `pdf` skill.
- Single `document.md` only — no per-page split for very large (400+ page) docs.
- Backend version isn't part of the cache key. After bumping Marker or Docling, use `--force` to refresh sidecars if you want the new backend's output.
- No image deduplication — backend-emitted duplicates land as separate files.
- `metadata.json.source.path` is the absolute path at extraction time; moving the PDF doesn't invalidate the cache (the sha key still matches) but the recorded path goes stale.
