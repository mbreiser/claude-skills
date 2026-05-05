#!/usr/bin/env bash
# Symlink each skill directory in this repo into ~/.claude/skills/.
# Idempotent: re-running is safe. Refuses to overwrite existing non-symlink targets unless --force.

set -euo pipefail

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SKILLS_DIR="${HOME}/.claude/skills"
FORCE=0

for arg in "$@"; do
    case "$arg" in
        -f|--force) FORCE=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--force]

Symlinks every skill directory in this repo (any directory containing SKILL.md
at its root) into ~/.claude/skills/.

  --force   Replace existing entries (real dirs, files, or symlinks pointing
            elsewhere) with the new symlink. Without --force, conflicts are
            reported and skipped.
EOF
            exit 0
            ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$SKILLS_DIR"

installed=0
skipped=0
conflicts=0

for skill_dir in "$REPO_DIR"/*/; do
    skill_name="$(basename "$skill_dir")"
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    target="$SKILLS_DIR/$skill_name"
    src="$REPO_DIR/$skill_name"

    if [[ -L "$target" ]]; then
        existing="$(readlink "$target")"
        if [[ "$existing" == "$src" ]]; then
            echo "= $skill_name (already linked correctly)"
            ((skipped++))
            continue
        fi
        if [[ $FORCE -eq 1 ]]; then
            rm "$target"
            ln -s "$src" "$target"
            echo "~ $skill_name (was symlink to $existing → relinked)"
            ((installed++))
        else
            echo "! $skill_name (symlink exists pointing at $existing — use --force to replace)" >&2
            ((conflicts++))
        fi
    elif [[ -e "$target" ]]; then
        if [[ $FORCE -eq 1 ]]; then
            backup="${target}.backup-$(date +%Y%m%d-%H%M%S)"
            mv "$target" "$backup"
            ln -s "$src" "$target"
            echo "~ $skill_name (existing dir backed up to $backup → linked)"
            ((installed++))
        else
            echo "! $skill_name (target exists and is not a symlink — use --force to back up + replace)" >&2
            ((conflicts++))
        fi
    else
        ln -s "$src" "$target"
        echo "+ $skill_name (linked)"
        ((installed++))
    fi
done

echo
echo "Installed: $installed | Already-correct: $skipped | Conflicts: $conflicts"
[[ $conflicts -eq 0 ]] || exit 1
