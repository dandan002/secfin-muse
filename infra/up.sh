#!/usr/bin/env bash
# Start mock systems for a session, and arm the auto-stop.
#
#   infra/up.sh demo                    # every system, stopping in 3 hours
#   infra/up.sh demo ngt ib control     # just these
#   infra/up.sh demo --for 90m          # running it again (any systems) moves the stop time
#   infra/up.sh demo --fresh ngt        # discard Venue A's saved session and start from the seed
#
# Each system resumes from the snapshot its Postgres saved when it last stopped (infra/postgres).
# Per system: create its image registry, build and push its project's image unless that version
# (the last commit touching the project or the libs) is already there, then apply with
# running=true. Systems start in parallel; logs go to infra/out/up-<env>-<system>.log.
# The stop runs inside AWS (see terraform/shared/autostop.tf), so it fires with the laptop shut.
# Needs terraform, docker, zip and the AWS CLI (export AWS_PROFILE=... first).
set -euo pipefail

env="${1:?usage: up.sh <env> [--for 3h] [--fresh] [system ...]}"
shift
source "$(dirname "$0")/lib.sh"
duration=3h
fresh=false
systems=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --for) duration="${2:?--for needs a duration like 3h or 90m}"; shift 2 ;;
    --fresh) fresh=true; shift ;;
    *) systems+=("$1"); shift ;;
  esac
done
[[ ${#systems[@]} -gt 0 ]] || systems=("${all_systems[@]}")
check_systems "${systems[@]}"

case "$duration" in
  *h) seconds=$(( ${duration%h} * 3600 )) ;;
  *m) seconds=$(( ${duration%m} * 60 )) ;;
  *) echo "--for takes hours or minutes, e.g. 3h or 90m" >&2; exit 2 ;;
esac
(( seconds >= 900 && seconds <= 12 * 3600 )) || { echo "--for must be between 15m and 12h" >&2; exit 2; }

aws sts get-caller-identity > /dev/null || { echo "AWS credentials missing or expired: run aws login" >&2; exit 1; }

# Docker Desktop's keychain helper can block on a prompt nobody sees, so log in to ECR in a
# throwaway Docker config instead: same context and plugins, no credential store.
docker_context="$(docker context show)"
export DOCKER_CONFIG="$(mktemp -d)"
for dir in contexts cli-plugins buildx; do
  [[ ! -e "$HOME/.docker/$dir" ]] || ln -s "$HOME/.docker/$dir" "$DOCKER_CONFIG/$dir"
done
printf '{"currentContext":"%s"}\n' "$docker_context" > "$DOCKER_CONFIG/config.json"

path_tag() {  # <path>... -> the last commit touching them
  local tag
  tag="$(git -C "$here" log -1 --format=%h -- "$@")"
  tag="${tag:-$(git -C "$here" rev-parse --short HEAD)}"  # not committed yet
  git -C "$here" diff --quiet HEAD -- "$@" || tag="$tag-dirty-$(date +%s)"
  if [[ -n "$(git -C "$here" ls-files --others --exclude-standard -- "$@")" ]]; then
    tag="${tag%-dirty-*}-dirty-$(date +%s)"
  fi
  echo "$tag"
}

project_tag() {  # the system's project and the simulator libs it builds on
  path_tag "$mocks/$1" "$mocks/libs"
}

tf_eval() {  # <system> <expression>
  echo "$2" | TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" console "$vars" -var running=true | tr -d '"'
}

push_image() {  # <repo url> <tag> <docker build args...>
  local repo="$1" tag="$2"
  shift 2
  if aws ecr describe-images --region "$region" --repository-name "${repo#*/}" --image-ids "imageTag=$tag" > /dev/null 2>&1; then
    echo "$repo:$tag already pushed"
    return
  fi
  docker build --platform linux/amd64 -t "$repo:$tag" "$@"
  docker push "$repo:$tag"
}

