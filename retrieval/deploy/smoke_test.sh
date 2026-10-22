#!/usr/bin/env bash
# Smoke test against the running service: health, readiness, one answerable and one unanswerable query.
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:8000}"
post() { curl -sf -m 60 -H 'content-type: application/json' -d "{\"query\": \"$1\"}" "$BASE/v1/triage"; }
status() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["confidence"]["overall"], d["metrics"]["latency_ms"]["total"], "ms")'; }

echo "healthz: $(curl -sf "$BASE/healthz")"
echo "readyz:  $(curl -s "$BASE/readyz")"
echo -n "answerable:   "; post "users cannot sign in to the Coverage web application" | status
echo -n "unanswerable: "; post "zebra printer ribbon jam in cafeteria" | status
