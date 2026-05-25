---
name: codex-diff-review
description: Get a GPT-5.5 second opinion on a code change (working tree or branch diff) before commit or merge. Runs both a standard code review and an adversarial pressure-test in parallel via Codex CLI, then reconciles their feedback against Claude's own analysis and surfaces a structured agreement / disagreement / open-questions report. Use this skill whenever the user asks for a "second opinion," "Codex review," "GPT-5 review," "cross-check," "pre-commit review," or "sanity check" on code that has already been written — even if Codex isn't named explicitly. Also use when the user mentions reviewing a diff against main, a feature branch, or staged changes. Do NOT use for plans or designs that haven't been implemented (use codex-plan-review instead).
---

# Codex Diff Review

Get a GPT-5.5 second opinion on a code change, then reconcile it with your own analysis. Surface agreements, disagreements, and open questions. Do not auto-revise.

## When this skill applies

The user wants outside-model feedback on **already-written code** — staged changes, unstaged working tree, or branch diff. Triggers include "Codex review the diff," "cross-check before I commit," "pre-commit second opinion," "pressure-test what I just implemented." If no code exists yet, use `codex-plan-review` instead.

## Reducing permission prompts (one-time setup)

A fresh run triggers 2–4 permission prompts: the `claude-analysis.md` write (Step 2), the orchestrator bash call (Step 3), the final report write (Step 4), and possibly a `git diff` Bash call in Step 1 if you choose to read the diff before writing analysis. All go to zero if these entries are added to `~/.claude/settings.json` (apply via `/permissions` or by editing the file):

```jsonc
{
  "permissions": {
    "allow": [
      "Bash(codex --version)",
      "Bash(bash *codex-plan-review/bin/run-review.sh *)",
      "Bash(bash *codex-diff-review/bin/run-review.sh *)",
      "Read(./.codex-review/**)",
      "Write(./.codex-review/**)"
    ]
  }
}
```

This is opt-in: do not modify the user's settings on the user's behalf. Mention the snippet if the user complains about prompt churn.

## Defining "the diff"

Ask one targeted question if ambiguous, otherwise infer. The orchestrator's first argument selects the diff source:

| User intent | Argument |
|---|---|
| "review what I just did" / against branch base | `<base-ref>` (e.g., `main`, `develop`, `HEAD~3`) → `git diff <ref>...HEAD` |
| "review staged changes" | `--staged` → `git diff --staged` |
| "review my working tree" | `--working` → `git diff HEAD` (working tree vs HEAD; **includes both staged and unstaged**) |

If the resulting diff is empty, the orchestrator exits with code 2 and a clear message.

**Untracked files note:** `git diff` doesn't show untracked files. If the review should cover new files in the working tree, run `git add -N <paths>` (intent-to-add) first — that makes them appear in `git diff HEAD` as additions without actually staging the content.

**Large diffs:** the orchestrator does not pre-screen size; it runs whatever diff you point it at. If you know the diff is huge (thousands of lines) and want to chunk by file group before paying for two full Codex passes, run `git diff <ref> --stat` yourself first and decide. The orchestrator's `meta.json` reports `diff_lines` and `file_count` after the fact for the final report.

## Workflow

### Step 1 — See the diff yourself

