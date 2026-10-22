#!/usr/bin/env bash
# Write KEY=VALUE lines from a file to SSM Parameter Store under /daimo/retrieval/ (String type, not secrets).
#   ./deploy/put_ssm_params.sh deploy/ssm-params.example.env
# Needs ssm:PutParameter (an admin/deploy identity, not the runtime role). Restart the service afterwards.
set -euo pipefail
FILE="${1:?usage: put_ssm_params.sh <file>}"
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  key="${line%%=*}"; val="${line#*=}"
  aws ssm put-parameter --region "${AWS_REGION:-us-east-1}" --name "/daimo/retrieval/$key" \
    --type String --overwrite --value "$val" >/dev/null && echo "set $key"
done < "$FILE"
