#!/bin/bash
# Orchestrate a dual Codex review of a Claude Code skill directory.
#
# Usage:    bash run-review.sh <skill-path>
#   <skill-path>: path to a skill directory (must contain SKILL.md)
#
# Env vars: CODEX_REVIEW_MODEL    (default: gpt-5.5)
#           CODEX_REVIEW_TIMEOUT  seconds wall-clock cap per pass (default: 600)
#
# On success, prints the path to meta.json on stdout.
#
# Exit codes:
#   0    both codex passes succeeded
#   1    exactly one codex pass failed (caller can still produce a report)
#   2    setup failure (codex CLI missing, jq missing, bad skill path, not in git repo)
#   3    both codex passes failed (unrecoverable)
#   130  interrupted (SIGINT/SIGTERM)

set -eu

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <skill-path>" >&2
  exit 2
fi
SKILL_PATH="${1%/}"

if [[ ! -d "$SKILL_PATH" ]]; then
  echo "ERROR: $SKILL_PATH is not a directory" >&2
  exit 2
fi
if [[ ! -r "$SKILL_PATH/SKILL.md" ]]; then
  echo "ERROR: $SKILL_PATH/SKILL.md not found (this does not look like a Claude Code skill)" >&2
  exit 2
fi
SKILL_NAME="$(basename "$SKILL_PATH")"

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
RUN_DIR=".codex-review/codex-skill-review-$TS-$$"
mkdir -p "$RUN_DIR"

STD_TEMPLATE=$(cat <<'EOF'
You are reviewing a Claude Code skill. A skill is a directory containing SKILL.md (with YAML frontmatter declaring name and description, plus a markdown body describing the workflow) and optionally bin/ scripts and reference docs. The skill being reviewed is at `__SKILL_PATH__` (relative to the current working directory). The repository it lives in is the current working directory; you have read-only access. Sibling skill directories at the same level are visible and relevant for overlap detection.

Read `__SKILL_PATH__/SKILL.md` carefully. Then read any bin/ scripts the skill includes. Then sample at least 2-3 sibling skills SKILL.md files to understand the repo conventions and surrounding skill ecosystem.

Produce a focused review covering:

1. **Description trigger quality.** The frontmatter description is what Claude uses to decide whether to invoke this skill. Is it specific enough that Claude picks it for the right requests? Does it list concrete trigger phrases? Does it explicitly NOT-trigger on adjacent use cases (a Do-NOT-use clause)? Are there sibling skills whose descriptions could collide?

2. **Workflow ergonomics.** Read the workflow section of SKILL.md. Count tool calls per invocation. Count permission prompts (Bash, Write, Read in non-allowlisted paths) the user would face. Are steps clearly numbered? Is the ordering correct (later steps do not depend on outputs earlier steps have not yet produced)? Are independence checkpoints (like write-your-analysis-before-peeking) preserved where they matter?

3. **bin/ script robustness.** If the skill has bin/ scripts: are they `set -eu`? Quoted properly? Validate inputs? Trap signals and clean up children on interrupt? Use timeouts where appropriate? Exit codes meaningful and documented? Dependencies checked at startup with clear error messages?

4. **Fit with sibling skills.** Does this skill duplicate functionality already present in another skill in this repo? Does its description compete with another skill triggers? Does it follow the repo conventions (per-skill bin/ pattern, artifact dir layout) or break them?

5. **Failure-mode documentation.** Does SKILL.md tell Claude what to do when things go wrong (failed pass, missing dependency, empty input, partial output)? Are Failure-modes-to-avoid footers present where appropriate?

Format as markdown with these five sections as level-2 headers. Within each section, group items by severity:

- **Blocking** — would cause incorrect skill behavior, missed triggers, broken bin/ scripts, or unsafe code.
- **Significant** — meaningful design issues, drift risk, UX problems.
- **Minor** — wording, examples, naming.

Cite specific file paths and line numbers (`path/to/file.ext:42`). Be direct. Avoid hedging.

If the change is genuinely good, say so. Do not manufacture nits to look thorough.
EOF
)

