#!/bin/bash
# Orchestrate a dual Codex review of an implementation plan.
#
# Usage:    bash run-review.sh <plan-path>
# Env vars: CODEX_REVIEW_MODEL    (default: gpt-5.5)
#           CODEX_REVIEW_TIMEOUT  seconds wall-clock cap per pass (default: 600)
#
# On success, prints the path to meta.json on stdout. The caller reads
# meta.json to find standard.md, adversarial.md, and per-pass exit codes.
#
# Exit codes:
#   0    both codex passes succeeded
#   1    exactly one codex pass failed (caller can still produce a report)
#   2    setup failure (codex CLI missing, plan unreadable, bad args)
#   3    both codex passes failed (unrecoverable)
#   130  interrupted (SIGINT/SIGTERM)

set -eu

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <plan-path>" >&2
  exit 2
fi
PLAN_PATH="$1"

if [[ ! -r "$PLAN_PATH" ]]; then
  echo "ERROR: cannot read plan at $PLAN_PATH" >&2
  exit 2
fi

if ! command -v codex > /dev/null 2>&1; then
  echo "ERROR: codex CLI not found in PATH" >&2
  exit 2
fi
if ! command -v jq > /dev/null 2>&1; then
  echo "ERROR: jq not found in PATH (needed for meta.json generation)" >&2
  exit 2
fi
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "ERROR: not in a git repository (codex review expects the cwd to be the repo the plan applies to)" >&2
  exit 2
fi

