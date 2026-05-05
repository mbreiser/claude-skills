---
name: codex-diff-review
description: Get a GPT-5.5 second opinion on a code change (working tree or branch diff) before commit or merge. Runs both a standard code review and an adversarial pressure-test in parallel via Codex CLI, then reconciles their feedback against Claude's own analysis and surfaces a structured agreement / disagreement / open-questions report. Use this skill whenever the user asks for a "second opinion," "Codex review," "GPT-5 review," "cross-check," "pre-commit review," or "sanity check" on code that has already been written — even if Codex isn't named explicitly. Also use when the user mentions reviewing a diff against main, a feature branch, or staged changes. Do NOT use for plans or designs that haven't been implemented (use codex-plan-review instead).
---

# Codex Diff Review

Get a GPT-5.5 second opinion on a code change, then reconcile it with your own analysis. Surface agreements, disagreements, and open questions. Do not auto-revise.

## When this skill applies

The user wants outside-model feedback on **already-written code** — staged changes, unstaged working tree, or branch diff. Triggers include "Codex review the diff," "cross-check before I commit," "pre-commit second opinion," "pressure-test what I just implemented." If no code exists yet, use `codex-plan-review` instead.

## Defining "the diff"

Ask one targeted question if ambiguous, otherwise infer:

- "review what I just did" / context implies recent work → `git diff HEAD` or `git diff <branch>...HEAD`
- "against main" → `git diff main...HEAD`
- Named base ref → `git diff <ref>...HEAD`
- Staged but uncommitted → `git diff --staged`

Save the diff to `.codex-review/diff-<timestamp>.patch` and changed files list to `.codex-review/files.txt`. If diff is empty, stop.

## Workflow

### Step 1 — Stage the diff

Run `git diff`, write to patch file, capture file list. Ensure `.codex-review/` is in `.gitignore`.

### Step 2 — Size check

- **< 1000 changed lines** — review whole diff in one shot.
- **1000–5000** — same, but warn the user about time and token cost.
- **> 5000** — chunk by file group (directory or logical area). Run all standard chunks in parallel, all adversarial chunks in parallel, then aggregate before reconciling. Tell the user.

### Step 3 — Confirm Codex is set up

Run `codex --version`. If it fails, tell the user and stop.

### Step 4 — Launch both reviews in parallel

Write the prompts (templates below) to `.codex-review/standard-prompt.txt` and `.codex-review/adversarial-prompt.txt` with `{DIFF_PATH}`, `{BASE_REF}`, `{FILES}` substituted. Then:

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

Codex inherits the working directory and reads source files for context as needed. `--sandbox read-only` prevents modifications. If `--model gpt-5.5` fails, fall back to `gpt-5.4` and tell the user.

### Step 5 — Write your own review (in parallel)

Produce your own review of the diff before peeking at Codex output. Save to `.codex-review/claude-analysis.md`. Author bias on code is even stronger than on plans — counteract deliberately. Try to review as if seeing it for the first time.

### Step 6 — Wait for Codex, parse outputs

Wait for both background jobs. Parse final assistant messages from each `.jsonl`. Failed run? Report it, don't fabricate. Proceed with one + your analysis if needed, flagging the missing piece.

### Step 7 — Reconcile

Use the output template below. Reasoning rules:

- **Agreement** = at least two of three flagged the same issue. Highest confidence.
- **Disagreement** = Codex raised what Claude didn't, or contradicted Claude.
- **Codex internal disagreement** = standard vs adversarial diverge — articulate the tradeoff.
- **Severity matters.** Within each section, group by **blocking** (correctness, security, data loss), **significant** (subtle bugs, design issues, maintainability), **minor** (style, naming). Don't bury blocking issues under nits.
- **Open questions** = depend on context the reviews didn't have.

When Claude and Codex disagree, present Codex's view at least as fully as your own.

### Step 8 — Present the report

Write inline. Save to `.codex-review/report-<timestamp>.md`. **Do not modify the code.**

## Output format

````markdown
# Codex Cross-Review: <short diff description>

**Diff:** `<base>...HEAD` (<N> files, +<additions>/-<deletions>)
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
- Codex standard: `.codex-review/standard-output.jsonl`
- Codex adversarial: `.codex-review/adversarial-output.jsonl`
- Diff: `.codex-review/diff-<timestamp>.patch`
````

