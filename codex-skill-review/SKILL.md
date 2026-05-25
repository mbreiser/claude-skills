---
name: codex-skill-review
description: Get a GPT-5.5 second opinion on an already-drafted Claude Code skill — does the SKILL.md description trigger correctly? Are bin/ scripts robust? Does it duplicate or overlap with sibling skills? Does it earn its place in the suite? Runs both a standard design review and an adversarial pressure-test in parallel via Codex CLI, then reconciles their feedback against Claude's own analysis and surfaces a structured agreement / disagreement / open-questions report. Use whenever the user has DRAFTED a skill (SKILL.md plus any bin/ scripts) and wants outside-model feedback on it, OR is auditing an existing skill for fit, discoverability, or overlap. Triggers include "review this skill," "review this SKILL.md," "review my skill frontmatter," "audit the skill description," "is this skill well-designed?", "does this skill overlap with X?", "Claude isn't invoking my skill," "the skill's permission prompts are noisy," "should this skill exist?". Do NOT use for code review of arbitrary code (use codex-diff-review instead), for reviewing a plan/design before the skill is drafted (use codex-plan-review instead), or to help the user CREATE a skill from scratch (no skill artifact exists yet).
---

# Codex Skill Review

Get a GPT-5.5 second opinion on a Claude Code skill — design quality, description discoverability, sibling overlap, bin/ robustness, failure-mode coverage. Reconcile with your own review. Surface agreements, disagreements, and open questions. Do not auto-fix.

## When this skill applies

The user wants outside-model feedback on a Claude Code skill — typically one they're building, modifying, or auditing for fit. Triggers include "review this skill," "audit the skill description," "is this skill discoverable?", "does this skill duplicate something I already have?", "should I ship this skill?", "is the description too broad / too narrow?". If they want code review of arbitrary code, use `codex-diff-review`. If they want a plan reviewed before writing it, use `codex-plan-review`.

## Reducing permission prompts (one-time setup)

A fresh run triggers 2–4 permission prompts: the `claude-analysis.md` write (Step 2), the orchestrator bash call (Step 3), the final report write (Step 5), and possibly Read calls for skill files in Step 1 if you choose to inspect them outside `.codex-review/`. All go to zero if these entries are added to `~/.claude/settings.json` (apply via `/permissions` or by editing the file):

```jsonc
{
  "permissions": {
    "allow": [
      "Bash(codex --version)",
      "Bash(bash *codex-plan-review/bin/run-review.sh *)",
      "Bash(bash *codex-diff-review/bin/run-review.sh *)",
      "Bash(bash *codex-skill-review/bin/run-review.sh *)",
      "Read(./.codex-review/**)",
      "Write(./.codex-review/**)"
    ]
  }
}
```

This is opt-in: do not modify the user's settings on their behalf. Mention the snippet if the user complains about prompt churn.

## Workflow

### Step 1 — Read the skill yourself

Read the target skill's `SKILL.md`. Read any `bin/` scripts it includes. Skim the SKILL.md files of 2–3 sibling skills in the same parent directory so you understand the surrounding ecosystem and what conventions the repo follows. You can't write a meaningful independent review without knowing what's in the skill and what surrounds it.

### Step 2 — Write your independent review (BEFORE running Codex)

Save your own review to `.codex-review/claude-analysis.md` **before** launching the orchestrator. Same structure as Codex's prompts: description trigger quality, workflow ergonomics, `bin/` script robustness, fit with sibling skills, failure-mode documentation; plus an adversarial pass (does this skill solve a real problem? discoverability false positives/negatives? overlap with existing skills? maintenance cost? strongest argument against shipping?).

Author bias is real when reviewing a skill you (or your sibling agent) just wrote. Try to evaluate as if it landed in the repo from someone else. The file checkpoint exists so you commit to your view before reading Codex.

### Step 3 — Run the orchestrator

```bash
bash codex-skill-review/bin/run-review.sh <skill-path>
```

The script verifies the skill path contains `SKILL.md`, then runs two `codex exec` passes (standard + adversarial) in parallel with `--output-last-message`, applies a wall-clock timeout (default 600s), traps SIGTERM/SIGINT to clean up the entire codex process tree, and emits a `meta.json` index. It prints the meta.json path to stdout.

