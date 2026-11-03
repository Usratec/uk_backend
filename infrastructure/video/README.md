# UK-246 AWS video pipeline

`stack.yaml` adds a dedicated private video bucket, CloudFront OAC, trusted signing
key group, credential-aware CORS, media DNS, IAM policies, logs, and separate
Fargate video worker and beat services. It does not replace the existing web
service, Redis, database, or media bucket. Dev and Stage use separate stacks.
Production is intentionally excluded from this initial rollout.

The bucket is retained on stack deletion, blocks all public access, uses SSE-S3,
and aborts incomplete multipart uploads after seven days. Originals do not expire.
Only `learning/video-hls/*` is exposed through CloudFront, always requiring a
signature. Source files remain private and staff links use signed S3 requests.
The worker task role can write only the two video prefixes. When
`LegacyMediaBucketName` is supplied, it also receives read-only access to
`learning/video/*` in that existing AWS bucket so admin re-encoding keeps working.
Leave this empty only if no legacy sources need re-encoding; R2 sources require
an explicit migration or equivalent legacy credentials before re-encoding. Web keeps its
existing task role and receives a supplementary policy for those prefixes.

## Before provisioning

1. Authenticate to AWS account **501588779980**, region `eu-west-2`. Check the
   actual cluster, web service, execution role and task role; do not reuse an
   account merely because its profile is named `orangeuni-uk`.
2. Inspect existing services for ordinary Celery workers and embedded beat. There
   must be exactly one beat across the deployment. Keep `StartServices=false`
   until the previous scheduler is stopped. `CreateGeneralWorker=false` avoids
   creating duplicate workers by default.
3. Verify Redis is reachable from each task, uses `noeviction`, and has a durable
   configuration. A cache with snapshots alone cannot guarantee every queued job
   survives a restart. Reuse only after testing restart recovery; otherwise
   provision a dedicated persistent broker before enabling these services.
4. Use subnets/security groups with DB and Redis access. Private subnets need
   routes/endpoints for ECR, logs, Secrets Manager and S3. IAM alone does not
   establish network access. Workers have no public inbound ports or load balancer.
5. Select an environment-specific media host beneath the API's parent domain,
   and an ACM certificate covering it in **us-east-1**. The Route 53 zone must be
   the authoritative public zone. `CookieDomain` must cover API and media hosts.
   Use exact HTTPS staff and learner origins in `AllowedOrigins`.
6. Create an RSA-2048 key pair. Put only the private PEM in Secrets Manager;
   pass only its public PEM as `SigningPublicKey`. Keep the pair stable across
   deploys. Do not put the private PEM in parameters, source, logs or outputs.
   A customer-managed KMS key additionally needs `kms:Decrypt` on the execution
   role and a matching key policy. AWS-managed Secrets Manager keys need no
   additional KMS policy here.
7. Populate a runtime JSON secret with `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`
   `CELERY_BROKER_URL`, `FRONTEND_BASE_URL`, `STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET` and `POSTMARK_SERVER_TOKEN`. Match the current web
   runtime exactly. Include any additional integration settings and secrets
   needed by ordinary jobs before opting into `CreateGeneralWorker=true`.

## Deploy the infrastructure

Use an environment-specific parameter file outside Git. It contains names,
ARNs, network IDs, public PEM and the immutable ECR digest, never secret values.
All required parameters are declared in `stack.yaml`. `Cluster` is the cluster
**name**, not ARN (also used for CloudWatch dimensions). `ExecutionRoleArn` and
`ExecutionRoleName` must identify the same role. Use the role's final name,
excluding any IAM path, for both role-name parameters.

```sh
aws sts get-caller-identity --profile UK_PROFILE
aws cloudformation deploy --profile UK_PROFILE --region eu-west-2 \
  --stack-name uk-video-dev --template-file infrastructure/video/stack.yaml \
  --parameter-overrides file:///absolute/private/video-dev-parameters.json \
  --capabilities CAPABILITY_IAM --no-execute-changeset
```

Inspect the proposed change set and execute that exact change set when ready.
Start with services disabled. No existing media objects need moving: the new
`VIDEO_S3_BUCKET_NAME` routes only the pipeline's source/HLS prefixes to the new
bucket; all existing paths remain on their original storage.

## Web and background rollout

1. Build one image from this branch for linux/amd64, push to the existing ECR
   repository and use its immutable digest for web and background services.
2. Run `python manage.py migrate --noinput` as a one-off ECS task with the same
   DB settings. Wait for exit code zero. Do not run migrations on each worker.
