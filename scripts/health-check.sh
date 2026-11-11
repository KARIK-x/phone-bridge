#!/usr/bin/env bash
# Local health check for the bridge
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://localhost:3000/health', timeout=5)
d = json.loads(r.read())
print('health:', d)
if not d.get('ok'): raise SystemExit(1)
"