---
name: codex-plan-review
description: Get a GPT-5.5 second opinion on an implementation plan or design proposal before writing code. Runs both a standard review and an adversarial pressure-test in parallel via Codex CLI, then reconciles their feedback against Claude's own analysis and surfaces a structured agreement / disagreement / open-questions report. Use this skill whenever the user asks for a "second opinion," "Codex review," "GPT-5 review," "cross-check," "pressure-test," or "sanity check" on a plan, design, approach, or proposed implementation — even if Codex isn't named explicitly. Also use when the user has just written or accepted a plan and is about to start implementation. Do NOT use for already-written code (use codex-diff-review instead).
---

# Codex Plan Review

Get a GPT-5.5 second opinion on an implementation plan, then reconcile it with your own analysis. Surface agreements, disagreements, and open questions for the user to decide on. Do not auto-revise the plan.

## When this skill applies

The user wants outside-model feedback on a plan or design **before** code is written. Triggers include "Codex second opinion," "cross-check this design," "sanity-check the approach," "pressure-test this plan." If code already exists, use `codex-diff-review` instead.

## What "the plan" is

Whatever document or set of bullet points is on the table — a markdown file the user wrote, a plan you just produced in this conversation, a spec they pasted, or a goal + proposed approach. If it's not already in a file, write it to `.codex-review/plan-<timestamp>.md` at the repo root before invoking Codex. `codex exec` reads its prompt from the command line and the working tree from `cwd`; pointing it at a file avoids quoting issues.

## Workflow

### Step 1 — Stage the plan

Note the file path if it exists. Otherwise write the plan to `.codex-review/plan-<timestamp>.md`. Ensure `.codex-review/` is in `.gitignore`.

### Step 2 — Confirm Codex is set up

Run `codex --version`. If it fails, tell the user and stop — don't install without permission.

### Step 3 — Launch both reviews in parallel

Write the two prompts (templates below) to `.codex-review/standard-prompt.txt` and `.codex-review/adversarial-prompt.txt` with `{PLAN_PATH}` substituted. Then:

```bash
codex exec \
  --sandbox read-only \
  --model gpt-5.5 \
  -c model_reasoning_effort='"high"' \
  --json \
  "$(cat .codex-review/standard-prompt.txt)" \
  > .codex-review/standard-output.jsonl 2>&1 &

codex exec \
  --sandbox read-only \
  --model gpt-5.5 \
  -c model_reasoning_effort='"high"' \
  --json \
  "$(cat .codex-review/adversarial-prompt.txt)" \
  > .codex-review/adversarial-output.jsonl 2>&1 &
```