3. Update web's task definition using `scripts/configure_video_web.py` below.
   It preserves existing environment/secrets, adds the stack's video settings
   and injects the signing key via `secrets`. It does not change the service.
4. Deploy the generated definition using the existing Jenkins ECS deployment
   process. Disable embedded workers/beat in web; run Gunicorn directly. The
   legacy `scripts/deploy_aws_dev.sh` updates only web and is insufficient by
   itself for this pipeline.
5. Stop any previous beat and update the stack with `StartServices=true`.
   Beat uses desired count 1, maximum 100%, minimum 0%, so the old scheduler
   stops before its replacement starts. Do not add autoscaling to beat.
   Video encodes use concurrency 1, 2 vCPU, 4 GiB memory, 50 GiB temporary disk,
   and on-demand Fargate to avoid Spot interruptions. Adjust after measuring
   the largest real clips. ECS gives only 120 seconds of shutdown grace; a
   longer encode can be interrupted and is recovered through job heartbeats.
6. On every release, update the stack `Image` **and** web to the same digest.
   The CloudFormation service circuit breaker reports failed deployments.
   CPU alarms are created but have no notification destination; connect the
   team's alarm actions. Running-count/ephemeral-disk alarms require Container
   Insights or application metrics and remain a rollout verification item.

Generate a private registration payload (the current task definition may
contain existing sensitive environment values):

```sh
umask 077
aws ecs describe-task-definition --task-definition CURRENT_WEB_ARN \
  > /absolute/private/current-web.json
python scripts/configure_video_web.py \
  --input /absolute/private/current-web.json \
  --container CURRENT_WEB_CONTAINER \
  --stack-outputs /absolute/private/video-stack-outputs.json \
  --region eu-west-2 --cookie-domain .dev.1theory.co.uk \
  --api-host dev.1theory.co.uk \
  --image ECR_REPOSITORY@sha256:DIGEST \
  --output /absolute/private/video-web.json
```

`video-stack-outputs.json` is the result of `describe-stacks` for exactly one
stack. Use the actual shared parent domain; the example is not a confirmed DNS
choice. Register the output via `--cli-input-json file://...` without printing it.
Jenkins needs scoped CloudFormation/ECS/ECR permissions and `iam:PassRole` for
these task roles; its build logs must never dump task environments or secrets.
This repository does not contain the live Jenkins job definition; that job must
call the stack rollout as well as the existing web rollout.

## Verification and rollback

Run CI (including real FFmpeg tests) before deploying. In AWS, validate a small
staff upload, multipart ETags, queued → processing → ready, all rendition
playlists/segments, signed-cookie playback, denied expired/missing cookies,
denied direct S3/CloudFront source access, and an existing legacy video/image.
Check actual Redis queue consumers: ordinary jobs on `celery`, only video worker
on `video`. Kill a test encode and verify beat requeues it. Restart the broker in
a controlled test and verify queued jobs remain. Browser/device HLS playback and
hazard scoring timing need a separate end-to-end check with the frontend owner.
The current transcoder changes frame rates above 30 FPS: high-frame-rate hazard
sources require a timing/scoring decision before use.

Rollback web and the stack `Image` to the previous known-good digest together;
stop the new beat before restarting an old scheduler. Keep storage and signing
keys. Do not roll back database migrations while new jobs or assets exist.

Current validation limitation: the available login opened account 210608587736,
not the UK deployment account. No resources or deployments have been changed by
this work; live inventory, existing worker/broker configuration and DNS choices
must be verified in the UK account before applying.

## Recurring Jenkins releases

`Jenkinsfile.video` is a separate Pipeline-from-SCM entrypoint for this branch.
It accepts a previously built and tested **linux/amd64** ECR digest, checks the
AWS account, selected environment, cluster and web service against the stack,
then runs `python -m scripts.rollout_video_aws`. Install `uv` on its Jenkins
agent and use its existing AWS role. The preview stage makes read-only calls.
The release stage registers the web definition, runs a one-off migration and
requires exit code zero, updates the background stack to the same image, waits
for CloudFormation, updates web, and verifies the requested web revision becomes
stable. It prints only service names, image digests and revision identifiers.

This release entrypoint is deliberately for **already bootstrapped** stacks
with `StartServices=true`. Initial creation still needs the account/network/DNS,
key and durable broker decisions above. Bind `WebServiceName` to the existing
backend service when creating the stack. Record the preview's previous web
revision and stack image for rollback. If a later phase fails, use those values
to restore both web and workers; this is not an atomic multi-service deployment.
