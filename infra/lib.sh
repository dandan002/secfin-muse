# Shared by up.sh, down.sh, status.sh and destroy.sh. Source it after setting `env`.
# Reads infra/terraform/<env>.tfvars and backend.hcl. Needs terraform and the AWS CLI.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tf="$here/terraform"
mocks="$here/../mocks"
vars="-var-file=$tf/$env.tfvars"
all_systems=(ib gateway ngt wematch sfp custodian analytics control)
autostop_schedule="loandesk-$env-autostop"

[[ -f "$tf/$env.tfvars" ]] || { echo "missing $tf/$env.tfvars (copy example.tfvars)" >&2; exit 2; }
[[ -f "$tf/backend.hcl" ]] || { echo "missing $tf/backend.hcl (copy backend.hcl.example)" >&2; exit 2; }

check_systems() {  # <system>...
  local s
  for s in "$@"; do
    [[ " ${all_systems[*]} " == *" $s "* ]] || { echo "unknown system '$s' (one of: ${all_systems[*]})" >&2; exit 2; }
  done
}

init() {
  terraform -chdir="$tf/$1" init -input=false -reconfigure \
    -backend-config="$tf/backend.hcl" -backend-config="key=loandesk/$1.tfstate" > /dev/null
}

has_workspace() {  # <root> <workspace>
  terraform -chdir="$tf/$1" workspace list | tr -d ' *' | grep -qx "$2"
}

# Runs "$@" once per system, all at once, logging to infra/out/<label>-<env>-<system>.log.
for_each_system() {  # <label> <function> <system>...
  local label="$1" fn="$2" failed=0 i
  shift 2
  local systems=("$@") pids=()
  mkdir -p "$here/out"
  for system in "${systems[@]}"; do
    "$fn" "$system" > "$here/out/$label-$env-$system.log" 2>&1 &
    pids+=("$!")
  done
  for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
      echo "ok    $env-${systems[$i]}"
    else
      echo "FAIL  $env-${systems[$i]} (infra/out/$label-$env-${systems[$i]}.log)"
      [[ -z "${CODEBUILD_BUILD_ID:-}" ]] || tail -n 40 "$here/out/$label-$env-${systems[$i]}.log"  # no laptop to read it on
      failed=1
    fi
  done
  return $failed
}
