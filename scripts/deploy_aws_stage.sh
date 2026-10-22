#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

if [[ "${ENV_VARIABLE:-}" != "stage" ]]; then
    printf 'This deployment script only accepts ENV_VARIABLE=stage.\n' >&2
    exit 1
fi

region=eu-west-2
cluster=shared-spot-dev
service=1theorycouk-shared-stage-back
registry=501588779980.dkr.ecr.eu-west-2.amazonaws.com
repository="$registry/$service"
commit=$(git rev-parse --short HEAD)
image="$repository:$commit-stage"

docker build --file Dockerfile --tag "$image" .
aws ecr get-login-password --region "$region" |
    docker login --username AWS --password-stdin "$registry"
docker push "$image"

deploy_tmp_dir=$(mktemp -d -t uk-backend-stage.XXXXXX)
trap 'rm -f "$deploy_tmp_dir/current.json" "$deploy_tmp_dir/updated.json"; rmdir "$deploy_tmp_dir"' EXIT

current_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$service" --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --region "$region" --task-definition "$current_revision" \
    --include TAGS --output json > "$deploy_tmp_dir/current.json"

jq --arg image "$image" --arg container "$service" '
    .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
    | .containerDefinitions |= map(if .name == $container then .image = $image else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
' "$deploy_tmp_dir/current.json" > "$deploy_tmp_dir/updated.json"

new_revision=$(aws ecs register-task-definition --region "$region" \
    --cli-input-json "file://$deploy_tmp_dir/updated.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
    --task-definition "$new_revision" \
    --query 'service.{name:serviceName,taskDefinition:taskDefinition}' --output json
bash scripts/wait_ecs_stage.sh "$new_revision"
printf 'Stage backend deployed: %s\n' "$new_revision"
