#!/usr/bin/env bash
# Cron entrypoint for the DAIMO Chunk+Embed job.
#   crontab: 30 2 * * * /home/ec2-user/daimo/ingest/run_chunk_embed.sh
set -euo pipefail
DAIMO_HOME=/home/ec2-user/daimo
set -a; source "$DAIMO_HOME/ingest/chunk_embed.env"; set +a
mkdir -p "$DAIMO_HOME/logs"
exec "$DAIMO_HOME/.venv/bin/python" -W ignore::DeprecationWarning \
  "$DAIMO_HOME/ingest/chunk_embed.py" "$@" >> "$DAIMO_HOME/logs/chunk_embed.log" 2>&1
