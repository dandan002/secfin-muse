#!/usr/bin/env bash
# Apply the public side of an environment on Cloudflare: a tunnel and DNS name per ECS service, and the
# tunnel tokens (into SSM, for the tasks). Free, and nothing in it runs, so it stays up between
# sessions; re-run it only when the catalog (infra/terraform/common/catalog.tf) changes.
#
#   export CLOUDFLARE_API_TOKEN=...   # Account: Cloudflare Tunnel Edit; Zone: DNS Edit
#   infra/edge.sh demo
set -euo pipefail

env="${1:?usage: edge.sh <env>}"
source "$(dirname "$0")/lib.sh"
[[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || { echo "export CLOUDFLARE_API_TOKEN first (see the header)" >&2; exit 1; }

init edge
terraform -chdir="$tf/edge" workspace select -or-create "$env" > /dev/null
terraform -chdir="$tf/edge" apply -input=false "$vars"
