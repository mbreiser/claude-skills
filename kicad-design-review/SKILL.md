---
name: kicad-design-review
description: Produce a structured design-review report for a KiCad PCB project — local path or GitHub-hosted. Runs a deterministic Python extractor (BOM × netlist × positions × schematic) to produce structured JSON facts, then synthesizes a human-readable report with executive summary, peripheral reference table, open questions, and documentation-quality observations. Use when the user asks for a "KiCad review", "PCB design review", "what's on this board", "schematic audit", "bring-up reference", "pin-out summary", or wants to interrogate a KiCad project to find pin maps, BOM-confirmed parts, or schematic-vs-doc discrepancies. Do NOT use for `.kicad_pcb` layout review (impedance, stackup, DFM) — that's a different problem.
---

# KiCad PCB Design Review

Two-stage workflow: a Python extractor produces deterministic structured facts, then this skill synthesizes them into a human-readable report. The deterministic work (parsing IPC-D-356A netlists, walking hierarchical schematic sheets, joining BOM and netlist by refdes, computing confidence flags) lives in `bin/kicad-extract.py`. The LLM does not redo the deterministic work — it only reads the extractor's JSON and synthesizes.

## When this skill applies

The user wants a structured review of a KiCad PCB project. Triggers include:

- "Review this KiCad project"
- "What's on this board?"
- "Give me a pin-out / bring-up reference for X"
- "Audit the schematic vs the docs"
- "What MCU and peripherals does this board use?"
- "What part is U23?" (single-fact questions still benefit from running the full extract once and serving from cache)
- Any mention of a `.kicad_sch`, `.kicad_pro`, or KiCad project URL

**Do NOT use this skill for:**

- `.kicad_pcb` layout / manufacturing review (impedance, stackup, DRC, gerber generation) — that's outside scope
- BOM cost analysis or supplier substitution — extractor reports parts but doesn't price them
- Comparing two revisions of the same board (v1.1 candidate, not in v1)
- Reconciling KiCad source against an *existing* markdown design doc — that's `kicad-design-review`'s reconcile mode (v1.1 candidate; not implemented)

## What a KiCad project is

KiCad files relevant to design-review (in priority order for this skill):

- **`.kicad_pro`** — project-level config; identifies the root schematic
- **`.kicad_sch`** — schematic file (S-expressions). A project has a root `.kicad_sch` plus sub-sheet files referenced via `(sheet (file "...") ...)` blocks. The skill walks all sub-sheets.
- **`production/<rev>/bom.csv`** — Bill of Materials; refdes → part / LCSC / package. CSV with KiCad's standard headers; multi-refdes rows like `"R1, R2, R3"` are common.
- **`production/<rev>/netlist.ipc`** — IPC-D-356A netlist; fixed-column format giving refdes-pin → net mapping. The single most useful file for cross-referencing.
- **`production/<rev>/positions.csv`** — refdes → XY + rotation (used for board-revision diffs and footprint-side detection).
- **`.kicad_sym`** — symbol library files; needed for full GPIO-alt-name resolution (not parsed in v1; consumers cross-reference externally).

Production extracts may not exist if the project hasn't been routed for fabrication; the skill degrades gracefully (skill output flags it in doc-quality).

## Inputs the skill accepts

Three forms of source specifier — pass to `kicad-extract.py --source`:

1. **Local path** to a KiCad project directory: `/path/to/project_v0p2r0/`
2. **GitHub shorthand**: `owner/repo[@ref][:path]`
   - Example: `iorodeo/LED-Display_G6_Hardware_Panel@prod_v0p2r0:panel_rp2354_20x20_v0p2`
   - `ref` defaults to `main` if omitted; `path` defaults to the repo root.
3. **GitHub URL**: `https://github.com/owner/repo/tree/ref/path/to/project`

## Workflow

### Step 1 — Stage / gitignore check

Ensure `.kicad-review/` is in the working repo's `.gitignore`. If not, append it:

```bash
test -f .gitignore && grep -qx '.kicad-review/' .gitignore || echo '.kicad-review/' >> .gitignore
```

Mention the addition in the report so the user knows.

### Step 2 — Auth precheck (only if source is GitHub-hosted)

```bash
gh auth status
```

If unauthenticated: stop with a clear remediation message ("`gh auth login` first"). Don't try to fetch.

### Step 3 — Run the extractor

Single shell call — extractor handles fetching, ref-to-SHA resolution, caching, parsing, cross-referencing internally:

```bash
~/.claude/skills/kicad-design-review/bin/kicad-extract.py \
  --source <user-input> \
  --cache .kicad-review \
  > .kicad-review/extract-<timestamp>.json
```

Extract path follows `~/.claude/skills/kicad-design-review/bin/kicad-extract.py` (the skill is symlinked from the install repo). On first run, `uv` installs `sexpdata` automatically (PEP 723 inline-deps).

If extractor exits non-zero: stop, report the error to the user, do not proceed to synthesis. Don't fabricate output.

### Step 4 — Read structured JSON

Parse `extract-<timestamp>.json`. Sanity-check:
- `stats.n_bom > 0` — if zero, BOM was missing or unparseable; doc-quality findings will say so
- `stats.n_netlist > 0` — same for netlist
- `stats.n_facts > 0` — there should be at least one cross-referenced fact

If all three are zero: the source likely isn't a KiCad project (or is missing production extracts and has no parseable schematic). Report that to the user; don't synthesize from empty data.

