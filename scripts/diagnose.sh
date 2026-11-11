#!/usr/bin/env bash
# Diagnostic dump (no secrets)
set -u
cd "$(dirname "$0")/.."
echo "=== tmux ==="; tmux ls 2>&1 || true
echo "=== bridge process ==="; pgrep -af "bridge.py" || echo "not running"
echo "=== port 3000 ==="; lsof -i:3000 2>/dev/null || echo "nothing listening"
echo "=== health ==="; curl -s http://localhost:3000/health 2>&1 || echo "unreachable"
echo "=== .env (presence only) ==="; python3 - <<'PY'
for line in open('.env'):
    s=line.strip()
    if s and not s.startswith('#') and '=' in s:
        k,v=s.split('=',1)
        print(f"  {k}: {'set' if v.strip() else 'EMPTY'}")
PY
echo "=== recent log ==="; tail -20 bridge.log 2>/dev/null || echo "no log"