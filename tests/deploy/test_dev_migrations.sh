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
    local code target argument
    local repository=501588779980.dkr.ecr.eu-west-2.amazonaws.com/1theorycouk-shared-dev-back
    local digest=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    case "$1 $2" in
        'ecr get-login-password') printf 'fixture\n' ;;
        'ecr describe-images') printf '%s\n' "$digest" ;;
        'ecs list-services')
            if [[ $SCENARIO == missing_workers ]]; then printf '[]\n'; else
                printf '["cluster/1theorycouk-shared-dev-back","cluster/video","cluster/general","cluster/beat","cluster/stage"]\n'
            fi ;;
        'ecs describe-services')
            if [[ ${10} == 'services[0]' ]]; then
                printf '%s\n' '{"status":"ACTIVE","launchType":"FARGATE","networkConfiguration":{"awsvpcConfiguration":{"subnets":["subnet-test"],"securityGroups":["sg-test"],"assignPublicIp":"DISABLED"}}}'
            else
                target=${8##*/}
                if [[ $SCENARIO == concurrent_release && $8 == video ]]; then
                    printf 'other:%s\n' "$target"
                else
                    printf 'old:%s\n' "$target"
                fi
            fi ;;
        'ecs describe-task-definition')
            target=${6#old:}
            if [[ $target == stage ]]; then repository="${repository%-dev-back}-stage-back"; fi
            printf '{"taskDefinition":{"family":"%s","containerDefinitions":[{"name":"%s","image":"%s:old"},{"name":"sidecar","image":"sidecar:old"}]}}\n' "$target" "$target" "$repository" ;;
        'ecs register-task-definition')
            jq -er --arg image "$repository@$digest" '
                select(.containerDefinitions[0].image == $image and .containerDefinitions[1].image == "sidecar:old")
                | "new:\(.family)"
            ' "${6#file://}" ;;
        'ecs run-task')
            jq -e '.networkConfiguration.awsvpcConfiguration.subnets == ["subnet-test"] and .launchType == "FARGATE" and .overrides.containerOverrides[0].command == ["python","manage.py","migrate","--noinput"]' "${6#file://}" >/dev/null
            if [[ $SCENARIO == start_failure ]]; then printf '%s\n' '{"failures":[{"reason":"failure"}],"tasks":[]}'; else printf '%s\n' '{"failures":[],"tasks":[{"taskArn":"arn:task"}]}'; fi ;;
        'ecs wait') [[ $SCENARIO != wait_failure ]] ;;
        'ecs describe-tasks')
            if [[ $SCENARIO == failure ]]; then code=1; else code=0; fi
            printf '{"failures":[],"tasks":[{"lastStatus":"STOPPED","containers":[{"name":"1theorycouk-shared-dev-back","exitCode":%s}]}]}\n' "$code" ;;
        'ecs update-service')
            for argument; do
                if [[ "$argument" == new:* ]]; then printf '%s\n' "$argument" >> "$TEST_TMP/updated"; fi
            done ;;
        *) printf 'Unexpected AWS call: %s %s\n' "$1" "$2" >&2; return 1 ;;
    esac
}
bash() { if [[ $1 == scripts/wait_ecs_dev.sh ]]; then return 0; fi; command bash "$@"; }
export -f git docker aws bash
for SCENARIO in success failure start_failure wait_failure missing_workers concurrent_release; do
    export SCENARIO
    rm -f "$tmp/updated"
    status=0
    command bash "$root/scripts/deploy_aws_dev.sh" >"$tmp/output" 2>&1 || status=$?
    if [[ $SCENARIO == success ]]; then
        [[ $status == 0 && -f $tmp/updated &&
            $(cat "$tmp/updated") == $'new:video\nnew:general\nnew:beat\nnew:1theorycouk-shared-dev-back' ]] || { cat "$tmp/output"; exit 1; }
    else
        [[ $status != 0 && ! -f $tmp/updated ]] || { cat "$tmp/output"; exit 1; }
    fi
done
printf 'Dev migration deployment checks passed.\n'
