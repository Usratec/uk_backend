# UK Stage deployment

AWS account: `501588779980`, region `eu-west-2`. The Stage site is `https://stage.1theory.co.uk/`.

## Backend

- GitHub repository: `Usratec/uk_backend`, branch `stage`.
- Jenkins job: `uk-backend-stage` (`ENV_VARIABLE=stage`).
- ECS service: `1theorycouk-shared-stage-back`.
- Database: `uk_backend_stage`, using the existing Stage database role. The database is isolated and starts empty; migrations and the role setup run at container start.
- Django host and frontend origins are Stage-only. Stage cache and task queue use dedicated Redis logical databases.

## Frontends

- GitHub repository: `Usratec/landing_uk_frontend`, branch `stage`.
- Jenkins job: `uk-stage-frontends`; choose `APP=landing`, `cabinet`, or `wikitheorie`.
- ECS services: `1theorycouk-shared-stage-front-landing`, `1theorycouk-shared-stage-front-cabinet`, and `1theorycouk-shared-stage-front-wikitheorie`.
- All three apps use `https://stage.1theory.co.uk/api` for the Stage backend.

## Verification

Each job builds an image tagged with the source revision, pushes it to the matching Stage ECR repository, updates only its Stage ECS service, and waits for the new ECS revision to become healthy. Stage-only deployment scripts reject any environment other than `stage`.

Stage email delivery is disabled until a Stage Postmark test token is available. Stripe keys are also empty, so payment flows need Stage test keys before they can be verified.

## Stage video

Stage uses `uk-video-stage-videoworker` for the `video` queue and keeps the
existing embedded general worker and scheduler on the `celery` queue. The
`uk-video-stage-media` CloudFormation stack owns private video storage, signed
CloudFront delivery, DNS, and scoped Jenkins permissions. The backend task
holds the media settings and a reference to the signing secret; do not print or
copy the private key. The Stage deploy script preserves these settings and
updates the backend and VideoWorker to the same image, checking both revisions.
A partial rollout should be repaired by redeploying or rolling both services
back together. The queue uses the existing Stage Redis logical database.

Before replacing the stack or changing video buckets, account for existing
video-source and HLS objects. Confirm that outstanding video jobs are drained
and copy existing objects before switching the web and worker configurations.
