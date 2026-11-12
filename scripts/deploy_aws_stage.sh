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

deploy_tmp_dir=$(mktemp -d -t uk-backend-stage.XXXXXX)
trap 'rm -f "$deploy_tmp_dir/current.json" "$deploy_tmp_dir/updated.json" "$deploy_tmp_dir/worker.json" "$deploy_tmp_dir/worker-updated.json"; rmdir "$deploy_tmp_dir"' EXIT

current_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$service" --query 'services[0].taskDefinition' --output text)
runtime_secret_arn=$(aws secretsmanager describe-secret --region "$region" \
    --secret-id uk-backend-stage-runtime --query ARN --output text)
aws ecs describe-task-definition --region "$region" --task-definition "$current_revision" \
    --include TAGS --output json > "$deploy_tmp_dir/current.json"
# Preserve the live Stage media configuration; refuse a release if it is absent.
video_bucket=$(jq -er --arg container "$service" '.taskDefinition.containerDefinitions[] | select(.name == $container) | .environment[] | select(.name == "VIDEO_S3_BUCKET_NAME") | .value | select(length > 0)' "$deploy_tmp_dir/current.json")
video_domain=$(jq -er --arg container "$service" '.taskDefinition.containerDefinitions[] | select(.name == $container) | .environment[] | select(.name == "CLOUDFRONT_DOMAIN") | .value | select(length > 0)' "$deploy_tmp_dir/current.json")
video_key_id=$(jq -er --arg container "$service" '.taskDefinition.containerDefinitions[] | select(.name == $container) | .environment[] | select(.name == "CLOUDFRONT_KEY_PAIR_ID") | .value | select(length > 0)' "$deploy_tmp_dir/current.json")
video_key_secret=$(jq -er --arg container "$service" '.taskDefinition.containerDefinitions[] | select(.name == $container) | .secrets[] | select(.name == "CLOUDFRONT_PRIVATE_KEY") | .valueFrom | select(length > 0)' "$deploy_tmp_dir/current.json")

jq --arg image "$image" --arg container "$service" --arg secret_arn "$runtime_secret_arn" \
    --arg bucket "$video_bucket" --arg domain "$video_domain" \
    --arg key_id "$video_key_id" --arg key_secret "$video_key_secret" '
    .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
    | .containerDefinitions |= map(
        if .name == $container then
            .image = $image
            | .command = ["bash", "./scripts/render-start.sh"]
            | .environment = [
                {name: "DJANGO_SETTINGS_MODULE", value: "config.settings.production"},
                {name: "ENVIRONMENT", value: "staging"},
                {name: "DEBUG", value: "False"},
                {name: "ALLOWED_HOSTS", value: "stage.1theorytest.co.uk"},
                {name: "FRONTEND_BASE_URL", value: "https://stage.1theorytest.co.uk"},
                {name: "BACKEND_BASE_URL", value: "https://stage.1theorytest.co.uk"},
                {name: "CORS_ALLOWED_ORIGINS", value: "https://stage.1theorytest.co.uk"},
                {name: "CSRF_TRUSTED_ORIGINS", value: "https://stage.1theorytest.co.uk"},
                {name: "TRUSTED_PROXY_COUNT", value: "1"},
                {name: "SECURE_SSL_REDIRECT", value: "True"},
                {name: "RUN_WORKER_IN_WEB", value: "true"},
                {name: "CELERY_QUEUES", value: "celery"},
                {name: "VIDEO_S3_BUCKET_NAME", value: $bucket},
                {name: "VIDEO_S3_REGION", value: "eu-west-2"},
                {name: "CLOUDFRONT_DOMAIN", value: $domain},
                {name: "CLOUDFRONT_KEY_PAIR_ID", value: $key_id},
                {name: "CLOUDFRONT_COOKIE_DOMAIN", value: ".1theorytest.co.uk"},
                {name: "VIDEO_ALLOW_UNSIGNED_PLAYBACK", value: "False"},
                {name: "WEB_CONCURRENCY", value: "1"},
                {name: "WEB_THREADS", value: "8"},
                {name: "PORT", value: "8000"},
                {name: "AWS_REGION", value: "eu-west-2"},
                {name: "AWS_ENDPOINT_URL_S3", value: "https://s3.eu-west-2.amazonaws.com"},
                {name: "S3_BUCKET_NAME", value: "1theorycouk-shared-stage"},
                {name: "S3_ENDPOINT_URL", value: "https://s3.eu-west-2.amazonaws.com"},
                {name: "S3_REGION", value: "eu-west-2"},
                {name: "LEARNING_URL", value: "https://stage.1theorytest.co.uk/learn/"},
                {name: "EMAIL_ENABLED", value: "false"},
                {name: "GOOGLE_CLIENT_IDS", value: ""},
                {name: "ADMIN_URL", value: "api/admin/"}
            ]
            | .secrets = ([
                "SECRET_KEY", "DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL",
                "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "POSTMARK_SERVER_TOKEN",
                "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"
            ] | map({name: ., valueFrom: ($secret_arn + ":" + . + "::")})) + [{name: "CLOUDFRONT_PRIVATE_KEY", valueFrom: $key_secret}]
        else . end
    )
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
' "$deploy_tmp_dir/current.json" > "$deploy_tmp_dir/updated.json"

docker build --file Dockerfile --tag "$image" .
aws ecr get-login-password --region "$region" |
    docker login --username AWS --password-stdin "$registry"
docker push "$image"

new_revision=$(aws ecs register-task-definition --region "$region" \
    --cli-input-json "file://$deploy_tmp_dir/updated.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
    --task-definition "$new_revision" \
    --query 'service.{name:serviceName,taskDefinition:taskDefinition}' --output json
bash scripts/wait_ecs_stage.sh "$new_revision"
# Update the dedicated worker to the identical image while preserving its stage runtime.
worker_service=uk-video-stage-videoworker
worker_revision=$(aws ecs describe-services --region "$region" --cluster "$cluster" \
    --services "$worker_service" --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --region "$region" --task-definition "$worker_revision" \
    --include TAGS --output json > "$deploy_tmp_dir/worker.json"
jq --arg image "$image" '
    .taskDefinition + (if ((.tags // []) | length) > 0 then {tags: .tags} else {} end)
    | .containerDefinitions |= map(if .name == "VideoWorker" then .image = $image else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)
' "$deploy_tmp_dir/worker.json" > "$deploy_tmp_dir/worker-updated.json"
worker_new_revision=$(aws ecs register-task-definition --region "$region" \
    --cli-input-json "file://$deploy_tmp_dir/worker-updated.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
aws ecs update-service --region "$region" --cluster "$cluster" --service "$worker_service" \
    --task-definition "$worker_new_revision" --query 'service.serviceName' --output text
bash scripts/wait_ecs_stage.sh "$worker_new_revision" "$worker_service"
printf 'Stage backend and video worker deployed: %s %s\n' "$new_revision" "$worker_new_revision"
