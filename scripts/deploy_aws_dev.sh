#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

if [[ "${ENV_VARIABLE:-}" != "dev" ]]; then
    printf 'This deployment script only accepts ENV_VARIABLE=dev.\n' >&2
    exit 1
fi

region=eu-west-2
cluster=shared-spot-dev
service=1theorycouk-shared-dev-back
registry=501588779980.dkr.ecr.eu-west-2.amazonaws.com
repository="$registry/$service"
commit=$(git rev-parse --short HEAD)
image="$repository:$commit-dev"

docker build --file Dockerfile --tag "$image" .
aws ecr get-login-password --region "$region" |
    docker login --username AWS --password-stdin "$registry"
docker push "$image"

deploy_tmp_dir=$(mktemp -d -t uk-backend-dev.XXXXXX)
trap 'rm -rf "$deploy_tmp_dir"' EXIT

current_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$service" --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --region "$region" --task-definition "$current_revision" \
    --include TAGS --output json > "$deploy_tmp_dir/current.json"

# Keep the deployed environment and task roles in private files, never in shell traces or argv.
jq --arg image "$image" --arg container "$service" '
    .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
    | .containerDefinitions |= map(if .name == $container then .image = $image else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
' "$deploy_tmp_dir/current.json" > "$deploy_tmp_dir/updated.json"

new_revision=$(aws ecs register-task-definition --region "$region" \
    --cli-input-json "file://$deploy_tmp_dir/updated.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
# Run the new image once with the service's own networking and capacity settings.
# A failed or unstartable task must prevent the service update.
aws ecs describe-services --region "$region" --cluster "$cluster" --services "$service" \
    --query 'services[0]' --output json > "$deploy_tmp_dir/service.json"
jq -e --arg cluster "$cluster" --arg task "$new_revision" --arg container "$service" '
    select(. != null and .status == "ACTIVE") |
    {cluster:$cluster, taskDefinition:$task, count:1,
     overrides:{containerOverrides:[{name:$container, command:["python","manage.py","migrate","--noinput"]}]}}
    + (if .networkConfiguration.awsvpcConfiguration then {networkConfiguration:.networkConfiguration} else {} end)
    + (if (.capacityProviderStrategy // [] | length) > 0 then {capacityProviderStrategy:.capacityProviderStrategy}
       elif .launchType then {launchType:.launchType} else {} end)
    + (if .platformVersion then {platformVersion:.platformVersion} else {} end)
' "$deploy_tmp_dir/service.json" > "$deploy_tmp_dir/run.json"
aws ecs run-task --region "$region" --cli-input-json "file://$deploy_tmp_dir/run.json" \
    --output json > "$deploy_tmp_dir/run-result.json"
task_arn=$(jq -er 'select((.failures // [] | length) == 0 and (.tasks | length) == 1) | .tasks[0].taskArn | select(type == "string" and length > 0)' "$deploy_tmp_dir/run-result.json") || {
    printf 'Could not start the Dev migration task.\n' >&2
    exit 1
}
printf 'Waiting for Dev migration task %s.\n' "$task_arn"
aws ecs wait tasks-stopped --region "$region" --cluster "$cluster" --tasks "$task_arn"
aws ecs describe-tasks --region "$region" --cluster "$cluster" --tasks "$task_arn" \
    --output json > "$deploy_tmp_dir/task-result.json"
if ! jq -e --arg container "$service" '
    (.failures // [] | length) == 0 and (.tasks | length) == 1
    and .tasks[0].lastStatus == "STOPPED"
    and ([.tasks[0].containers[] | select(.name == $container and .exitCode == 0)] | length) == 1
' "$deploy_tmp_dir/task-result.json" >/dev/null; then
    jq -r '.tasks[]? | "Migration task failed: \(.stoppedReason // "unknown reason")", (.containers[]? | select(.exitCode != 0) | "Container \(.name): exit=\(.exitCode // "unknown"), reason=\(.reason // "unknown")")' "$deploy_tmp_dir/task-result.json" >&2
    exit 1
fi
printf 'Dev migrations completed successfully.\n'
aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
    --task-definition "$new_revision" \
    --query 'service.{name:serviceName,taskDefinition:taskDefinition}' --output json
bash scripts/wait_ecs_dev.sh "$new_revision"
printf 'Dev backend deployed: %s\n' "$new_revision"
