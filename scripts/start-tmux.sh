#!/usr/bin/env bash
set -euo pipefail
SESSION="${TMUX_SESSION:-ai-bridge}"
WORKDIR="${OPENCODE_WORKDIR:-$HOME}"
CMD="${OPENCODE_CMD:-opencode}"
if tmux has-session -t "$SESSION" 2>/dev/null; then echo "session '$SESSION' exists"; exit 0; fi
tmux new-session -d -s "$SESSION" -c "$WORKDIR" "while true; do $CMD; sleep 2; done"
tmux set-option -t "$SESSION" history-limit 100000
echo "created '$SESSION'"