If `--model gpt-5.5` fails (account doesn't have access), fall back to `gpt-5.4` and tell the user. Don't silently downgrade.

### Step 4 — Write your own analysis (in parallel with Codex)

While Codex runs, produce **your own** review structured the same way the prompts ask for. Don't peek at Codex output before completing your own pass — independence matters. Save to `.codex-review/claude-analysis.md`.

### Step 5 — Wait for Codex, parse outputs

Wait for both background jobs. Parse the final assistant message from each `.jsonl` stream. If either run failed (non-zero exit, error event, timeout > 10 min), report the failure — do not fabricate output. You can still reconcile with one Codex review + your analysis, but flag the missing piece.

### Step 6 — Reconcile

Use the output template below. Reasoning rules:

- **Agreement** = at least two of the three sources flagged the same issue. Note which.
- **Disagreement** = Codex (one or both) raised something Claude didn't, OR contradicted Claude. Both directions.
- **Codex internal disagreement** = standard and adversarial point in different directions on the same point. *Articulate the tradeoff* rather than picking a side — this category usually marks real design tradeoffs.
- **Open questions** = depend on context none of the three had: user intent, hardware specifics, downstream consumers, prior decisions.

When you and Codex disagree on a substantive technical point, do not default to defending your original view. Lean toward presenting Codex's view fairly — you have author bias on the plan, Codex doesn't.

### Step 7 — Present the report

Write the report inline to the conversation. Save to `.codex-review/report-<timestamp>.md`. **Do not modify the plan.**

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
- Codex standard: `.codex-review/standard-output.jsonl`
- Codex adversarial: `.codex-review/adversarial-output.jsonl`
````

Empty sections keep the header with `*(none)*` underneath. Empty sections are signal too.

## Standard plan review prompt (substitute `{PLAN_PATH}`)

```
You are reviewing an implementation plan for code that has not yet been written. The plan is at `{PLAN_PATH}` (relative to the current working directory). The repository the plan applies to is the current working directory; you have read-only access.

Read the plan carefully. Then read enough of the existing repository to understand context — directory structure, files the plan names explicitly, surrounding code the plan would touch.

Produce a focused review covering:

1. **Correctness of the proposed approach.** Will the plan, if implemented as described, achieve the stated goal? Logical errors? Incorrect assumptions about libraries / APIs / hardware? Missing steps?

2. **Completeness.** What's missing that a careful implementer would need? Edge cases not addressed? Failure modes not handled? Tests not specified?

3. **Fit with the existing codebase.** Does the plan respect conventions, patterns, and constraints of the code it would live in? Duplicates work done elsewhere? Conflicts with anything?

4. **Risk areas.** Anything likely to cause subtle bugs, performance problems, or maintenance pain later? Be specific — "concurrency is hard" is not useful; "the proposed lock ordering can deadlock if X and Y are called from different threads" is.

5. **Specific suggested changes.** Concrete, actionable items.

Format as markdown with these five sections as level-2 headers. Reference specific file paths or line numbers from existing code where relevant. Be direct and specific. Avoid hedging language.

If the plan is genuinely good and you have little to add, say so explicitly rather than padding.
```

## Adversarial plan review prompt (substitute `{PLAN_PATH}`)

```
You are reviewing an implementation plan adversarially. Your job is to pressure-test the *design choice itself*, not to find bugs in the proposed steps. The plan is at `{PLAN_PATH}` (relative to the current working directory); the repository it applies to is the current working directory, read-only.

A standard reviewer asks "does this plan correctly do the thing it sets out to do?" — that's not your job. Your job is "is this the right thing to do at all?"

Read the plan and enough surrounding code for context. Then produce a review covering:

1. **Is the chosen approach the right one?** What alternatives exist? Why might one be better? Be concrete: name the alternative, explain when it would win, what the current plan trades away by not choosing it.

2. **What assumptions is the plan making, and which might be wrong?** List the load-bearing ones. For each: what happens if it's wrong? How would we even know it's wrong?

3. **Failure modes the plan doesn't address.** Failure modes of the *design*, not bugs. What at 10x or 100x scale? Dependency unavailable? Inputs malformed in unanticipated ways? Multiple of these at once?

4. **Hidden costs.** Maintenance burden, debugging difficulty, performance ceiling, lock-in, opportunity cost of *not* doing something else.

5. **Reversibility.** If wrong six months from now, how hard to undo? One-way door? If so, is it being treated like one?

6. **The strongest argument against this plan.** Spend at least a few sentences making the strongest case against. Steelman the opposition. If you can't make a compelling case against, say so — that's signal.

Format as markdown with these six sections as level-2 headers. Be direct. Avoid politeness inflation — soft adversarial review is worse than none.

If after honest pressure-testing the plan is sound, say so. Adversarial reviewing isn't about always finding fault; it's about always *trying* to, and being honest about what you find.
```

## Failure modes to avoid

- **Don't summarize Codex.** The reconciliation is the value.
- **Don't auto-revise the plan.** User decides.
- **Don't skip your own analysis.** Two voices collapses into Codex summary.
- **Don't hide disagreements.** Author bias is real; flag rather than smooth.
- **Don't fabricate Codex output.** Failed run? Say so.