Environment overrides:
- `CODEX_REVIEW_MODEL` — pin a model (defaults to `gpt-5.5`); use this if `gpt-5.5` is unavailable on the user's account.
- `CODEX_REVIEW_TIMEOUT` — seconds per pass (default 600; must be a positive integer).

Exit codes: `0` both passes succeeded, `1` exactly one failed (proceed with the survivor), `2` setup failure (codex CLI missing, jq missing, bad skill path, not in a git repo, bad timeout), `3` both failed (stop and report), `130` interrupted.

### Step 4 — Read the outputs

Read `meta.json`, then the `standard_md` and `adversarial_md` files it names. The JSONL streams are kept as provenance but you should not need to parse them — `--output-last-message` already extracts the final assistant message into `.md`.

If a pass failed (`std_rc != 0` or `adv_rc != 0`), say so in the final report rather than fabricating output. You can still reconcile with one Codex review plus your analysis, but flag the missing piece explicitly.

### Step 5 — Reconcile and write the final report

Use the output format below. Save to `.codex-review/report-<timestamp>.md` and present inline. **Do not modify the skill.**

Reasoning rules:

- **Agreement** = at least two of three flagged the same issue. Highest confidence.
- **Disagreement** = Codex raised what Claude didn't, or contradicted Claude.
- **Codex internal disagreement** = standard vs adversarial diverge — articulate the tradeoff. For skill review specifically: standard is most useful for "is the implementation right?", adversarial for "should this skill exist at all?" — when they disagree, the tradeoff is usually quality-vs-fit.
- **Severity matters.** Within each section, group by **blocking** (broken triggers, unsafe bin/ code, hard overlap with existing skill), **significant** (UX issues, drift risk, narrow descriptions), **minor** (wording, examples). Don't bury blocking issues under nits.
- **Open questions** = depend on context the reviews didn't have (user's actual workflow, planned future skills, downstream consumers).

When Claude and Codex disagree, present Codex's view at least as fully as your own.

## Output format

````markdown
# Codex Skill Review: <skill name>

**Skill:** `<skill-path>` (SKILL.md + <N> bin/ scripts)
**Reviewed by:** Claude (independent), Codex GPT-5.5 standard, Codex GPT-5.5 adversarial
**Date:** <ISO date>

## 1. Where all three agree

### Blocking
- **<issue>** [Claude / Codex-std / Codex-adv] — `path:line` or section reference
  - <one or two sentences>

### Significant
- ...

### Minor
- ...

## 2. Where Codex disagrees with Claude

### Codex raised, Claude missed
- **<issue>** [Codex-std / Codex-adv / both]
  - Codex's point: <fair summary>
  - My take: <agree / partial / disagree, with reasoning>

### Claude raised, Codex didn't
- **<issue>**
  - My point: <summary>
  - Possible reason Codex skipped: <e.g., out of scope of the prompt>

### Direct contradictions
- **<issue>**
  - Claude said: <X>
  - Codex said: <not-X>
  - Who I think is right and why. If genuinely unsure, say so.

## 3. Where Codex's two reviews disagree

- **<topic>**
  - Standard view: <quality framing>
  - Adversarial view: <fit/existence framing>
  - Tradeoff: <one or two sentences>

## 4. Open questions

- <question requiring user input — e.g., "do you actually hit this trigger in practice?">

## 5. Raw outputs

- Claude's analysis: `.codex-review/claude-analysis.md`
- Codex standard: `<standard_md from meta.json>`
- Codex adversarial: `<adversarial_md from meta.json>`
- Per-run index: `<meta.json path>`
````

Empty sections keep the header with `*(none)*` underneath.

## Failure modes to avoid

- **Don't summarize Codex.** Reconciliation is the value.
- **Don't auto-edit the skill.** Even unanimous agreement on a fix doesn't authorize the change.
- **Don't skip your own review.** Three voices is the point.
- **Don't bury blocking issues.** Broken triggers, unsafe `bin/` code, or hard sibling overlap → top of section 1 or 2 with explicit "Blocking" tagging.
- **Don't fabricate Codex output.** Failed run? Say so.
- **Don't dismiss the adversarial pass.** "Should this skill exist?" is the question plan-review and diff-review can't answer for an already-written skill — that's why this skill exists. If the adversarial pass says "the user probably doesn't need this," surface it; don't smooth it.
- **Don't write claude-analysis.md after reading Codex.** Independence requires committing to your view before peeking.
