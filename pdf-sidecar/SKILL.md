---
name: pdf-sidecar
description: Extract a PDF's text, figures, and tables into a cached sidecar directory of markdown + images so Claude reads the markdown instead of the raw PDF on every read. Use whenever Claude needs to READ content from a PDF (scientific paper, datasheet, technical manual, spec sheet) — first check for a sibling `<file>.pdf.extracted/document.md`; if missing or stale, run `bin/pdf-sidecar.py`, then read the markdown. Do NOT use this skill to manipulate PDFs (merge, split, rotate, watermark, encrypt, fill forms, create new PDFs) — use the `pdf` skill for those. Triggers include "read this PDF", "summarize this paper", "what does this datasheet say about X", "extract figures from this paper", or any time a `.pdf` path is referenced and content needs to come out of it.
---

# PDF Sidecar Extraction

Convert a PDF into a deterministic, cached sidecar directory so reads are cheap from then on. Future Claude sessions reading the same PDF skip the token-heavy direct read and `Read` the sidecar's `document.md` instead.

## When this skill applies

Use this skill whenever the user wants Claude to **read** content out of a PDF:

- "Summarize this paper"
- "What does the datasheet say about pin 17?"
- "Extract the figures from this PDF"
- "Read the methods section of paper.pdf"
- Any time a `.pdf` file is in the working directory or referenced in the conversation and the user expects Claude to know what's in it.

**Do NOT use this skill** for PDF *manipulation* — use the built-in `pdf` skill instead:

- Merging, splitting, rotating PDFs
- Creating new PDFs from scratch (reportlab)
- Filling PDF forms
- Adding watermarks, encrypting/decrypting
- OCR'ing scanned PDFs (no text layer) — this skill exits with a clear pointer to the `pdf` skill's pytesseract recipe

## The sidecar contract

For `paper.pdf`, the extractor produces:

```
paper.pdf.extracted/
├── document.md       # full markdown, single file
├── metadata.json     # source sha256, backend, version, stats, verification
├── images/
│   ├── figure-001.png
│   └── ...
└── tables/
    ├── table-001.md
    └── ...
```

In `--text-only` mode only `document.md` and `metadata.json` are written.

If the PDF's parent directory isn't writable, the sidecar lands at `~/.cache/pdf-sidecar/<sha256>/` instead. `metadata.json.source.path` records the original location so it's always findable.

### `metadata.json` shape

```json
{
  "tool":     {"name": "pdf-sidecar", "version": "0.1.0", "schema_version": "0.1.0"},
  "backend":  {"name": "marker", "version": "..."},
  "mode":     "full",
  "source":   {"path": "...", "sha256": "<hex>", "size_bytes": 1234567},
  "extracted_at": "2026-05-23T...",
  "stats":    {"pages": 12, "figures": 7, "tables": 3, "chars": 48201, "words": 8132},
  "verification": {
    "expected_words":  8500, "actual_words":  8132, "word_ratio":  0.957,
    "expected_images": 7,    "actual_images": 7,
    "status": "ok"
  }
}
```

The sidecar is considered fresh if `source.sha256` matches a fresh hash of the source PDF and `schema_version` matches the current extractor.

## Workflow — this is the load-bearing payoff

When you encounter a PDF that needs to be read:

### Step 1 — Check for an existing sidecar

```bash
test -f "<pdf>.extracted/metadata.json" && \
  cat "<pdf>.extracted/metadata.json" | jq -r '.source.sha256'
```

Hash the source: `shasum -a 256 <pdf>` (macOS) or `sha256sum <pdf>` (Linux). If the two values match and `mode` in `metadata.json` is `"full"` (or `"text-only"` and you only need text), **read `<pdf>.extracted/document.md` directly. You are done.** No need to invoke the extractor.

### Step 2 — Otherwise run the extractor

```bash
# Default: Marker backend (fast, MIT, light deps)
uv run --script --with marker-pdf \
  ~/.claude/skills/pdf-sidecar/bin/pdf-sidecar.py paper.pdf
```

Optional flags:

| Flag | When to use |
|---|---|
| `--text-only` | User only needs text. Skips image + table extraction. Faster, smaller sidecar. |
| `--high-fidelity` | User specifically wants Docling for better table fidelity. Pulls ~2 GB of torch on first run. Invoke as `uv run --script --with docling ... --high-fidelity`. |
| `--force` | Re-extract even if a valid cache exists. |
| `--out DIR` | Override sidecar location (use when target dir is not writable and you want a specific cache location). |

The extractor prints a single JSON line to stdout:

```json
{"status": "cached", "sidecar": "/.../paper.pdf.extracted", "document_md": "...", "stats": {...}, "verification": {...}, "mode": "full"}
```

### Step 3 — Read the markdown

```bash
# Now read with the standard Read tool — it's a normal .md file
```

Images are referenced by relative path. Only `Read` an image if the user's question specifically asks about a figure.

## Backend selection

| Backend | Default | License | Strengths | Cost |
|---|---|---|---|---|
| **Marker** | ✓ | MIT | Fast, light deps, broad format support | First-run download of a few hundred MB |
| **Docling** | opt-in via `--high-fidelity` | Apache 2.0 | Best table fidelity, structured exports | ~2 GB torch download on first run |

Default to Marker unless the user explicitly asks for higher table fidelity or you're working with a datasheet whose tables are critical to the task.

## Verification — built into every fresh extraction

Every fresh run does a cheap baseline pass over the source PDF (via `pypdf`) before extraction to record `expected_words` and `expected_images`. After the backend finishes, the extractor compares what landed in `document.md` and `images/` against the baseline and writes a `verification` block into `metadata.json` with status `ok` / `warning` / `fail`:

- **`ok`** — `word_ratio >= 0.85` (and in full mode, `image_ratio >= 0.70`). Sidecar committed.
- **`warning`** — degraded but committed; check the `notes` array and the stderr message.
- **`fail`** — `word_ratio < 0.50` or zero words from a non-empty PDF. Sidecar is **NOT committed**; exit code 6. Report this to the user; do not synthesize from a non-existent sidecar.

Override thresholds with `PDF_SIDECAR_WORD_RATIO_OK`, `PDF_SIDECAR_WORD_RATIO_FAIL`, `PDF_SIDECAR_IMAGE_RATIO_OK` env vars if needed.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (cached or freshly extracted) |
| `2` | Source PDF missing, unreadable, or encrypted |
| `3` | Backend crashed during extraction |
| `4` | Scanned PDF (no extractable text layer) — use the `pdf` skill's OCR recipe |
| `5` | Argument error |
| `6` | Verification failed — sidecar NOT committed |

On any non-zero exit, stop. Report the error to the user verbatim. **Do not fabricate content from a failed extraction.**

## Failure modes to avoid

- **Don't read the raw PDF** when a fresh sidecar exists next to it. The whole point is to amortize the extraction cost.
- **Don't trust a sidecar without checking the hash.** PDFs get updated in place; the cache contract requires a sha256 match.
- **Don't synthesize content from a `status: "fail"` verification result.** The sidecar wasn't committed.
- **Don't use `--high-fidelity` reflexively.** Marker is the default for a reason — Docling pulls ~2 GB on first run.
- **Don't OCR here.** v1 doesn't do OCR. Hand off to the `pdf` skill for scanned PDFs.

## Permissions playbook

For zero-prompt invocation, recommend the user add to `~/.claude/settings.json`:

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

- **No OCR.** Scanned PDFs exit with code 4 and a pointer to the `pdf` skill.
- **Single `document.md`** — no per-page split for very large (400+ page) docs. Workable but not optimal.
- **Backend version is not part of the cache key.** Bumping Marker or Docling between runs won't auto-invalidate sidecars; the source sha is the only key. `--force` to re-extract after a backend upgrade.
- **Image dedup not implemented.** If a backend emits the same figure twice (e.g. headers/footers), both land in `images/`.
- **`metadata.json.source.path` is absolute** at extraction time; if the user moves the PDF, the cache still keys on sha so it stays valid, but the recorded path is stale.
