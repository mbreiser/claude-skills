#!/bin/bash
# Orchestrate a dual Codex review of a code diff.
#
# Usage:    bash run-review.sh <base-ref-or-flag>
#   <base-ref-or-flag>:
#     main, develop, HEAD~3, etc.   → git diff <ref>...HEAD
#     --staged                       → git diff --staged
#     --working                      → git diff HEAD (working tree vs HEAD; includes staged + unstaged)
#
# Env vars: CODEX_REVIEW_MODEL    (default: gpt-5.5)
#           CODEX_REVIEW_TIMEOUT  seconds wall-clock cap per pass (default: 600)
#
# On success, prints the path to meta.json on stdout.
#
# Exit codes:
#   0    both codex passes succeeded
#   1    exactly one codex pass failed (caller can still produce a report)
#   2    setup failure (codex CLI missing, not in a git repo, empty diff)
#   3    both codex passes failed (unrecoverable)
#   130  interrupted (SIGINT/SIGTERM)

set -eu

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <base-ref|--staged|--working>" >&2
  exit 2
fi
BASE_REF="$1"

if ! command -v codex > /dev/null 2>&1; then
  echo "ERROR: codex CLI not found in PATH" >&2
  exit 2
fi
if ! command -v jq > /dev/null 2>&1; then
  echo "ERROR: jq not found in PATH (needed for meta.json generation)" >&2
  exit 2
fi
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "ERROR: not in a git repository" >&2
  exit 2
fi

MODEL="${CODEX_REVIEW_MODEL:-gpt-5.5}"
TIMEOUT_SECS="${CODEX_REVIEW_TIMEOUT:-600}"
if ! [[ "$TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: CODEX_REVIEW_TIMEOUT must be a positive integer (got '$TIMEOUT_SECS')" >&2
  exit 2
fi
TS=$(date +%Y%m%d-%H%M%S)
RUN_DIR=".codex-review/codex-diff-review-$TS-$$"
mkdir -p "$RUN_DIR"

DIFF_PATH="$RUN_DIR/diff.patch"
FILES_PATH="$RUN_DIR/files.txt"

case "$BASE_REF" in
  --staged)
    git diff --staged > "$DIFF_PATH"
    git diff --staged --name-only > "$FILES_PATH"
    DIFF_DESC="staged changes"
    ;;
  --working)
    git diff HEAD > "$DIFF_PATH"
    git diff HEAD --name-only > "$FILES_PATH"
    DIFF_DESC="working tree vs HEAD"
    ;;
  *)
    if ! git rev-parse --verify "$BASE_REF" > /dev/null 2>&1; then
      echo "ERROR: '$BASE_REF' is not a valid git ref" >&2
      exit 2
    fi
    git diff "$BASE_REF...HEAD" > "$DIFF_PATH"
    git diff "$BASE_REF...HEAD" --name-only > "$FILES_PATH"
    DIFF_DESC="$BASE_REF...HEAD"
    ;;
esac

if [[ ! -s "$DIFF_PATH" ]]; then
  echo "ERROR: diff is empty ($DIFF_DESC); nothing to review" >&2
  exit 2
fi

DIFF_LINES=$(wc -l < "$DIFF_PATH" | tr -d ' ')
FILE_COUNT=$(wc -l < "$FILES_PATH" | tr -d ' ')

STD_TEMPLATE=$(cat <<'EOF'
You are reviewing a code change. The diff is at `__DIFF_PATH__` (relative to the current working directory) and represents `__DIFF_DESC__`. Changed files are listed in `__FILES_PATH__`. The repository is the current working directory; you have read-only access and can read any file you need for context.

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

If the change is genuinely good, say so. Do not manufacture nits to look thorough.
EOF
)

ADV_TEMPLATE=$(cat <<'EOF'
You are reviewing a code change adversarially. Your job is to pressure-test the *design and approach*, not just to find bugs in lines. The diff is at `__DIFF_PATH__` representing `__DIFF_DESC__`. Changed files are listed in `__FILES_PATH__`. The repository is the current working directory, read-only.

A standard reviewer asks "is this code correct?" — that is not your job. Your job is "should this code exist in this form?"

Read the diff and surrounding code in each changed file. Then produce a review covering:

1. **Was this the right approach?** Alternative implementations? Why might one be better? Be concrete: name the alternative, when it would win, what the current implementation trades away.

2. **What assumptions does this code make, and which might be wrong?** Inputs, callers, downstream consumers, environment, hardware, runtime. Load-bearing ones. For each: what if it is wrong? How would we know?

3. **Failure modes the code does not address.** Failure modes of the *design*, not just untested branches. 10x or 100x scale? Partial failure of dependencies? Adversarial or malformed inputs? Multiple at once?

4. **Hidden costs.** Maintenance burden, debugging difficulty, performance ceiling, coupling, lock-in. Cite locations.

5. **Reversibility.** Wrong six months from now — how hard to undo? Public APIs added, schema changes, persisted state formats — one-way doors? Treated like one?

6. **Race conditions, data-loss risks, rollback risks, reliability risks.** Specifically named so you do not skip them. Concurrency bugs hide well; look harder than you think.

7. **The strongest argument against merging.** Spend a few sentences making the strongest case against. Steelman the opposition. If you cannot, say so — that is information.

Format as markdown with these seven sections as level-2 headers. Cite specific paths and line numbers. Be direct. Avoid politeness inflation.

If after honest pressure-testing the change is sound, say so plainly.
EOF
)

STD_PROMPT="${STD_TEMPLATE//__DIFF_PATH__/$DIFF_PATH}"
STD_PROMPT="${STD_PROMPT//__DIFF_DESC__/$DIFF_DESC}"
STD_PROMPT="${STD_PROMPT//__FILES_PATH__/$FILES_PATH}"
ADV_PROMPT="${ADV_TEMPLATE//__DIFF_PATH__/$DIFF_PATH}"
ADV_PROMPT="${ADV_PROMPT//__DIFF_DESC__/$DIFF_DESC}"
ADV_PROMPT="${ADV_PROMPT//__FILES_PATH__/$FILES_PATH}"

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
  --arg mode "diff" \
  --arg base_ref "$BASE_REF" \
  --arg diff_desc "$DIFF_DESC" \
  --arg diff_path "$DIFF_PATH" \
  --arg files_path "$FILES_PATH" \
  --argjson diff_lines "$DIFF_LINES" \
  --argjson file_count "$FILE_COUNT" \
  --arg run_dir "$RUN_DIR" \
  --arg standard_md "$RUN_DIR/standard.md" \
  --arg adversarial_md "$RUN_DIR/adversarial.md" \
  --arg standard_jsonl "$RUN_DIR/standard.jsonl" \
  --arg adversarial_jsonl "$RUN_DIR/adversarial.jsonl" \
  --arg model "$MODEL" \
  --argjson timeout_secs "$TIMEOUT_SECS" \
  --argjson std_rc "$STD_RC" \
  --argjson adv_rc "$ADV_RC" \
  '{timestamp:$timestamp, mode:$mode, base_ref:$base_ref, diff_desc:$diff_desc, diff_path:$diff_path, files_path:$files_path, diff_lines:$diff_lines, file_count:$file_count, run_dir:$run_dir, standard_md:$standard_md, adversarial_md:$adversarial_md, standard_jsonl:$standard_jsonl, adversarial_jsonl:$adversarial_jsonl, model:$model, timeout_secs:$timeout_secs, std_rc:$std_rc, adv_rc:$adv_rc}' \
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