### Step 5 — Synthesize the report

This is the LLM's job. Produce a markdown report with these sections:

**Executive summary** (2-3 paragraphs):
- What's the board? Identify the MCU (find in BOM by `value` matching common MCU patterns: `RP2354`, `STM32`, `Teensy`, `ESP32`, etc.), board class (single-purpose / general-purpose), and rough complexity.
- What are the major peripherals? (Power regs, USB, SPI/I2C devices, ADCs/DACs, level translators.)
- IO surface? (Connectors, BNCs, headers — find in BOM by package containing "Conn" / "BNC" / "JST" / etc.)
- Identify the production revision (from `inventory.production_extract_dir`).

**Peripheral reference table** — group facts by `category`:
- Columns: `Function | Refdes | Footprint pin | Net name | Part / LCSC | Confidence | Notes`
- Group by category (`mcu`, `power_rail`, `spi_bus`, `i2c_bus`, `usb`, `analog_in`, `analog_out`, `external_interrupt`, `digital_io`, `connector`, etc.)
- Within each group, sort by refdes then pin
- Skip entries with confidence below `bom+netlist` unless the user asks for everything (those are noisy)
- Truncated net names (14 chars per IPC-D-356A) — note where helpful that fuller names live in the schematic

**Wire-trace appendix** — only if any:
- Currently v1 doesn't auto-emit wire-traces (extractor doesn't do BFS through passive transit).
- If the report needs to discuss specific traces (e.g. "where does R25 go?"), the user can do this manually using the schematic data in the JSON output. Note this as a v1 limitation in the report.

**Open questions** — truthfulness-based:
- Pull from `extract.open_questions[]` (extractor-emitted)
- Add LLM-synthesized open questions only when there's a real ambiguity to surface (e.g. "Is jumper J30 default open or shorted? Schematic shows the default fits but firmware can't detect it.")
- **Empty section is fine** — if everything resolves, write "No open questions surfaced from this review." Don't pad.

**Documentation quality observations** — from `extract.doc_quality[]`:
- Severity-order: error > warn > info
- For each: state the finding + what it means + suggested resolution path
- If extractor ran on a project with no production extracts → doc-quality will flag it; report it prominently because the rest of the review is degraded

### Step 6 — Write the report

Save to `.kicad-review/report-<timestamp>.md`. Print inline to the conversation. Provide the file path so the user can refer back.

Don't modify any project source files. Don't commit anything in `.kicad-review/`.

## Output format

```markdown
# KiCad Design Review: <board name from MCU + size cue>

**Source:** <owner/repo@sha:path> (resolved from <ref>)
**Reviewed:** <ISO date>
**Production rev (if known):** <vXpYrZ>
**Extractor:** kicad-extract v0.1.0; <stats summary>

## Executive summary

<2-3 paragraphs: MCU, major peripherals, IO surface, board class>

## Peripheral reference table

### MCU and core
| Function | Refdes | Pin | Net | Part / LCSC | Confidence | Notes |
|---|---|---|---|---|---|---|
...

### Power rails
| ... | ... | ... | ... | ... | ... | ... |

### SPI bus(es)
...

### I2C
...

### Analog I/O
...

### External interrupts / triggers
...

### Connectors
...

## Wire-trace appendix

<Only if needed; v1 emits "No wire-traces required for this review" or
 a manual analysis when the user requests one.>

## Open questions

- <Item, with resolution path>

(Empty section: "No open questions surfaced.")

## Documentation quality observations

- **<finding>** [severity] — <details, with suggested resolution>

## Provenance

- Extracted from <source> at <timestamp>
- Cache: <cache_dir>
- Stats: <n_bom> BOM entries, <n_netlist> netlist entries, <n_schematic_sheets> schematic sheets, <n_facts> cross-referenced facts.
```

## Failure modes to avoid

- **Don't synthesize facts the extractor didn't emit.** If a refdes appears with confidence `single-source`, surface it that way — don't promote to "verified".
- **Don't pad open questions or doc-quality observations** to hit a count. Empty is fine.
- **Don't bypass the extractor for "quick" lookups.** The whole point of the inverted architecture is that the LLM doesn't redo deterministic work.
- **Don't trust ground-truth markdown docs blindly during validation.** If the user has a markdown doc to compare against (and that's reconcile mode, not greenfield), check the open-issues docs in their repo first for known errors.
- **If extractor exits non-zero, stop.** Report the error verbatim. Don't fabricate from partial data.
- **Don't commit anything in `.kicad-review/`.** It's a scratch directory.

## Permissions playbook

For zero-prompt invocation, suggest the user add to `~/.claude/settings.json`:

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

`gh api` is the most consequential; without it, every fetched file prompts.

## v1 known limitations

- Net names from `netlist.ipc` are truncated to 14 chars (IPC-D-356A standard); fuller names in schematic aren't cross-referenced in v1.
- GPIO-name resolution (e.g. footprint pin 56 → "GP45") requires `.kicad_sym` library parsing — not implemented; consumers map externally using package pinout references.
- Wire-trace through passive transit (follow R25 → other terminal → fan-out → panels) is not implemented. Production extracts cover ~80 % of typical questions; the rest needs schematic graph BFS (v1.1).
- Tested against KiCad 7.x output. Newer schemas may parse but with degraded fidelity.
- v1 is greenfield-review-only. Reconciling against an existing markdown doc is v1.1.
