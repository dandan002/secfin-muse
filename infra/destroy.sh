#!/usr/bin/env bash
# Remove systems entirely, base included. For between sessions, infra/down.sh is enough: a stopped
# system already costs ~$0. All mock data is derived from the seed, so nothing is lost either way.
#
#   infra/destroy.sh demo ngt          # just Venue A
#   infra/destroy.sh demo              # every system, then the shared stack (token, EIP, auto-stop)
set -euo pipefail

env="${1:?usage: destroy.sh <env> [system ...]}"
shift
source "$(dirname "$0")/lib.sh"
systems=("$@")
everything=false
[[ ${#systems[@]} -gt 0 ]] || { systems=("${all_systems[@]}"); everything=true; }
read -r -a apply_args <<< "${TF_APPLY_ARGS:-}"

init vendor
for system in "${systems[@]}"; do
  has_workspace vendor "$env-$system" || continue
  echo "== $env-$system"
  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" destroy -input=false "$vars" -var running=false \
    ${apply_args[@]+"${apply_args[@]}"}
done

if $everything; then
  region="$(sed -n 's/^region *= *"\(.*\)".*/\1/p' "$tf/$env.tfvars")"
  aws scheduler delete-schedule ${region:+--region "$region"} --name "$autostop_schedule" 2> /dev/null || true
  init shared
  terraform -chdir="$tf/shared" workspace select "$env"
  terraform -chdir="$tf/shared" destroy -input=false "$vars" ${apply_args[@]+"${apply_args[@]}"}
fi