Run `git diff <base-ref>` (or `git diff --staged` / `git diff HEAD`, matching the argument you'll pass to the orchestrator) so you can read what's actually changed. You can't write a meaningful independent review of a diff you haven't seen, and the orchestrator's `diff.patch` doesn't exist until after Step 3 finishes.

### Step 2 — Write your independent review (BEFORE running Codex)

Save your own review of the diff to `.codex-review/claude-analysis.md` **before** launching the orchestrator. Same structure as Codex's prompts: correctness, tests, fit, risk and reliability, suggested changes, grouped by severity (blocking / significant / minor); plus an adversarial pass (right approach? assumptions? failure modes? hidden costs? reversibility? races/data loss? strongest argument against merging?).

Author bias on code is even stronger than on plans — try to review as if seeing it for the first time. The file checkpoint exists so you commit to your view before reading Codex.

### Step 3 — Run the orchestrator

```bash
bash codex-diff-review/bin/run-review.sh <base-ref|--staged|--working>
```

The script captures the diff via `git diff`, writes it to a per-run directory, then runs two `codex exec` passes (standard + adversarial) in parallel with `--output-last-message`, applies a wall-clock timeout (default 600s), traps SIGINT to clean up children, and emits a `meta.json` index. It prints the meta.json path to stdout.

Environment overrides:
- `CODEX_REVIEW_MODEL` — pin a model (defaults to `gpt-5.5`); use this if `gpt-5.5` is unavailable on the user's account.
- `CODEX_REVIEW_TIMEOUT` — seconds per pass (default 600; must be a positive integer).

Exit codes: `0` both passes succeeded, `1` exactly one failed (proceed with the survivor), `2` setup failure (codex CLI missing, not in a git repo, empty diff, bad timeout), `3` both failed (stop and report), `130` interrupted.

### Step 4 — Read the outputs

Read `meta.json`, then the `standard_md` and `adversarial_md` files it names. The JSONL streams are kept as provenance but you should not need to parse them — `--output-last-message` already extracts the final assistant message into `.md`.

If a pass failed (`std_rc != 0` or `adv_rc != 0`), say so in the final report rather than fabricating output. You can still reconcile with one Codex review plus your analysis, but flag the missing piece explicitly.

### Step 5 — Reconcile and write the final report

Use the output format below. Save to `.codex-review/report-<timestamp>.md` and present inline. **Do not modify the code.**

Reasoning rules:

- **Agreement** = at least two of three flagged the same issue. Highest confidence.
- **Disagreement** = Codex raised what Claude didn't, or contradicted Claude.
- **Codex internal disagreement** = standard vs adversarial diverge — articulate the tradeoff.
- **Severity matters.** Within each section, group by **blocking** (correctness, security, data loss), **significant** (subtle bugs, design issues, maintainability), **minor** (style, naming). Don't bury blocking issues under nits.
- **Open questions** = depend on context the reviews didn't have.

When Claude and Codex disagree, present Codex's view at least as fully as your own.

## Output format

````markdown
# Codex Cross-Review: <short diff description>

**Diff:** `<diff_desc>` (<N> files, <diff_lines> lines)
**Reviewed by:** Claude (independent), Codex GPT-5.5 standard, Codex GPT-5.5 adversarial
**Date:** <ISO date>

## 1. Where all three agree

### Blocking
- **<issue>** [Claude / Codex-std / Codex-adv] — `path/to/file.ext:line`
  - <one or two sentences>

### Significant
- ...

### Minor
- ...

## 2. Where Codex disagrees with Claude

### Codex raised, Claude missed
- **<issue>** [Codex-std / Codex-adv / both] — `path:line`
  - Codex's point: <fair summary>
  - My take: <agree / partial / disagree, with reasoning>

### Claude raised, Codex didn't
- **<issue>** — `path:line`
  - My point: <summary>
  - Possible reason Codex skipped: <e.g., requires runtime context>

### Direct contradictions
- **<issue>** — `path:line`
  - Claude said: <X>
  - Codex said: <not-X>
  - Who I think is right and why. If genuinely unsure, say so.

## 3. Where Codex's two reviews disagree

- **<topic>** — `path:line`
  - Standard view: <summary>
  - Adversarial view: <summary>
  - Tradeoff: <one or two sentences>

## 4. Open questions

- <question requiring user input>

## 5. Raw outputs

- Claude's analysis: `.codex-review/claude-analysis.md`
- Codex standard: `<standard_md from meta.json>`
- Codex adversarial: `<adversarial_md from meta.json>`
- Diff: `<diff_path from meta.json>`
- Per-run index: `<meta.json path>`
````

Empty sections keep the header with `*(none)*` underneath.

## Failure modes to avoid

- **Don't summarize Codex.** Reconciliation is the value.
- **Don't auto-fix.** Even unanimous agreement on a fix doesn't authorize the change.
- **Don't skip your own review.** Three voices is the point.
- **Don't bury blocking issues.** Correctness or security flagged anywhere → top of section 1 or 2 with explicit "Blocking" tagging.
- **Don't fabricate Codex output.** Failed run? Say so.
- **Don't dismiss Codex on "it doesn't have context."** Say "here's what Codex said, here's the context that may change the picture" — not silent omission.
- **Don't write claude-analysis.md after reading Codex.** Independence requires committing to your view before peeking.
