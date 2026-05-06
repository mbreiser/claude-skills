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

## Known limitations (v1.1)

### Resolved in v1.1
- **Net names truncated to 14 characters** — still happens (IPC-D-356A standard), but `symbol_pin_name` (from `lib_symbols`) gives an honest, untruncated identifier per pin alongside the truncated `net_name`.
- **GPIO name resolution** — done via `(lib_symbols)` parsing in v1.1. ~99-100 % of facts on G6 boards now have `symbol_pin_name` resolved (e.g. footprint pin 56 → `GPIO45_ADC5`).
- **Sheet-instance walker** — fixes v1's "313/1494 facts confirmed" recall on hierarchical boards; v1.1 resolves per-instance refdeses (e.g. arena's panel-column J5 → J19/J23/...).
- **Multi-unit symbols** — handled. OPA2277 unit A pins 1-4 / unit B pins 5-8 correctly disambiguated.
- **KiCad schema-version probe** — `(version YYYYMMDD)` and `(generator)` recorded per file; out-of-range versions emit a doc-quality finding without bailing.
- **Cache versioning** — `manifest.json` per cache dir; loud warning + refetch on stale (v1-shaped) caches.

### v1.1 partial / experimental
- **Wire-trace through passive transit** — Phase 2 prototype behind `--trace-pin` flag, output in `_experimental.wire_traces[]`. Validated on cleanly-bounded cases (e.g. crystal-circuit transit through 1kΩ resistor; MCP4725 → AOUT label + BNC). Default transit: resistors + ferrites only; refuses GND/power; capacitors deliberately not transit-able.
- **Single-sheet BFS only** — no cross-sheet hierarchical-label propagation in v1.1. Pins whose connections live on another sheet trace to the sheet boundary (or `(unconnected)`); cross-sheet via sheet-pin matching is deferred to v1.2.
- **No bus-alias expansion** — arena uses bus-style sheet pins (`PAN{PAN}`, `TNY{TNY}`, `AIN{AIN}`, `I2C{I2C}`); BFS doesn't expand them in v1.1. v1.2 candidate.
- **Active-component signal flow** — BFS terminates at active-IC pins. Tracing through op-amps / mux / level-translators (e.g. AIN0 → OPA2277 → BNC) is not modelled. Acceptance softened: "reaches OPA2277 input pin" rather than "through OPA2277 chain". v1.3 territory.
- **Pin-coordinate transform on large multi-pin chips** — edge case observed on the RP2350 80-pin QFN where U2 pin 56 BFS finds many decoupling-cap endpoints. Suggests a transform issue specific to large symbols with rotation/mirror combinations. v1.2 candidate.

### Still deferred to v1.2 / later
- **`sym-lib-table` parsing** with KiCad env-var substitution (`${KIPRJMOD}` etc.). v1.1 falls back to project-local `.kicad_sym` filename-stem matching only.
- **Reconcile mode** (KiCad source ↔ existing markdown design doc).
- **Multi-revision diff** (e.g. v0.2 → v0.3 deltas).
- **`.kicad_pcb` layout review** — different domain.

## Wire-trace usage (Phase 2, experimental)

```bash
./bin/kicad-extract.py \
  --source 'iorodeo/LED-Display_G6_Hardware_Panel@prod_v0p2r0:panel_rp2354_20x20_v0p2' \
  --trace-pin 'Y1:1,U85:1,R29:1' \
  | jq '._experimental.wire_traces'
```

Each trace returns: `{source: {refdes, pin, sheet, position}, path: [...], endpoints: [...], confidence: "single-sheet"}`. Endpoints are labels (with `label_kind`), other component pins (with `<refdes>:<pin> (<symbol_pin_name>)`), or boundary markers (`(unconnected)`, sheet-pin → file).

Override transit defaults: `--transit-prefix R,FB,L` (default: `R,FB`).

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
