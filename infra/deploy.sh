#!/usr/bin/env bash
# Deploy the mocks as separate external systems: each is its own project (mocks/<name>),
# image registry, Terraform state and AWS stack.
#
#   infra/deploy.sh demo               # shared stack, then every system
#   infra/deploy.sh demo ngt sfp       # just these systems
#
# Per system: create its ECR repos, build and push its project's image (tagged with the project's
# last commit), then apply its stack. Reads infra/terraform/<env>.tfvars and backend.hcl.
# TF_APPLY_ARGS=-auto-approve skips the prompts; add PARALLEL=1 to deploy every system at once.
# Needs terraform, docker and the AWS CLI.
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

init() {
  terraform -chdir="$tf/$1" init -input=false -reconfigure \
    -backend-config="$tf/backend.hcl" -backend-config="key=loandesk/$1.tfstate" > /dev/null
}

# A system's version is the last commit touching its project or the simulator libs it builds on.
project_tag() {
  local paths=("$mocks/$1" "$mocks/libs")
  local tag
  tag="$(git -C "$here" log -1 --format=%h -- "${paths[@]}")"
  tag="${tag:-$(git -C "$here" rev-parse --short HEAD)}"  # project not committed yet
  git -C "$here" diff --quiet HEAD -- "${paths[@]}" || tag="$tag-dirty-$(date +%s)"
  echo "$tag"
}

tf_eval() {  # <system> <expression>
  echo "$2" | TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" console "$vars" | tr -d '"'
}

deploy_system() {
  local system="$1" tag repo registry
  tag="$(project_tag "$system")"
  echo "== $env-$system @ $tag"

  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" apply -input=false "$vars" -var "image_tag=$tag" \
    -target=module.system.aws_ecr_repository.this ${apply_args[@]+"${apply_args[@]}"}
  repo="$(tf_eval "$system" 'module.system.ecr["mock"]')"
  registry="${repo%%/*}"
  aws ecr get-login-password --region "$(tf_eval "$system" var.region)" \
    | docker login --username AWS --password-stdin "$registry" > /dev/null

  docker build --platform linux/amd64 -f "$mocks/$system/Dockerfile" -t "$repo:$tag" "$mocks"
  docker push "$repo:$tag"
  if [[ "$system" == ib ]]; then
    # The browser calls mock-ib directly, so its public URL is baked into the bundle.
    local chat_repo
    chat_repo="$(tf_eval ib 'module.system.ecr["desk-chat"]')"
    docker build --platform linux/amd64 --build-arg "VITE_IB_URL=https://$(tf_eval ib local.hostname)" \
      -t "$chat_repo:$tag" "$mocks/ib/desk-chat"
    docker push "$chat_repo:$tag"
  fi

  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" apply -input=false "$vars" -var "image_tag=$tag" \
    ${apply_args[@]+"${apply_args[@]}"}
}

init shared
terraform -chdir="$tf/shared" workspace select -or-create "$env"
terraform -chdir="$tf/shared" apply -input=false "$vars" ${apply_args[@]+"${apply_args[@]}"}

init vendor
for system in "${systems[@]}"; do  # make sure every workspace exists before TF_WORKSPACE picks it
  terraform -chdir="$tf/vendor" workspace select -or-create "$env-$system" > /dev/null
done

if [[ -n "${PARALLEL:-}" ]]; then
  # Separate projects and states, so systems deploy at once (each creates an RDS instance, ~5-10 min).
  [[ " ${apply_args[*]-} " == *" -auto-approve "* ]] || { echo "PARALLEL needs TF_APPLY_ARGS=-auto-approve" >&2; exit 2; }
  mkdir -p "$here/out"
  pids=()
  for system in "${systems[@]}"; do
    deploy_system "$system" > "$here/out/deploy-$env-$system.log" 2>&1 &
    pids+=("$!")
  done
  failed=0
  for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
      echo "ok    $env-${systems[$i]}"
    else
      echo "FAIL  $env-${systems[$i]} (infra/out/deploy-$env-${systems[$i]}.log)"
      failed=1
    fi
  done
  [[ $failed == 0 ]] || exit 1
else
  for system in "${systems[@]}"; do
    deploy_system "$system"
  done
fi

terraform -chdir="$tf/shared" output urls
