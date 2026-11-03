# UK-246 handover review

Reviewed PR #61 at `2f439476fd1c77d01289d697c0d429cf5eb5a285`, UK-246,
UK-159 comment 14948, the supplied video processing architecture document, and
the repository's Docker, Compose, CI and existing AWS deployment scripts.

The implementation follows the agreed FFmpeg/Celery approach: direct S3
uploads, multipart verification, a dedicated queue, job claims, heartbeats,
HLS renditions and subscriber-authorized CloudFront cookies. MediaConvert and
SQS are not needed for this implementation.

## Addressed in this branch

- Additive CloudFormation stack for private S3, CloudFront OAC and trusted key
  group, credential-aware CORS, DNS, IAM, ECS video worker and singleton beat.
- Dedicated video storage through `VIDEO_S3_BUCKET_NAME`, preserving existing
  media storage and legacy video playback. Optional read-only legacy AWS bucket
  access allows admin re-encoding without moving the originals.
- Separate local workers, persistent Redis volume with AOF/noeviction, explicit
  video concurrency and disk/tool/broker startup checks.
- Infrastructure lint and real FFmpeg integration tests in CI.
- Jenkins release entrypoint and read-only release preview; migration failure
  prevents service updates, and image/revision checks prevent false success.
- CloudFront signing key injected through ECS secrets, never plaintext in the
  generated web settings or printed release plan.

## Requires live UK account verification

The connector had no AWS credentials. The available local profile authenticated
successfully but opened account `210608587736`, while UK deployments target
`501588779980`. No AWS resources, DNS, Jenkins jobs or remote branches have been
changed. The local profile name alone is not reliable account evidence.

Before creating resources, inspect the actual web task roles, existing ordinary
worker/scheduler, Redis persistence and queue consumers, VPC routes, authoritative
zone and certificate. Populate the stack parameters from those verified values.
The adjacent legacy Terraform checkout is not proof of current AWS state.

See `infrastructure/video/README.md` for bootstrap, release, validation and
rollback steps. The stack starts background services at zero by default until
broker/network/migration and scheduler ownership checks are complete.

## Application requirements to confirm before acceptance

1. `output_frame_rate()` reduces sources above 30 FPS. UK-159 requires hazard
   re-encoding to preserve frames and timing. The current code does not select a
   hazard-specific encoding policy. Validate real scoring clips before enabling
   this path for them, and decide whether high-FPS hazard uploads must be rejected
   or processed without frame reduction.
2. The encoder produces H.264/AAC MPEG-TS HLS and keeps the original; it does not
   create a separate optimized progressive MP4. Confirm whether UK-246's MP4 and
   Fast Start requirements also require an optimized progressive download.
3. CPU alarms exist, but notification actions, running-count/disk monitoring,
   broker restart recovery, signed-cookie denial checks, browser/device playback
   and real hazard timing remain live rollout checks. They cannot be established
   by mocked tests or static infrastructure validation.

## Local validation

- Docker image built successfully with FFmpeg and ffprobe.
- Full suite in Docker/PostgreSQL: 2,093 passed, one skipped (the image lacks Git).
- Ruff lint/format and mypy passed.
- CloudFormation validation with cfn-lint 1.59.0 passed.
- Existing ECS deployment-waiter shell scenarios passed.
- AWS-backed end-to-end tests and the Jenkins job have not been run.
