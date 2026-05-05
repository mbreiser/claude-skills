# claude-skills

Claude Code skills I use across my workflow. Each skill is a self-contained directory with a `SKILL.md` frontmatter file (and optionally helper scripts, tests, fixtures).

## Install

Symlink everything into `~/.claude/skills/`:

```bash
git clone https://github.com/mbreiser/claude-skills.git
cd claude-skills
./install.sh
```

`install.sh` is idempotent. Running again after a `git pull` is safe — symlinks already pointing at the right place are skipped. Pass `--force` to replace pre-existing real directories or symlinks pointing elsewhere (existing real directories are backed up to `<name>.backup-<timestamp>` rather than deleted).

To install a single skill manually:

```bash
ln -s "$(pwd)/codex-plan-review" ~/.claude/skills/codex-plan-review
```

## Skills

| Skill | Purpose |
|---|---|
| [codex-plan-review](codex-plan-review/) | GPT-5.5 second opinion on an implementation plan before code is written. Runs standard + adversarial reviews in parallel via the Codex CLI, reconciles against an independent Claude pass, surfaces agreements / disagreements / open questions. |
| [codex-diff-review](codex-diff-review/) | GPT-5.5 second opinion on a code change (working-tree or branch diff) before commit or merge. Same parallel-reviews + reconciliation pattern as `codex-plan-review`, applied to written code. |
| [instruments](instruments/) | Control Digilent Analog Discovery 3 (AD3) and Saleae Logic Pro 8 test instruments via Python. Capture waveforms, generate trigger/stimulus signals, analyze oscilloscope / logic-analyzer traces. |
| [nano-banana-artwork](nano-banana-artwork/) | Generate consistent character artwork via Google's Nano Banana Pro (gemini-3-pro-image-preview). Themed posters, logos, character sets with cross-image consistency. |
| [kicad-design-review](kicad-design-review/) | KiCad PCB schematic review. Python extractor produces deterministic structured facts (BOM × netlist × schematic × positions, with confidence flags); skill synthesizes the human-readable design-review report. |

## Dependencies

- **codex-plan-review** / **codex-diff-review** require [Codex CLI](https://github.com/anthropics/codex) (`brew install codex` or follow the upstream install).
- **instruments** requires Digilent Waveforms SDK (AD3) and/or Saleae Logic 2 (Saleae). Python 3.14 on macOS for AD3.
- **kicad-design-review** requires Python 3.10+ and `kiutils` (installed via `pyproject.toml` in the skill directory).
- **nano-banana-artwork** requires a Google AI Studio API key (`GOOGLE_GENAI_API_KEY`).

Skills that need additional setup describe it in their own `SKILL.md` body.

## Updating

```bash
cd ~/Documents/GitHub/claude-skills   # or wherever you cloned it
git pull
```

Symlinks pick up changes automatically. No re-install needed unless a new skill was added (re-run `./install.sh` to link new skills).

## License

MIT — see [LICENSE](LICENSE).