up_system() {
  local system="$1" tag repo
  tag="$(project_tag "$system")"
  echo "== $env-$system @ $tag"

  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" apply -input=false -auto-approve "$vars" \
    -var running=true -var "image_tag=$tag" -target=module.system.aws_ecr_repository.this
  repo="$(tf_eval "$system" 'module.system.ecr["mock"]')"

  push_image "$repo" "$tag" -f "$mocks/$system/Dockerfile" "$mocks"
  if [[ "$system" == ib ]]; then
    # The browser calls mock-ib directly, so its public URL is baked into the bundle.
    push_image "$(tf_eval ib 'module.system.ecr["desk-chat"]')" "$tag" \
      --build-arg "VITE_IB_URL=https://$(tf_eval ib local.hostname)" "$mocks/ib/desk-chat"
  fi

  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" apply -input=false -auto-approve "$vars" \
    -var running=true -var "image_tag=$tag" -var "postgres_image_tag=$postgres_tag"
}

utc() {  # <epoch seconds> -> 2026-10-27T21:00:00, on BSD (macOS) or GNU date
  date -u -r "$1" +%Y-%m-%dT%H:%M:%S 2> /dev/null || date -u -d "@$1" +%Y-%m-%dT%H:%M:%S
}

arm_autostop() {
  local project role source stop_at target
  project="$(terraform -chdir="$tf/shared" output -raw autostop_project)"
  role="$(terraform -chdir="$tf/shared" output -raw autostop_scheduler_role)"
  source="$(terraform -chdir="$tf/shared" output -raw autostop_source)"

  # The stop job runs this checkout's down.sh and Terraform config (with the tfvars and backend).
  local bundle
  bundle="$(mktemp -d)/infra.zip"
  (cd "$here/.." && zip -qr "$bundle" infra/down.sh infra/lib.sh infra/terraform \
    -x '*/.terraform/*' '*.tfstate' '*.tfstate.*')
  aws s3 cp --region "$region" --only-show-errors "$bundle" "$source"

  stop_at="$(utc $(( $(date +%s) + seconds )))"
  target="$(printf '{"Arn":"arn:aws:scheduler:::aws-sdk:codebuild:startBuild","RoleArn":"%s","Input":"{\\"ProjectName\\":\\"%s\\"}"}' "$role" "$project")"
  local verb=create-schedule
  aws scheduler get-schedule --region "$region" --name "$autostop_schedule" > /dev/null 2>&1 && verb=update-schedule
  aws scheduler "$verb" --region "$region" --name "$autostop_schedule" \
    --schedule-expression "at($stop_at)" --schedule-expression-timezone UTC \
    --flexible-time-window Mode=OFF --action-after-completion DELETE --target "$target" > /dev/null
  echo "auto-stop armed: every $env system stops at $stop_at UTC ($duration from now)"
}

init shared
terraform -chdir="$tf/shared" workspace select -or-create "$env" > /dev/null
terraform -chdir="$tf/shared" apply -input=false -auto-approve "$vars"
region="$(terraform -chdir="$tf/shared" output -raw region)"

arm_autostop  # before anything billable starts, so a failed or abandoned start still gets stopped

postgres_repo="$(terraform -chdir="$tf/shared" output -raw postgres_repository)"
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${postgres_repo%%/*}" > /dev/null
postgres_tag="$(path_tag "$here/postgres")"
push_image "$postgres_repo" "$postgres_tag" "$here/postgres"

if $fresh; then
  bucket="$(terraform -chdir="$tf/shared" output -raw snapshot_bucket)"
  for system in "${systems[@]}"; do  # versioned: the old dump stays recoverable for 30 days
    aws s3 rm --region "$region" --only-show-errors "s3://$bucket/$system.sql.gz"
    echo "$system starts from the seed"
  done
fi

init vendor
for system in "${systems[@]}"; do  # make sure every workspace exists before TF_WORKSPACE picks it
  terraform -chdir="$tf/vendor" workspace select -or-create "$env-$system" > /dev/null
done
for_each_system up up_system "${systems[@]}" || status=$?

echo
for system in "${systems[@]}"; do
  TF_WORKSPACE="$env-$system" terraform -chdir="$tf/vendor" output -json urls 2> /dev/null || true
done
exit "${status:-0}"
