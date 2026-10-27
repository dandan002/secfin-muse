#!/usr/bin/env bash
# Deploy the mocks as separate external systems, one Terraform state per system.
#
#   infra/deploy.sh demo               # shared stack, both images, then every system
#   infra/deploy.sh demo ngt sfp       # rebuild images and roll just these systems
#
# Reads infra/terraform/<env>.tfvars and infra/terraform/backend.hcl. TF_APPLY_ARGS=-auto-approve skips
# the prompts; add PARALLEL=1 to apply every system at once. Needs terraform, docker and the AWS CLI.
set -euo pipefail

env="${1:?usage: deploy.sh <env> [system ...]}"
shift
systems=("$@")
# mock-control last, so its first clock push finds the others up.
[[ ${#systems[@]} -gt 0 ]] || systems=(ib gateway ngt wematch sfp custodian control)

here="$(cd "$(dirname "$0")" && pwd)"
tf="$here/terraform"
mocks="$here/../mocks"
vars="-var-file=$tf/$env.tfvars"
read -r -a apply_args <<< "${TF_APPLY_ARGS:-}"
tag="${TAG:-$(git -C "$here" rev-parse --short HEAD)$(git -C "$here" diff --quiet HEAD -- "$mocks" || echo "-dirty-$(date +%s)")}"

init() {
  terraform -chdir="$tf/$1" init -input=false -reconfigure \
    -backend-config="$tf/backend.hcl" -backend-config="key=loandesk/$1.tfstate" > /dev/null
}

init shared
terraform -chdir="$tf/shared" workspace select -or-create "$env"
terraform -chdir="$tf/shared" apply -input=false "$vars" ${apply_args[@]+"${apply_args[@]}"}
mocks_repo="$(terraform -chdir="$tf/shared" output -raw ecr_mocks)"
chat_repo="$(terraform -chdir="$tf/shared" output -raw ecr_desk_chat)"
ib_url="$(terraform -chdir="$tf/shared" output -raw ib_url)"
region="${mocks_repo#*.dkr.ecr.}"; region="${region%%.*}"

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${mocks_repo%%/*}"
docker build --platform linux/amd64 -t "$mocks_repo:$tag" "$mocks"
docker push "$mocks_repo:$tag"
# The browser calls mock-ib directly, so its public URL is baked into the bundle.
docker build --platform linux/amd64 --build-arg "VITE_IB_URL=$ib_url" -t "$chat_repo:$tag" "$mocks/apps/desk-chat"
docker push "$chat_repo:$tag"

init vendor
apply_system() {
  TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" apply -input=false "$vars" -var "image_tag=$tag" \
    ${apply_args[@]+"${apply_args[@]}"}
}
for system in "${systems[@]}"; do  # make sure every workspace exists before TF_WORKSPACE picks it
  terraform -chdir="$tf/vendor" workspace select -or-create "$env-$system" > /dev/null
done
if [[ -n "${PARALLEL:-}" ]]; then
  # Separate states, so systems can apply at once (each creates an RDS instance, ~5-10 min).
  [[ " ${apply_args[*]-} " == *" -auto-approve "* ]] || { echo "PARALLEL needs TF_APPLY_ARGS=-auto-approve" >&2; exit 2; }
  mkdir -p "$here/out"
  pids=()
  for system in "${systems[@]}"; do
    apply_system "$system" > "$here/out/apply-$env-$system.log" 2>&1 &
    pids+=("$!")
  done
  failed=0
  for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then echo "ok    $env-${systems[$i]}"; else echo "FAIL  $env-${systems[$i]} (infra/out/apply-$env-${systems[$i]}.log)"; failed=1; fi
  done
  [[ $failed == 0 ]] || exit 1
else
  for system in "${systems[@]}"; do
    echo "== $env-$system"
    apply_system "$system"
  done
fi

terraform -chdir="$tf/shared" output urls
