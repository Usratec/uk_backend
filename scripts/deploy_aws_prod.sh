#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

if [[ "${ENV_VARIABLE:-}" != "prod" ]]; then
    printf 'This deployment script only accepts ENV_VARIABLE=prod.\n' >&2
    exit 1
fi

region=eu-west-2
cluster=shared-spot-prod
service=1theorycouk-shared-prod-back
account=971450397487
registry="$account.dkr.ecr.eu-west-2.amazonaws.com"
repository="$registry/$service"
commit=$(git rev-parse --short HEAD)
image="$repository:$commit-prod"

# Prod is a different account from dev and stage; fail before building if the
# job is bound to the wrong credential.
resolved=$(aws sts get-caller-identity --region "$region" --query Account --output text)
if [[ "$resolved" != "$account" ]]; then
    printf 'Refusing to deploy: credentials resolve to %s, expected %s.\n' "$resolved" "$account" >&2
    exit 1
fi

docker build --platform linux/amd64 --file Dockerfile --tag "$image" .
aws ecr get-login-password --region "$region" |
    docker login --username AWS --password-stdin "$registry"
docker push "$image"

# Terraform's task definition pins :prod, so keep that tag on the current build;
# otherwise the next terraform apply rolls the service back to a stale image.
docker tag "$image" "$repository:prod"
docker push "$repository:prod"

deploy_tmp_dir=$(mktemp -d -t uk-backend-prod.XXXXXX)
trap 'rm -rf "$deploy_tmp_dir"' EXIT

current_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$service" --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --region "$region" --task-definition "$current_revision" \
    --include TAGS --output json > "$deploy_tmp_dir/current.json"

# Keep the deployed environment and task roles in private files, never in shell traces or argv.
# Always upsert GOOGLE_CLIENT_IDS to match terraform/1theorycouk prod so a prior empty or
# wrong value cannot be copied forward when this job only swaps the image.
google_client_ids='932236057244-n2ovo2i53a176klup4q60hu2ke7p3usn.apps.googleusercontent.com'
jq --arg image "$image" --arg container "$service" --arg google_client_ids "$google_client_ids" '
    .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
    | .containerDefinitions |= map(
        if .name == $container then
            .image = $image
            | .environment = (
                ((.environment // []) | map(select(.name != "GOOGLE_CLIENT_IDS")))
                + [{name: "GOOGLE_CLIENT_IDS", value: $google_client_ids}]
              )
        else . end
      )
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
' "$deploy_tmp_dir/current.json" > "$deploy_tmp_dir/updated.json"

new_revision=$(aws ecs register-task-definition --region "$region" \
    --cli-input-json "file://$deploy_tmp_dir/updated.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)

# Migrate once with the new image and the service's networking before rolling the web
# service. Prod's task command is gunicorn only (unlike stage's render-start.sh), so without
# this step schema changes never apply. Fail the release if migrate does not exit 0.
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
    printf 'Could not start the Prod migration task.\n' >&2
    exit 1
}
printf 'Waiting for Prod migration task %s.\n' "$task_arn"
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
printf 'Prod migrations completed successfully.\n'

live_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$service" --query 'services[0].taskDefinition' --output text)
if [[ "$live_revision" != "$current_revision" ]]; then
    printf 'ECS service %s changed during release preparation. Retry from its current revision.\n' "$service" >&2
    exit 1
fi

aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
    --task-definition "$new_revision" \
    --query 'service.{name:serviceName,taskDefinition:taskDefinition}' --output json
bash scripts/wait_ecs_prod.sh "$new_revision"
printf 'Prod backend deployed: %s\n' "$new_revision"
