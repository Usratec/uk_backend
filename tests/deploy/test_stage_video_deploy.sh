#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export TEST_TMP="$tmp" ENV_VARIABLE=stage
git() { if [[ $1 == rev-parse ]]; then echo abc1234; else command git "$@"; fi; }
docker() { if [[ $1 == login ]]; then cat >/dev/null; fi; }
aws() {
  case "$1 $2" in
    'ecr get-login-password') echo fixture ;;
    'ecs describe-services') if [[ "$*" == *uk-video-stage-videoworker* ]]; then echo worker:1; else echo web:1; fi ;;
    'secretsmanager describe-secret') echo runtime-arn ;;
    'cloudformation describe-stacks') echo '{"Stacks":[{"Outputs":[{"OutputKey":"VideoBucketName","OutputValue":"stage-bucket"},{"OutputKey":"CloudFrontDomain","OutputValue":"media-stage.1theorytest.co.uk"},{"OutputKey":"CloudFrontKeyPairId","OutputValue":"key-id"},{"OutputKey":"SigningSecretArn","OutputValue":"signing-arn"}]}]}' ;;
    'ecs describe-task-definition')
      if [[ "$*" == *worker:1* ]]; then echo '{"taskDefinition":{"family":"worker","containerDefinitions":[{"name":"VideoWorker","image":"old","environment":[{"name":"VIDEO_S3_BUCKET_NAME","value":"stage-bucket"}]}]}}';
      else echo '{"taskDefinition":{"family":"web","containerDefinitions":[{"name":"1theorycouk-shared-stage-back","image":"old","environment":[{"name":"VIDEO_S3_BUCKET_NAME","value":"stage-bucket"},{"name":"CLOUDFRONT_DOMAIN","value":"media-stage.1theorytest.co.uk"},{"name":"CLOUDFRONT_KEY_PAIR_ID","value":"key-id"}],"secrets":[{"name":"CLOUDFRONT_PRIVATE_KEY","valueFrom":"signing-arn"}]}]}}'; fi ;;
    'ecs register-task-definition')
      for arg in "$@"; do if [[ $arg == file://* ]]; then file=${arg#file://}; fi; done
      if jq -e '.family == "worker"' "$file" >/dev/null; then
        jq -e '.containerDefinitions[0] | .image | endswith(":abc1234-stage")' "$file" >/dev/null
        jq -e '.containerDefinitions[0].environment[] | select(.name == "VIDEO_S3_BUCKET_NAME" and .value == "stage-bucket")' "$file" >/dev/null
        echo worker:2
      else
        jq -e '.containerDefinitions[0] | ([.environment[] | select(.name == "CELERY_QUEUES" and .value == "celery")] | length == 1) and ([.environment[] | select(.name == "VIDEO_S3_BUCKET_NAME" and .value == "stage-bucket")] | length == 1) and ([.secrets[] | select(.name == "CLOUDFRONT_PRIVATE_KEY" and .valueFrom == "signing-arn")] | length == 1)' "$file" >/dev/null
        echo web:2
      fi ;;
    'ecs update-service') if [[ "$*" == *uk-video-stage-videoworker* ]]; then touch "$TEST_TMP/worker"; else touch "$TEST_TMP/web"; fi; echo ok ;;
    *) echo "Unexpected AWS operation: $1 $2" >&2; return 1 ;;
  esac
}
bash() { if [[ $1 == scripts/wait_ecs_stage.sh ]]; then return 0; fi; command bash "$@"; }
export -f git docker aws bash
cd "$root"
command bash scripts/deploy_aws_stage.sh >"$tmp/output" 2>&1 || { cat "$tmp/output"; exit 1; }
[[ -f "$tmp/web" && -f "$tmp/worker" ]]
echo 'Stage video deployment checks passed.'