Empty sections keep the header with `*(none)*` underneath.

## Standard diff review prompt (substitute `{DIFF_PATH}`, `{BASE_REF}`, `{FILES}`)

```
You are reviewing a code change. The diff is at `{DIFF_PATH}` (relative to the current working directory) and represents `{BASE_REF}...HEAD`. Changed files are listed in `{FILES}`. The repository is the current working directory; you have read-only access and can read any file you need for context.

Read the diff. Then read surrounding code in each changed file — a diff in isolation is rarely enough.

Produce a focused review covering:

1. **Correctness.** Bugs? Logic errors? Off-by-ones? Incorrect API usage? Race conditions? Unhandled error paths? Cite file paths and line numbers.

2. **Tests.** Adequate for what changed? Untested changes? Tests passing for the wrong reason? Missing edge cases?

3. **Fit with existing code.** Conventions and patterns respected? Duplicates functionality elsewhere? Dead code, unused imports, stale comments?

4. **Risk and reliability.** Performance regressions, memory issues, security concerns, operational risks? Pay attention to error handling, resource cleanup, input validation, concurrency, external-dependency assumptions.

5. **Specific suggested changes.** Concrete, actionable items with locations.

Format as markdown with these five sections as level-2 headers. Within each section, group items by severity:

- **Blocking** — correctness, security, data loss, anything that should not ship.
- **Significant** — subtle bugs, design concerns, maintainability.
- **Minor** — style, naming, small cleanups.

Cite specific file paths and line numbers (`path/to/file.ext:42`). Be direct. Avoid hedging.

If the change is genuinely good, say so. Don't manufacture nits to look thorough.
```

## Adversarial diff review prompt (substitute `{DIFF_PATH}`, `{BASE_REF}`, `{FILES}`)

```
You are reviewing a code change adversarially. Your job is to pressure-test the *design and approach*, not just to find bugs in lines. The diff is at `{DIFF_PATH}` representing `{BASE_REF}...HEAD`. Changed files are listed in `{FILES}`. The repository is the current working directory, read-only.

A standard reviewer asks "is this code correct?" — that's not your job. Your job is "should this code exist in this form?"

Read the diff and surrounding code in each changed file. Then produce a review covering:

1. **Was this the right approach?** Alternative implementations? Why might one be better? Be concrete: name the alternative, when it would win, what the current implementation trades away.

2. **What assumptions does this code make, and which might be wrong?** Inputs, callers, downstream consumers, environment, hardware, runtime. Load-bearing ones. For each: what if it's wrong? How would we know?

3. **Failure modes the code doesn't address.** Failure modes of the *design*, not just untested branches. 10x or 100x scale? Partial failure of dependencies? Adversarial or malformed inputs? Multiple at once?

4. **Hidden costs.** Maintenance burden, debugging difficulty, performance ceiling, coupling, lock-in. Cite locations.

5. **Reversibility.** Wrong six months from now — how hard to undo? Public APIs added, schema changes, persisted state formats — one-way doors? Treated like one?

6. **Race conditions, data-loss risks, rollback risks, reliability risks.** Specifically named so you don't skip them. Concurrency bugs hide well; look harder than you think.

7. **The strongest argument against merging.** Spend a few sentences making the strongest case against. Steelman the opposition. If you can't, say so — that's information.

Format as markdown with these seven sections as level-2 headers. Cite specific paths and line numbers. Be direct. Avoid politeness inflation.

If after honest pressure-testing the change is sound, say so plainly.
```

## Failure modes to avoid

- **Don't summarize Codex.** Reconciliation is the value.
- **Don't auto-fix.** Even unanimous agreement on a fix doesn't authorize the change.
- **Don't skip your own review.** Three voices is the point.
- **Don't bury blocking issues.** Correctness or security flagged anywhere → top of section 1 or 2 with explicit "Blocking" tagging.
- **Don't fabricate Codex output.** Failed run? Say so.
- **Don't dismiss Codex on "it doesn't have context."** Say "here's what Codex said, here's the context that may change the picture" — not silent omission.
