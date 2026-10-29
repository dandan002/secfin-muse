#!/usr/bin/env bash
# What's running, where, and when it stops.
#
#   infra/status.sh demo
set -euo pipefail

env="${1:?usage: status.sh <env>}"
source "$(dirname "$0")/lib.sh"
region="$(sed -n 's/^region *= *"\(.*\)".*/\1/p' "$tf/$env.tfvars")"

init vendor
for system in "${all_systems[@]}"; do
  if ! has_workspace vendor "$env-$system"; then
    printf '%-10s never started\n' "$system"
  elif TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" state list | grep aws_ecs_service.app > /dev/null; then  # grep -q would cut the pipe
    printf '%-10s on   %s\n' "$system" \
      "$(TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" output -json urls | tr -d '{}"' | tr ',' ' ')"
  else
    printf '%-10s off\n' "$system"
  fi
done

echo
stop_at="$(aws scheduler get-schedule ${region:+--region "$region"} --name "$autostop_schedule" \
  --query ScheduleExpression --output text 2> /dev/null || true)"
if [[ -n "$stop_at" ]]; then
  echo "auto-stop: $(sed 's/^at(\(.*\)T\(.*\))$/\1 \2/' <<< "$stop_at") UTC (infra/up.sh $env --for <duration> moves it)"
else
  echo "auto-stop: none scheduled"
fi
