#!/usr/bin/env bash
# SCRIPT: INSTALL-CODEX-AUTHORING-CONTROL-PLANE
# TYPE: MANUAL RUN ONLY
# PURPOSE: Copy the generated Codex authoring-control files into /home/tyler.
# CODEX-EXECUTION: FORBIDDEN
# INPUTS: this extracted bundle
# OUTPUTS: /home/tyler/AGENTS.md and /home/tyler/CODEX/

set -eu

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/home/tyler"

if [ ! -d "$TARGET" ]; then
    echo "ERROR: expected WSL home does not exist: $TARGET" >&2
    exit 1
fi

echo "Source: $SOURCE_DIR"
echo "Target: $TARGET"

if [ -e "$TARGET/AGENTS.md" ]; then
    echo "ERROR: $TARGET/AGENTS.md already exists."
    echo "Review/merge it manually instead of overwriting it."
    exit 2
fi

if [ -e "$TARGET/CODEX" ]; then
    echo "ERROR: $TARGET/CODEX already exists."
    echo "Review/merge it manually instead of overwriting it."
    exit 3
fi

cp "$SOURCE_DIR/AGENTS.md" "$TARGET/AGENTS.md"
cp -R "$SOURCE_DIR/CODEX" "$TARGET/CODEX"

echo
echo "Installed:"
echo "  $TARGET/AGENTS.md"
echo "  $TARGET/CODEX/"
echo
echo "Nothing else was modified."