MODEL="${CODEX_REVIEW_MODEL:-gpt-5.5}"
TIMEOUT_SECS="${CODEX_REVIEW_TIMEOUT:-600}"
if ! [[ "$TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: CODEX_REVIEW_TIMEOUT must be a positive integer (got '$TIMEOUT_SECS')" >&2
  exit 2
fi
TS=$(date +%Y%m%d-%H%M%S)
RUN_DIR=".codex-review/codex-plan-review-$TS-$$"
mkdir -p "$RUN_DIR"

STD_TEMPLATE=$(cat <<'EOF'
You are reviewing an implementation plan for code that has not yet been written. The plan is at `__PLAN_PATH__` (relative to the current working directory, or absolute). The repository the plan applies to is the current working directory; you have read-only access.

Read the plan carefully. Then read enough of the existing repository to understand context — directory structure, files the plan names explicitly, surrounding code the plan would touch.

Produce a focused review covering:

1. **Correctness of the proposed approach.** Will the plan, if implemented as described, achieve the stated goal? Logical errors? Incorrect assumptions about libraries / APIs / hardware? Missing steps?

2. **Completeness.** What is missing that a careful implementer would need? Edge cases not addressed? Failure modes not handled? Tests not specified?

3. **Fit with the existing codebase.** Does the plan respect conventions, patterns, and constraints of the code it would live in? Duplicates work done elsewhere? Conflicts with anything?

4. **Risk areas.** Anything likely to cause subtle bugs, performance problems, or maintenance pain later? Be specific — "concurrency is hard" is not useful; "the proposed lock ordering can deadlock if X and Y are called from different threads" is.

5. **Specific suggested changes.** Concrete, actionable items.

Format as markdown with these five sections as level-2 headers. Reference specific file paths or line numbers from existing code where relevant. Be direct and specific. Avoid hedging language.

If the plan is genuinely good and you have little to add, say so explicitly rather than padding.
EOF
)

ADV_TEMPLATE=$(cat <<'EOF'
You are reviewing an implementation plan adversarially. Your job is to pressure-test the *design choice itself*, not to find bugs in the proposed steps. The plan is at `__PLAN_PATH__`; the repository it applies to is the current working directory, read-only.

A standard reviewer asks "does this plan correctly do the thing it sets out to do?" — that is not your job. Your job is "is this the right thing to do at all?"

Read the plan and enough surrounding code for context. Then produce a review covering:

1. **Is the chosen approach the right one?** What alternatives exist? Why might one be better? Be concrete: name the alternative, explain when it would win, what the current plan trades away by not choosing it.

2. **What assumptions is the plan making, and which might be wrong?** List the load-bearing ones. For each: what happens if it is wrong? How would we even know it is wrong?

3. **Failure modes the plan does not address.** Failure modes of the *design*, not bugs. What at 10x or 100x scale? Dependency unavailable? Inputs malformed in unanticipated ways? Multiple of these at once?

4. **Hidden costs.** Maintenance burden, debugging difficulty, performance ceiling, lock-in, opportunity cost of *not* doing something else.

5. **Reversibility.** If wrong six months from now, how hard to undo? One-way door? If so, is it being treated like one?

6. **The strongest argument against this plan.** Spend at least a few sentences making the strongest case against. Steelman the opposition. If you cannot make a compelling case against, say so — that is signal.

Format as markdown with these six sections as level-2 headers. Be direct. Avoid politeness inflation — soft adversarial review is worse than none.

If after honest pressure-testing the plan is sound, say so. Adversarial reviewing is not about always finding fault; it is about always *trying* to, and being honest about what you find.
EOF
)

STD_PROMPT="${STD_TEMPLATE//__PLAN_PATH__/$PLAN_PATH}"
ADV_PROMPT="${ADV_TEMPLATE//__PLAN_PATH__/$PLAN_PATH}"

# Enable job control so each backgrounded codex gets its own process group.
# This lets cleanup() kill the whole tree (codex CLI wrapper + spawned binary)
# via `kill -KILL -$PID` rather than just the direct child.
set -m

codex exec \
  --sandbox read-only \
  --model "$MODEL" \
  -c model_reasoning_effort='"high"' \
  --json \
  --output-last-message "$RUN_DIR/standard.md" \
  "$STD_PROMPT" \
  > "$RUN_DIR/standard.jsonl" 2> "$RUN_DIR/standard.stderr" &
STD_PID=$!

codex exec \
  --sandbox read-only \
  --model "$MODEL" \
  -c model_reasoning_effort='"high"' \
  --json \
  --output-last-message "$RUN_DIR/adversarial.md" \
  "$ADV_PROMPT" \
  > "$RUN_DIR/adversarial.jsonl" 2> "$RUN_DIR/adversarial.stderr" &
ADV_PID=$!

(
  sleep "$TIMEOUT_SECS"
  kill -TERM -- "-$STD_PID" "-$ADV_PID" 2>/dev/null
  sleep 5
  kill -KILL -- "-$STD_PID" "-$ADV_PID" 2>/dev/null
) &
WATCHDOG_PID=$!

cleanup() {
  kill -KILL -- "-$STD_PID" "-$ADV_PID" 2>/dev/null || true
  kill -KILL "$WATCHDOG_PID" 2>/dev/null || true
  exit 130
}
trap cleanup INT TERM

set +e
wait "$STD_PID"; STD_RC=$?
wait "$ADV_PID"; ADV_RC=$?
set -e

kill -TERM "$WATCHDOG_PID" 2>/dev/null || true
wait "$WATCHDOG_PID" 2>/dev/null || true

if [[ $STD_RC -eq 0 && ! -s "$RUN_DIR/standard.md" ]]; then
  STD_RC=124
fi
if [[ $ADV_RC -eq 0 && ! -s "$RUN_DIR/adversarial.md" ]]; then
  ADV_RC=124
fi

TS_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
jq -n \
  --arg timestamp "$TS_ISO" \
  --arg mode "plan" \
  --arg plan_path "$PLAN_PATH" \
  --arg run_dir "$RUN_DIR" \
  --arg standard_md "$RUN_DIR/standard.md" \
  --arg adversarial_md "$RUN_DIR/adversarial.md" \
  --arg standard_jsonl "$RUN_DIR/standard.jsonl" \
  --arg adversarial_jsonl "$RUN_DIR/adversarial.jsonl" \
  --arg model "$MODEL" \
  --argjson timeout_secs "$TIMEOUT_SECS" \
  --argjson std_rc "$STD_RC" \
  --argjson adv_rc "$ADV_RC" \
  '{timestamp:$timestamp, mode:$mode, plan_path:$plan_path, run_dir:$run_dir, standard_md:$standard_md, adversarial_md:$adversarial_md, standard_jsonl:$standard_jsonl, adversarial_jsonl:$adversarial_jsonl, model:$model, timeout_secs:$timeout_secs, std_rc:$std_rc, adv_rc:$adv_rc}' \
  > "$RUN_DIR/meta.json"

echo "$RUN_DIR/meta.json"

if [[ $STD_RC -ne 0 && $ADV_RC -ne 0 ]]; then
  echo "ERROR: both codex passes failed (std=$STD_RC adv=$ADV_RC, model=$MODEL). Set CODEX_REVIEW_MODEL to override." >&2
  exit 3
fi
if [[ $STD_RC -ne 0 || $ADV_RC -ne 0 ]]; then
  echo "WARNING: one codex pass failed (std=$STD_RC adv=$ADV_RC, model=$MODEL). Caller can still reconcile from the surviving pass." >&2
  exit 1
fi
exit 0
