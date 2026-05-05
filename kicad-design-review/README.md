# kicad-design-review

Two pieces:

- **`bin/kicad-extract.py`** — deterministic Python extractor that produces structured JSON facts from a KiCad project (local path or GitHub-hosted). Walks BOM × netlist × positions × schematic, emits one fact per refdes-pin with a confidence flag (`netlist+bom+schematic`, `netlist+bom`, etc.) plus doc-quality observations.
- **`SKILL.md`** — the Claude Code skill body. Thin orchestrator: invoke the extractor, parse the JSON, synthesize the human-readable design-review report. The deterministic work lives in the extractor; the LLM does the synthesis.

## Quick start

```bash
# Local KiCad project
./bin/kicad-extract.py --source /path/to/project | jq .

# GitHub-hosted (uses gh CLI; must be authenticated)
./bin/kicad-extract.py --source 'owner/repo@ref:path/to/project' | jq .

# GitHub URL
./bin/kicad-extract.py --source 'https://github.com/owner/repo/tree/ref/path' | jq .
```

Output is structured JSON to stdout, suitable for downstream analysis or the synthesis step in `SKILL.md`.

The extractor uses `uv run --script` with PEP 723 inline dependencies — no venv setup needed. First run installs `sexpdata` automatically.

## Caching

By default, fetched files are cached under `./.kicad-review/<owner>__<repo>__<sha>__<path-hash>/`. Cache is keyed by resolved commit SHA (refs are resolved via `gh api` first), so different SHAs don't collide and re-runs against the same SHA are served from cache without re-fetching. Override with `--cache <dir>` or invalidate with `--no-cache`.

Add `.kicad-review/` to `.gitignore` in the working repo before running.

## What's in the JSON output

```jsonc
{
  "tool": {"name": "kicad-extract", "version": "0.1.0"},
  "fetched_at": "...",
  "source": {"kind": "github", "owner": "...", "repo": "...", "ref": "...", "resolved_sha": "...", "path": "..."},
  "inventory": {
    "all_files": [{"rel_path": "...", "size": 1234, "sha": "..."}, ...],
    "production_extract_dir": "production/v0p2r1",
    "root_schematic": "panel_rp2354_20x20.kicad_sch"
  },
  "bom": [{"refdes": "U2", "part": "...", "lcsc": "C39843328", "raw": {...}}, ...],
  "netlist": [{"net": "+3V3", "refdes": "U2", "pin": "5"}, ...],
  "positions": [{"refdes": "D50", "x": 51.125, "y": -51.125, "rotation": 45}, ...],
  "schematic": {"sheet.kicad_sch": {"symbols": [...], "labels": [...], "sheets": [...]}, ...},
  "facts": [{"category": "spi_bus", "function": "...", "refdes": "U1", "pin": "13", "net_name": "TNY.SCK_B0", "confidence": "netlist+bom", "notes": [...]}, ...],
  "doc_quality": [{"finding": "...", "severity": "warn", "details": "..."}, ...],
  "open_questions": [...],
  "stats": {"n_files": ..., "n_bom": ..., "n_netlist": ..., ...}
}
```

## Known limitations (v1)

- **Net names are truncated to 14 characters** when sourced from `netlist.ipc` (IPC-D-356A standard limit). Full names exist in the schematic but cross-reference to the IPC nets isn't implemented in v1.
- **GPIO name resolution** (e.g. footprint-pin 56 → "GP45") requires parsing `.kicad_sym` library files to extract symbol-pin alt-name tables. Not implemented in v1; consumers map footprint pins to GPIO names externally.
- **Wire-trace through passive transit** (e.g. follow R25 to its other terminal then to the panel-EINT bus) is not implemented. Production extracts (BOM × netlist) cover ~80 % of typical questions; the rest needs schematic graph BFS, which is a v1.1 candidate.
- **KiCad schema versioning**: v1 was tested against KiCad 7.x output. Other versions may parse but with degraded fidelity.
- **Symbol library parsing**: not implemented. Symbol metadata is read from the schematic instances directly; if the schematic doesn't embed symbols (older KiCad), some details may be missing.

## Tested against

- `floesche/LED-Display_G6_Hardware_Panel @ 23dad5e:panel_rp2354_20x20_v0p2` (G6 panel v0.2)
- `reiserlab/LED-Display_G6_Hardware_Arena @ 0a8ec33c:arena_10-10/arena_10-10_v1` (G6 arena v1.1.7)

Verified: BOM with multi-refdes rows + UTF-8 BOM headers, IPC-D-356A fixed-column netlist, positions.csv with `Mid X / Mid Y` headers, hierarchical schematic walks (6 sheets for panel, 12 sheets for arena), 1300+ netlist entries each, no fabrication of facts.

## Permissions (for the SKILL)

For Claude Code to run the extractor without per-call permission prompts, recommend adding to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(gh api:*)",
      "Bash(./bin/kicad-extract.py:*)",
      "Bash(kicad-extract.py:*)",
      "Bash(uv run:*)",
      "Bash(jq:*)"
    ]
  }
}
```
