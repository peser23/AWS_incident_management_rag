#!/usr/bin/env bash
# Smoke test for the running UI: Streamlit health, page served, bound to localhost only, retrieval reachable.
set -euo pipefail
UI="${UI_URL:-http://127.0.0.1:8501}"
API="${API_URL:-http://127.0.0.1:8000}"

echo "ui health:      $(curl -sf "$UI/_stcore/health")"
curl -sf "$UI/" | grep -q '<div id="root">' && echo "ui page:        served"
listen=$(ss -ltnH 'sport = :8501' | awk '{print $4}' | sort -u | tr '\n' ' ')
echo "ui listens on:  $listen"
[[ "$listen" == "127.0.0.1:8501 " ]] || { echo "FAIL: 8501 must listen on 127.0.0.1 only"; exit 1; }
echo "service ready:  $(curl -s "$API/readyz" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ready"])')"
echo "facets:         $(curl -sf "$API/v1/facets" | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: len(v) for k, v in d.items()})')"
