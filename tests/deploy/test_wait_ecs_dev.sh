#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
test_tmp_dir=$(mktemp -d -t uk-dev-wait-test.XXXXXX)
trap 'rm -f "$test_tmp_dir/calls" "$test_tmp_dir/output"; rmdir "$test_tmp_dir"' EXIT
export TEST_CALLS_FILE="$test_tmp_dir/calls"
export EXPECTED='arn:aws:ecs:eu-west-2:501588779980:task-definition/1theorycouk-shared-dev-back:55'

# Exported Bash functions replace only AWS and sleeps inside the test child.
aws() {
    local calls=0
    if [[ -f "$TEST_CALLS_FILE" ]]; then read -r calls < "$TEST_CALLS_FILE"; fi
    calls=$((calls + 1))
    printf '%s\n' "$calls" > "$TEST_CALLS_FILE"
    if [[ "$SCENARIO" == api_error || ( "$SCENARIO" == transient && "$calls" == 1 ) ]]; then
        return 1
    fi
    if [[ "$SCENARIO" == missing ]]; then printf 'null\n'; return; fi
    local revision="$EXPECTED" rollout=COMPLETED desired=1 pending=0 draining=false
    case "$SCENARIO" in
        replaced) revision="${EXPECTED%:*}:54" ;;
        failed) rollout=FAILED ;;
        zero) desired=0 ;;
        timeout) pending=1 ;;
        delayed) if (( calls <= 45 )); then draining=true; fi ;;
    esac
    jq -n --arg revision "$revision" --arg rollout "$rollout" \
        --argjson desired "$desired" --argjson pending "$pending" --argjson draining "$draining" '
        {taskDefinition:$revision,desiredCount:$desired,runningCount:$desired,pendingCount:$pending,
         deployments:([{taskDefinition:$revision,status:"PRIMARY",rolloutState:$rollout,
                       rolloutStateReason:"fixture rollout failure"}]
           + (if $draining then [{taskDefinition:"old",status:"DRAINING",rolloutState:"COMPLETED"}] else [] end)),
         events:["fixture event"]}'
}
sleep() {
    if [[ "$SCENARIO" == timeout || "$SCENARIO" == zero ]]; then command sleep "$@"; fi
}
export -f aws sleep

check() {
    local scenario=$1 expected_status=$2 expected_text=$3
    export SCENARIO="$scenario"
    printf '0\n' > "$TEST_CALLS_FILE"
    local timeout=30 status=0
    if [[ "$scenario" == timeout || "$scenario" == zero ]]; then timeout=1; fi
    ECS_DEPLOY_TIMEOUT_SECONDS="$timeout" ECS_DEPLOY_POLL_SECONDS=1 \
        bash "$repo_root/scripts/wait_ecs_dev.sh" "$EXPECTED" > "$test_tmp_dir/output" 2>&1 || status=$?
    if [[ "$status" != "$expected_status" ]] || ! grep -Eq "$expected_text" "$test_tmp_dir/output"; then
        printf 'FAIL: %s (exit %s)\n' "$scenario" "$status" >&2
        sed -n '1,12p' "$test_tmp_dir/output" >&2
        exit 1
    fi
    printf 'PASS: %s\n' "$scenario"
}

check stable 0 'Dev revision is stable'
check delayed 0 'Dev revision is stable'
read -r calls < "$TEST_CALLS_FILE"
[[ "$calls" == 46 ]]  # Successful draining can exceed the stock waiter's 40 checks.
check transient 0 'Dev revision is stable'
check replaced 1 'replaced or rolled back'
check failed 1 'fixture rollout failure'
check missing 1 'did not return'
check api_error 1 'Could not read Dev deployment status \(3/3\)'
check timeout 1 'did not complete within'
check zero 1 'did not complete within'

if ECS_DEPLOY_TIMEOUT_SECONDS=0 bash "$repo_root/scripts/wait_ecs_dev.sh" "$EXPECTED" \
    > "$test_tmp_dir/output" 2>&1; then exit 1; fi
if bash "$repo_root/scripts/wait_ecs_dev.sh" 'arn:aws:ecs:eu-west-2:501588779980:task-definition/stage:1' \
    > "$test_tmp_dir/output" 2>&1; then exit 1; fi
printf 'PASS: invalid settings and non-Dev target rejected\n'
