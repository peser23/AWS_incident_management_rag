#!/usr/bin/env bash
# Install the DAIMO Incident management UI on the app EC2. Run as root from the repo's web/ directory:
#   sudo ./deploy/install.sh        (re-run to upgrade)
# Builds a venv from the pinned requirements.lock, copies the app, installs the unit, starts it and smoke-tests.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/daimo-web
PY="${PYTHON:-python3.12}"

id daimo &>/dev/null || useradd --system --no-create-home --shell /sbin/nologin daimo
mkdir -p "$DEST"
[[ -x "$DEST/venv/bin/python" ]] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$SRC/requirements.lock"

# app code: root-owned, world-readable; replaced wholesale so deleted files don't linger
rm -rf "$DEST/app.new"
mkdir -p "$DEST/app.new"
cp -r "$SRC/app.py" "$SRC/daimo_web" "$SRC/facets.json" "$SRC/.streamlit" "$DEST/app.new/"
find "$DEST/app.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$DEST/app.new"
chmod -R u=rwX,go=rX "$DEST/app.new"
rm -rf "$DEST/app.old"
[[ -d "$DEST/app" ]] && mv "$DEST/app" "$DEST/app.old"
mv "$DEST/app.new" "$DEST/app"
rm -rf "$DEST/app.old"

install -m 0644 "$SRC/deploy/daimo-web.service" /etc/systemd/system/daimo-web.service
systemctl daemon-reload
systemctl enable daimo-web
systemctl restart daimo-web
echo "waiting for the UI..."
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null && break; sleep 1; done
"$SRC/deploy/smoke_test.sh"
