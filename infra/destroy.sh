#!/usr/bin/env bash
# Tear systems down. All mock data is derived from the seed, so nothing is lost: deploy.sh and a
# POST /reset bring back the same state.
#
#   infra/destroy.sh demo ngt          # just Venue A
#   infra/destroy.sh demo              # every system, then the shared stack (images, token, EIP)
set -euo pipefail

env="${1:?usage: destroy.sh <env> [system ...]}"
shift
systems=("$@")
all=false
[[ ${#systems[@]} -gt 0 ]] || { systems=(control ib gateway ngt wematch sfp custodian analytics); all=true; }

tf="$(cd "$(dirname "$0")" && pwd)/terraform"
vars="-var-file=$tf/$env.tfvars"
read -r -a apply_args <<< "${TF_APPLY_ARGS:-}"

init() {
  terraform -chdir="$tf/$1" init -input=false -reconfigure \
    -backend-config="$tf/backend.hcl" -backend-config="key=loandesk/$1.tfstate" > /dev/null
}

init vendor
for system in "${systems[@]}"; do
  echo "== $env-$system"
  terraform -chdir="$tf/vendor" workspace select -or-create "$env-$system"
  terraform -chdir="$tf/vendor" destroy -input=false "$vars" ${apply_args[@]+"${apply_args[@]}"}
done

if $all; then
  init shared
  terraform -chdir="$tf/shared" workspace select "$env"
  terraform -chdir="$tf/shared" destroy -input=false "$vars" ${apply_args[@]+"${apply_args[@]}"}
fi
