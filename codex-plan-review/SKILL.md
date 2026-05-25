---
name: codex-plan-review
description: Get a GPT-5.5 second opinion on an implementation plan or design proposal before writing code. Runs both a standard review and an adversarial pressure-test in parallel via Codex CLI, then reconciles their feedback against Claude's own analysis and surfaces a structured agreement / disagreement / open-questions report. Use this skill whenever the user asks for a "second opinion," "Codex review," "GPT-5 review," "cross-check," "pressure-test," or "sanity check" on a plan, design, approach, or proposed implementation — even if Codex isn't named explicitly. Also use when the user has just written or accepted a plan and is about to start implementation. Do NOT use for already-written code (use codex-diff-review instead).
---

# Codex Plan Review

Get a GPT-5.5 second opinion on an implementation plan, then reconcile it with your own analysis. Surface agreements, disagreements, and open questions for the user to decide on. Do not auto-revise the plan.

## When this skill applies

The user wants outside-model feedback on a plan or design **before** code is written. Triggers include "Codex second opinion," "cross-check this design," "sanity-check the approach," "pressure-test this plan." If code already exists, use `codex-diff-review` instead.

## Reducing permission prompts (one-time setup)

A fresh run triggers 3–4 permission prompts: the `claude-analysis.md` write (Step 2), the orchestrator bash call (Step 3), the final report write (Step 5), and optionally a staging write if the plan isn't already in a file (Step 1). All four go to zero if these entries are added to `~/.claude/settings.json` (apply via `/permissions` or by editing the file):

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

## Workflow

### Step 1 — Stage the plan

If the plan is already in a file, note its path. Otherwise write it to `.codex-review/plan-<timestamp>.md` at the repo root. `.codex-review/` should already be in `.gitignore` (sibling skills assume this; check if unsure).

### Step 2 — Write your independent review (BEFORE running Codex)

This is a soft commitment device. Save your own review to `.codex-review/claude-analysis.md` **before** launching the orchestrator. Structure it the same way the prompts ask Codex for: correctness / completeness / fit / risk / suggested changes, plus an adversarial pass (right approach? assumptions? failure modes? hidden costs? reversibility? strongest argument against?).

Author bias on the plan is real, especially when you wrote it. Try to review as if seeing it for the first time. The file is the checkpoint — once you commit to it on disk, don't go back and rewrite after reading Codex.

### Step 3 — Run the orchestrator

```bash
bash codex-plan-review/bin/run-review.sh <plan-path>
```

The script runs two `codex exec` passes (standard + adversarial) in parallel with `--output-last-message`, applies a wall-clock timeout (default 600s), traps SIGINT to clean up children, and emits a `meta.json` index. It prints the meta.json path to stdout.

Environment overrides:
- `CODEX_REVIEW_MODEL` — pin a model (defaults to `gpt-5.5`); use this if `gpt-5.5` is unavailable on the user's account.
- `CODEX_REVIEW_TIMEOUT` — seconds per pass (default 600).

Exit codes: `0` both passes succeeded, `1` exactly one failed (proceed with the survivor), `2` setup failure (codex CLI missing, plan unreadable), `3` both failed (stop and report), `130` interrupted.

### Step 4 — Read the outputs

Read `meta.json`, then the `standard_md` and `adversarial_md` files it names. The JSONL streams are kept as provenance but you should not need to parse them — `--output-last-message` already extracts the final assistant message into `.md`.

If a pass failed (`std_rc != 0` or `adv_rc != 0`), say so in the final report rather than fabricating output. You can still reconcile with one Codex review plus your analysis, but flag the missing piece explicitly.

### Step 5 — Reconcile and write the final report

Use the output format below. Save to `.codex-review/report-<timestamp>.md` and present inline. **Do not modify the plan.**

Reasoning rules:

- **Agreement** = at least two of the three sources flagged the same issue. Note which.
- **Disagreement** = Codex (one or both) raised something Claude didn't, OR contradicted Claude. Both directions.
- **Codex internal disagreement** = standard and adversarial point in different directions on the same point. *Articulate the tradeoff* rather than picking a side — this category usually marks real design tradeoffs.
- **Open questions** = depend on context none of the three had: user intent, hardware specifics, downstream consumers, prior decisions.

When you and Codex disagree on a substantive technical point, do not default to defending your original view. Lean toward presenting Codex's view fairly — you have author bias on the plan, Codex does not.

## Output format

````markdown
# Codex Cross-Review: <short plan description>

**Plan reviewed:** `<path>`
**Reviewed by:** Claude (independent), Codex GPT-5.5 standard, Codex GPT-5.5 adversarial
**Date:** <ISO date>

## 1. Where all three agree

- **<issue>** — <one sentence> [Claude / Codex-std / Codex-adv]
  - Why it matters: <one sentence>

## 2. Where Codex disagrees with Claude

### Codex raised, Claude missed
- **<issue>** [Codex-std / Codex-adv / both]
  - Codex's point: <fair summary>
  - My take: <agree / partial / disagree, with reasoning>

### Claude raised, Codex didn't
- **<issue>**
  - My point: <summary>
  - Possible reason Codex skipped: <e.g., out of scope of plan text>

### Direct contradictions
- **<issue>**
  - Claude said: <X>
  - Codex said: <not-X>
  - Who I think is right and why. If genuinely unsure, say so.

## 3. Where Codex's two reviews disagree

- **<topic>**
  - Standard view: <summary>
  - Adversarial view: <summary>
  - Tradeoff: <one or two sentences>

## 4. Open questions

- <question requiring user input>

## 5. Raw outputs

- Claude's analysis: `.codex-review/claude-analysis.md`
- Codex standard: `<standard_md from meta.json>`
- Codex adversarial: `<adversarial_md from meta.json>`
- Per-run index: `<meta.json path>`
````

Empty sections keep the header with `*(none)*` underneath. Empty sections are signal too.

## Failure modes to avoid

- **Don't summarize Codex.** The reconciliation is the value.
- **Don't auto-revise the plan.** User decides.
- **Don't skip your own analysis.** Two voices collapses into Codex summary.
- **Don't hide disagreements.** Author bias is real; flag rather than smooth.
- **Don't fabricate Codex output.** Failed run? Say so.
- **Don't write claude-analysis.md after reading Codex.** Independence requires committing to your view before peeking.