ADV_TEMPLATE=$(cat <<'EOF'
You are reviewing a Claude Code skill adversarially. Your job is to pressure-test whether this skill should EXIST IN THIS FORM as part of the user skill suite — not whether the implementation is correct. The skill is at `__SKILL_PATH__`; the repository is the current working directory, read-only.

A standard reviewer asks: is this skill well-built. That is not your job. Your job is: should this skill be part of the suite, in this shape, given the user actual workflow inferred from the rest of the repo.

Read `__SKILL_PATH__/SKILL.md`, any bin/ scripts, and at least 2-3 sibling skills SKILL.md files to understand the surrounding ecosystem. Then produce a review covering:

1. **Does this skill solve a real problem?** Or one the user might never hit? Based on what you can infer from the repo other skills and recent activity, does the trigger context come up in the user actual workflow? Name the alternative: what does the user do today without this skill, and is that workable?

2. **Description discoverability — false positives and false negatives.** Invent 5 concrete user prompts that the description SHOULD trigger on. Then 5 prompts where it should NOT trigger but might (because of wording overlap with this skill description, or because of overlap with a sibling skill). For each, would Claude pick this skill correctly? Where are the failure modes — too broad picks this when something else is better; too narrow gets skipped when it should fire.

3. **Overlap with existing skills.** Find the sibling skill whose description overlaps most with this one. Is the boundary explicit? Will Claude flip-coin between them? If you removed THIS skill from the repo, would the user be meaningfully worse off, or would they just use the overlapping skill?

4. **Maintenance and drift surface.** What is the long-term cost? bin/ scripts coupled to a specific CLI version or output format? Prompt templates that rot as Codex or Claude evolve? Allowlist entries the user has to maintain? Documentation that drifts from code? How many other places in the repo would need to change in lockstep if this skill changes?

5. **The strongest case against shipping this skill.** Spend at least a few sentences making it. Steelman: the user does not need this; their existing skills cover the case; this is feature creep dressed as utility; the maintenance and cognitive cost outweighs the workflow saved. If you cannot make a compelling case, say so — that is signal the skill genuinely earns its place.

Format as markdown with these five sections as level-2 headers. Be direct. Avoid politeness inflation — soft adversarial review is worse than none.

If the skill earns its place after honest pressure-testing, say so plainly.
EOF
)

STD_PROMPT="${STD_TEMPLATE//__SKILL_PATH__/$SKILL_PATH}"
ADV_PROMPT="${ADV_TEMPLATE//__SKILL_PATH__/$SKILL_PATH}"

# Install the cleanup trap BEFORE backgrounding any child, so a SIGINT
# during codex startup still hits the trap. PIDs are filled in after each
# background launch; cleanup guards on empty.
STD_PID=
ADV_PID=
WATCHDOG_PID=

cleanup() {
  [[ -n "$STD_PID" ]] && kill -KILL -- "-$STD_PID" 2>/dev/null
  [[ -n "$ADV_PID" ]] && kill -KILL -- "-$ADV_PID" 2>/dev/null
  [[ -n "$WATCHDOG_PID" ]] && kill -KILL "$WATCHDOG_PID" 2>/dev/null
  exit 130
}
trap cleanup INT TERM

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
  --arg mode "skill" \
  --arg skill_path "$SKILL_PATH" \
  --arg skill_name "$SKILL_NAME" \
  --arg run_dir "$RUN_DIR" \
  --arg standard_md "$RUN_DIR/standard.md" \
  --arg adversarial_md "$RUN_DIR/adversarial.md" \
  --arg standard_jsonl "$RUN_DIR/standard.jsonl" \
  --arg adversarial_jsonl "$RUN_DIR/adversarial.jsonl" \
  --arg model "$MODEL" \
  --argjson timeout_secs "$TIMEOUT_SECS" \
  --argjson std_rc "$STD_RC" \
  --argjson adv_rc "$ADV_RC" \
  '{timestamp:$timestamp, mode:$mode, skill_path:$skill_path, skill_name:$skill_name, run_dir:$run_dir, standard_md:$standard_md, adversarial_md:$adversarial_md, standard_jsonl:$standard_jsonl, adversarial_jsonl:$adversarial_jsonl, model:$model, timeout_secs:$timeout_secs, std_rc:$std_rc, adv_rc:$adv_rc}' \
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
