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
trap 'rm -f "$deploy_tmp_dir/current.json" "$deploy_tmp_dir/updated.json"; rmdir "$deploy_tmp_dir"' EXIT

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
aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
    --task-definition "$new_revision" \
    --query 'service.{name:serviceName,taskDefinition:taskDefinition}' --output json
bash scripts/wait_ecs_prod.sh "$new_revision"
printf 'Prod backend deployed: %s\n' "$new_revision"
