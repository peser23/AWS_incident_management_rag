#!/usr/bin/env bash
# Install daimo-retrieval on the app EC2. Run as root from the repo's retrieval/ directory:
#   sudo ./deploy/install.sh        (re-run to upgrade)
# Builds a venv from the pinned requirements.lock, installs the unit, starts it and runs a smoke query.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/daimo-retrieval
PY="${PYTHON:-python3.12}"

id daimo &>/dev/null || useradd --system --no-create-home --shell /sbin/nologin daimo
mkdir -p "$DEST/deploy"
[[ -x "$DEST/venv/bin/python" ]] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$SRC/requirements.lock"
"$DEST/venv/bin/pip" install --quiet --no-deps --force-reinstall "$SRC"
install -m 0755 "$SRC/deploy/load_ssm_env.py" "$DEST/deploy/load_ssm_env.py"
install -m 0644 "$SRC/deploy/daimo-retrieval.service" /etc/systemd/system/daimo-retrieval.service

systemctl daemon-reload
systemctl enable daimo-retrieval
systemctl restart daimo-retrieval
echo "waiting for the service..."
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8000/healthz >/dev/null && break; sleep 1; done
"$SRC/deploy/smoke_test.sh"
