#!/usr/bin/env bash
set -euo pipefail
set +x

expected_revision=${1:?Pass the newly registered Stage task-definition ARN}
if [[ ! "$expected_revision" =~ ^arn:aws:ecs:eu-west-2:501588779980:task-definition/1theorycouk-shared-stage-back:[0-9]+$ ]]; then
    printf 'Refusing to wait for a task definition outside UK Stage.\n' >&2
    exit 1
fi

timeout=${ECS_DEPLOY_TIMEOUT_SECONDS:-1800}
interval=${ECS_DEPLOY_POLL_SECONDS:-15}
for value in "$timeout" "$interval"; do
    if [[ ! "$value" =~ ^[1-9][0-9]{0,4}$ ]]; then
        printf 'Deployment timeout and polling interval must be positive integers.\n' >&2
        exit 1
    fi
done
if (( timeout > 3600 || interval > 60 )); then
    printf 'Maximum timeout is 3600 seconds; maximum polling interval is 60 seconds.\n' >&2
    exit 1
fi

deadline=$((SECONDS + timeout))
read_failures=0
state='{}'
printf 'Waiting up to %ss for Stage revision %s.\n' "$timeout" "$expected_revision"

while true; do
    if state=$(AWS_MAX_ATTEMPTS=3 aws ecs describe-services --region eu-west-2 \
        --cluster shared-spot-dev --services 1theorycouk-shared-stage-back \
        --cli-connect-timeout 10 --cli-read-timeout 30 --output json \
        --query 'services[0].{taskDefinition:taskDefinition,desiredCount:desiredCount,runningCount:runningCount,pendingCount:pendingCount,deployments:deployments[].{taskDefinition:taskDefinition,status:status,rolloutState:rolloutState,rolloutStateReason:rolloutStateReason},events:events[:3].message}'); then
        read_failures=0
        if ! jq -e 'type == "object" and (.taskDefinition | type == "string")' \
            <<< "$state" >/dev/null; then
            printf 'AWS did not return the requested Stage service.\n' >&2
            exit 1
        fi
        if ! jq -e --arg expected "$expected_revision" '.taskDefinition == $expected' \
            <<< "$state" >/dev/null; then
            printf 'Stage deployment was replaced or rolled back; refusing to report success.\n' >&2
            exit 1
        fi
        if jq -e --arg expected "$expected_revision" \
            'any(.deployments[]; .taskDefinition == $expected and .rolloutState == "FAILED")' \
            <<< "$state" >/dev/null; then
            jq -r '.deployments[] | select(.rolloutState == "FAILED") | .rolloutStateReason' \
                <<< "$state" >&2
            jq -r '.events[]? | "ECS event: \(.)"' <<< "$state" >&2
            exit 1
        fi
        if jq -e --arg expected "$expected_revision" '
            .desiredCount > 0 and .runningCount == .desiredCount and .pendingCount == 0
            and (.deployments | length) == 1
            and .deployments[0].taskDefinition == $expected
            and .deployments[0].status == "PRIMARY"
            and .deployments[0].rolloutState == "COMPLETED"
        ' <<< "$state" >/dev/null; then
            printf 'Stage revision is stable: %s\n' "$expected_revision"
            exit 0
        fi
        jq -r '"Stage rollout: running=\(.runningCount)/\(.desiredCount), pending=\(.pendingCount), deployments=\(.deployments | length)"' \
            <<< "$state"
    else
        read_failures=$((read_failures + 1))
        printf 'Could not read Stage deployment status (%s/3).\n' "$read_failures" >&2
        if (( read_failures >= 3 )); then exit 1; fi
    fi

    remaining=$((deadline - SECONDS))
    if (( remaining <= 0 )); then
        printf 'Stage deployment did not complete within %ss; check ECS before retrying.\n' "$timeout" >&2
        if [[ -n "$state" ]]; then jq -r '.events[]? | "ECS event: \(.)"' <<< "$state" >&2; fi
        exit 1
    fi
    sleep_for=$interval
    if (( remaining < sleep_for )); then sleep_for=$remaining; fi
    sleep "$sleep_for"
done
