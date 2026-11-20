#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export TEST_TMP="$tmp" ENV_VARIABLE=prod
git() { if [[ $1 == rev-parse ]]; then printf 'abc1234\n'; else command git "$@"; fi; }
docker() { if [[ $1 == login ]]; then cat >/dev/null; fi; }
aws() {
    local code
    local repository=971450397487.dkr.ecr.eu-west-2.amazonaws.com/1theorycouk-shared-prod-back
    case "$1 $2" in
        'sts get-caller-identity') printf '971450397487\n' ;;
        'ecr get-login-password') printf 'fixture\n' ;;
        'ecs describe-services')
            if [[ ${10} == 'services[0]' ]]; then
                printf '%s\n' '{"status":"ACTIVE","capacityProviderStrategy":[{"capacityProvider":"FARGATE_SPOT","weight":1}],"networkConfiguration":{"awsvpcConfiguration":{"subnets":["subnet-test"],"securityGroups":["sg-test"],"assignPublicIp":"ENABLED"}}}'
            else
                printf 'old:1theorycouk-shared-prod-back\n'
            fi ;;
        'ecs describe-task-definition')
            printf '{"taskDefinition":{"family":"1theorycouk-shared-prod-back","containerDefinitions":[{"name":"1theorycouk-shared-prod-back","image":"%s:old","environment":[{"name":"GOOGLE_CLIENT_IDS","value":""}]}]}}\n' "$repository" ;;
        'ecs register-task-definition')
            jq -er --arg image "$repository:abc1234-prod" '
                select(.containerDefinitions[0].image == $image)
                | select(.containerDefinitions[0].environment[] | select(.name == "GOOGLE_CLIENT_IDS" and (.value | length) > 0))
                | "arn:aws:ecs:eu-west-2:971450397487:task-definition/1theorycouk-shared-prod-back:99"
            ' "${6#file://}" ;;
        'ecs run-task')
            jq -e '.networkConfiguration.awsvpcConfiguration.subnets == ["subnet-test"]
                and (.capacityProviderStrategy | length) > 0
                and .overrides.containerOverrides[0].command == ["python","manage.py","migrate","--noinput"]' "${6#file://}" >/dev/null
            if [[ $SCENARIO == start_failure ]]; then printf '%s\n' '{"failures":[{"reason":"failure"}],"tasks":[]}'; else printf '%s\n' '{"failures":[],"tasks":[{"taskArn":"arn:task"}]}'; fi ;;
        'ecs wait') [[ $SCENARIO != wait_failure ]] ;;
        'ecs describe-tasks')
            if [[ $SCENARIO == failure ]]; then code=1; else code=0; fi
            printf '{"failures":[],"tasks":[{"lastStatus":"STOPPED","containers":[{"name":"1theorycouk-shared-prod-back","exitCode":%s}]}]}\n' "$code" ;;
        'ecs update-service')
            printf '%s\n' "$@" >> "$TEST_TMP/updated" ;;
        *) printf 'Unexpected AWS call: %s %s\n' "$1" "$2" >&2; return 1 ;;
    esac
}
bash() { if [[ $1 == scripts/wait_ecs_prod.sh ]]; then return 0; fi; command bash "$@"; }
export -f git docker aws bash
for SCENARIO in success failure start_failure wait_failure; do
    export SCENARIO
    rm -f "$tmp/updated"
    status=0
    command bash "$root/scripts/deploy_aws_prod.sh" >"$tmp/output" 2>&1 || status=$?
    if [[ $SCENARIO == success ]]; then
        [[ $status == 0 && -f $tmp/updated ]] || { cat "$tmp/output"; exit 1; }
        grep -q '1theorycouk-shared-prod-back:99' "$tmp/updated" || { cat "$tmp/output"; exit 1; }
    else
        [[ $status != 0 && ! -f $tmp/updated ]] || { cat "$tmp/output"; exit 1; }
    fi
done
printf 'Prod migration deployment checks passed.\n'
