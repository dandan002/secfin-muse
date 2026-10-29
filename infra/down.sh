#!/usr/bin/env bash
# Stop mock systems: remove everything billed by the hour (load balancers, NAT, the task). Each
# task's Postgres saves a snapshot to S3 as it stops, and the next up.sh restores it. The free base
# stays (network, certificate, image registry, logs), so the next start takes a few minutes.
#
#   infra/down.sh demo            # every system, release control's egress IP, cancel the auto-stop
#   infra/down.sh demo ngt        # just Venue A (the auto-stop still covers the rest)
#
# The auto-stop job (terraform/shared/autostop.tf) runs this same script inside AWS.
set -euo pipefail

env="${1:?usage: down.sh <env> [system ...]}"
shift
source "$(dirname "$0")/lib.sh"
systems=("$@")
everything=false
[[ ${#systems[@]} -gt 0 ]] || { systems=("${all_systems[@]}"); everything=true; }
check_systems "${systems[@]}"

# A targeted destroy only ever deletes (the targets and whatever depends on them), so a half-started
# system can't make the stop job create its missing base. up.sh's apply puts everything back.
hourly=(aws_ecs_service.app aws_ecs_task_definition.app aws_lb.alb aws_lb.fix aws_lb_target_group.app
  aws_lb_target_group.fix aws_nat_gateway.this)

region="$(sed -n 's/^region *= *"\(.*\)".*/\1/p' "$tf/$env.tfvars")"

down_system() {
  local cluster tasks
  # The task first, on its own: its Postgres saves the snapshot as it stops, and mock-control's
  # reaches S3 through the NAT and routes removed in the second step.
  TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" destroy -input=false -auto-approve "$vars" \
    -var running=false -target=module.system.aws_ecs_service.app
  cluster="$(TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" output -raw cluster 2> /dev/null || true)"
  if [[ -n "$cluster" ]]; then
    # Both lists: the desired status of a task that is still stopping is already STOPPED.
    tasks="$(for status in RUNNING STOPPED; do
      aws ecs list-tasks --region "$region" --cluster "$cluster" --desired-status "$status" --query taskArns --output text
    done | tr '\t' '\n' | grep -v '^None$' || true)"
    [[ -z "$tasks" ]] || aws ecs wait tasks-stopped --region "$region" --cluster "$cluster" --tasks $tasks
  fi

  TF_WORKSPACE="$env-$1" terraform -chdir="$tf/vendor" destroy -input=false -auto-approve "$vars" \
    -var running=false "${hourly[@]/#/-target=module.system.}"
}

init vendor
started=()
for system in "${systems[@]}"; do  # a system that was never started has nothing to stop
  if has_workspace vendor "$env-$system"; then started+=("$system"); fi
done

status=0
if [[ ${#started[@]} -gt 0 ]]; then
  for_each_system down down_system "${started[@]}" || status=$?
else
  echo "nothing to stop"
fi

if $everything && [[ $status -eq 0 ]]; then
  # Nothing is running, so nothing is egressing: release the IP (an idle one is billed by the hour).
  log="$here/out/down-$env-egress-ip.log"
  mkdir -p "$here/out"
  init shared
  if TF_WORKSPACE="$env" terraform -chdir="$tf/shared" destroy -input=false -auto-approve "$vars" \
    -target=aws_eip.control_egress > "$log" 2>&1; then
    echo "released control's egress IP"
  else
    echo "FAIL  releasing control's egress IP (infra/out/down-$env-egress-ip.log)"
    [[ -z "${CODEBUILD_BUILD_ID:-}" ]] || tail -n 40 "$log"
    status=1
  fi
fi

if $everything && [[ -z "${CODEBUILD_BUILD_ID:-}" ]]; then
  # Stopped by hand: drop the pending auto-stop. (When the schedule itself fired, it's already gone.)
  aws scheduler delete-schedule ${region:+--region "$region"} --name "$autostop_schedule" 2> /dev/null \
    && echo "auto-stop cancelled" || true
fi
exit $status
