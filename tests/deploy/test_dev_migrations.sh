#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export TEST_TMP="$tmp" ENV_VARIABLE=dev
# Stub external services; keep the real jq and deployment script.
git() { if [[ $1 == rev-parse ]]; then printf 'abc1234\n'; else command git "$@"; fi; }
docker() { if [[ $1 == login ]]; then cat >/dev/null; fi; }
aws() {
    case "$1 $2" in
        'ecr get-login-password') printf 'fixture\n' ;;
        'ecs describe-services') printf '%s\n' '{"status":"ACTIVE","launchType":"FARGATE","networkConfiguration":{"awsvpcConfiguration":{"subnets":["subnet-test"],"securityGroups":["sg-test"],"assignPublicIp":"DISABLED"}}}' ;;
        'ecs describe-task-definition') printf '%s\n' '{"taskDefinition":{"containerDefinitions":[{"name":"1theorycouk-shared-dev-back"}]}}' ;;
        'ecs register-task-definition') printf 'arn:revision\n' ;;
        'ecs run-task')
            jq -e '.networkConfiguration.awsvpcConfiguration.subnets == ["subnet-test"] and .launchType == "FARGATE" and .overrides.containerOverrides[0].command == ["python","manage.py","migrate","--noinput"]' "${6#file://}" >/dev/null
            if [[ $SCENARIO == start_failure ]]; then printf '%s\n' '{"failures":[{"reason":"failure"}],"tasks":[]}'; else printf '%s\n' '{"failures":[],"tasks":[{"taskArn":"arn:task"}]}'; fi ;;
        'ecs wait') [[ $SCENARIO != wait_failure ]] ;;
        'ecs describe-tasks')
            if [[ $SCENARIO == failure ]]; then code=1; else code=0; fi
            printf '{"failures":[],"tasks":[{"lastStatus":"STOPPED","containers":[{"name":"1theorycouk-shared-dev-back","exitCode":%s}]}]}\n' "$code" ;;
        'ecs update-service') touch "$TEST_TMP/updated" ;;
        *) printf 'Unexpected AWS call: %s %s\n' "$1" "$2" >&2; return 1 ;;
    esac
}
bash() { if [[ $1 == scripts/wait_ecs_dev.sh ]]; then return 0; fi; command bash "$@"; }
export -f git docker aws bash
for SCENARIO in success failure start_failure wait_failure; do
    export SCENARIO
    rm -f "$tmp/updated"
    status=0
    command bash "$root/scripts/deploy_aws_dev.sh" >"$tmp/output" 2>&1 || status=$?
    if [[ $SCENARIO == success ]]; then
        [[ $status == 0 && -f $tmp/updated ]] || { cat "$tmp/output"; exit 1; }
    else
        [[ $status != 0 && ! -f $tmp/updated ]] || { cat "$tmp/output"; exit 1; }
    fi
done
printf 'Dev migration deployment checks passed.\n'
