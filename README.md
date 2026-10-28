# UK Backend

Backend for the UK platform.

## Stack

- Python
- Django
- Django REST Framework
- PostgreSQL
- uv
- Docker
- Django Admin / Jazzmin

## Architecture

The backend is implemented as a Django monolith containing:

- Authentication and identity
- Customer cabinet APIs
- Packages, orders and subscriptions
- CRM
- Package entitlements and permissions
- Online Learning Platform
- Learning Admin
- Background processing
- Media/storage integrations

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

### Running with Docker

```bash
docker compose up                                        # Postgres + dev server on :8000
docker compose run --rm web python manage.py migrate     # run migrations
docker compose run --rm web python manage.py createsuperuser
```

The source code is mounted into the container, so edits apply without rebuilding. Run
`docker compose build` after changing dependencies.

The `Dockerfile` installs runtime dependencies only (`uv sync --frozen --no-dev`) and runs
gunicorn by default; compose overrides that with `runserver`. Migrations are never run on
container start, so scaling out can't run them concurrently.

### API

| Area | Paths |
|---|---|
| Health | `GET /api/healthcheck/` |
| Authentication | `POST /api/auth/register/`, `login/`, `logout/`, `token/refresh/`, `password/reset/`, `password/reset/confirm/`; `GET /api/auth/email/verify/`, `POST /api/auth/email/verify/resend/` |
| Google sign-in | `POST /api/auth/google/`, `POST /api/auth/google/link/` |
| Catalogue | `GET /api/packages/`, `GET /api/packages/<slug>/` |
| Site content | `GET /api/content/faqs/`, `GET /api/content/testimonials/` |
| Landing page | `GET /api/landing/`, `GET /api/landing/text/`, `GET /api/landing/media/` |
| Payments | `POST /api/payments/checkout/`, `POST /api/payments/promo-codes/validate/`, `GET /api/payments/orders/<uuid>/`, `POST /api/payments/webhook/stripe/` |
| Cabinet | `GET/PATCH /api/cabinet/profile/`, `POST/DELETE /api/cabinet/profile/photo/`, `POST /api/cabinet/email/change/`, `GET /api/cabinet/email/change/confirm/`, `POST /api/cabinet/password/change/`, `GET /api/cabinet/subscription/`, `GET /api/cabinet/learning-url/`, `GET /api/cabinet/orders/` |
| Notifications | `GET /api/notifications/`, `POST /api/notifications/read-all/`, `POST /api/notifications/<id>/read/` |
| Learning | `GET /api/learn/topics/`, `GET /api/learn/lessons/<slug>/`, `POST /api/learn/videos/<slug>/progress/` (and the rest of `/api/learn/`) |
| Documentation | `/api/swagger/`, `/api/redoc/`, `/api/schema/` |
| Admin | `/api/admin/` |

The catalogue and site-content reads are public and unpaginated: the pages that show them show
them all at once, and unpublished items are never returned. Everything else needs a token.

Clients send `Authorization: Bearer <access token>`. Access tokens last 5 minutes; refresh tokens
last 30 days with `remember_me`, otherwise 24 hours. Refreshing returns a new refresh token and
invalidates the old one.

In local development emails are printed to the container logs rather than sent, so verification
and password-reset links can be copied straight from `docker compose logs web`.

Google sign-in needs `GOOGLE_CLIENT_IDS` set to the OAuth client ids the frontends use. While it
is empty, those two endpoints refuse every token.

Payments need `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. Customers buy as guests: checkout
creates a pending order and sends them to Stripe's hosted page, and the order only becomes paid
when Stripe's signed webhook says so. To receive webhooks locally, forward them with the Stripe
CLI:

```bash
stripe listen --forward-to localhost:8000/api/payments/webhook/stripe/
```

### What happens after a payment

Stripe's webhook marks the order paid and queues a background job, which creates the account if
the buyer had none, grants access, and sends a welcome email with a single-use link to choose a
password plus a receipt, and tells the CRM managers that a student now has access. The job is safe
to repeat: Stripe redelivers events, and each step checks whether it has already run.

Access is two dates. `package_expires_at` ends learning access; `account_expires_at` is later by
`ACCOUNT_LIFETIME_COEFFICIENT` (1.5, editable in the admin), so a lapsed customer can still sign
in and buy again. Buying while access is live adds the new period onto the end, so nothing paid
for is lost.

Scheduled jobs run from the `beat` service: expiry reminders at 09:00, telling learners their
access has ended at 09:30, closing expired accounts at 00:30 (never staff accounts), fulfilling
lost orders every ten minutes, and telling the CRM about access it was never told about, hourly.
There is deliberately no job to revoke learning access, because access is derived from the dates
every time it is checked.

### Learner profile

`GET /api/cabinet/profile/` returns the signed-in learner's own details and `PATCH` updates them.
Both act on `request.user`: there is no account id in the URL or in the body, and `id`, `pk` and
`email` sent in a PATCH are ignored (the address has its own confirmation flow). A PATCH is
partial, so a request that mentions one field leaves every other one — including the marketing
preference and the photo — exactly as it was, and the response is the whole profile as it now
stands.

| Field | Accepts |
|---|---|
| `first_name`, `last_name`, `phone` | strings, as before |
| `postcode` | a UK postcode in any spacing or case; stored as `SW1A 1AA`. `""` clears it |
| `theory_test_date` | `YYYY-MM-DD`, or `null` when no test is booked |
| `confidence_level` | `not_confident`, `somewhat_confident`, `confident`, `very_confident`, or `""` for no answer |
| `marketing_email_opt_in` | `true` or `false` |
| `photo_url` | read-only; absolute URL of the photo, or `null` |
| `email`, `email_verified`, `has_google_auth` | read-only |

A rejected value answers 400 with DRF's field errors, for example
`{"postcode": ["Enter a valid UK postcode, for example SW1A 1AA."]}`.

`marketing_email_opt_in` defaults to false and is only ever true because the learner set it
themselves — registering, buying and editing the profile are none of them consent. Changing it
stamps `marketing_email_opt_in_at`, which is the record of when they chose and is read-only in the
admin so it cannot be backdated. Nothing sends marketing email yet; this only stores the choice.

`POST /api/cabinet/profile/photo/` takes a multipart body with one `photo` field and replaces any
photo already there; `DELETE` removes it. Both answer with the full profile. JPG, JPEG, PNG and
WebP up to 10 MB are accepted, and every upload is opened with Pillow — the filename and the
browser's content type are supplied by the client, so a renamed script or a truncated file is
refused with `{"detail": "That file is not a picture this site can show."}` and the photo already
on the account is left alone. Uploads are throttled at 20 an hour per account, because one
gunicorn worker serves the whole site and each one may be 10 MB. The file is stored through the configured storage backend (the
bucket in production, where URLs are signed and short-lived), under a random name in a directory
per account, and the file behind a replaced or removed photo is deleted so nothing is orphaned.

What is stored is never the file that arrived. See **Uploaded pictures carry no metadata** below.

### Learner order history

`GET /api/cabinet/orders/` returns the signed-in learner's own orders, newest first, as
`{"results": [...]}`. It needs a token and nothing else: access that has expired, been refunded or
never existed still reads its own history, because a receipt outlives what it bought.

```json
{
  "results": [
    {
      "id": "6f0b1b6e-6a1a-4a7e-9c5e-6f4a2b0d9c11",
      "created_at": "2026-10-26T10:00:00Z",
      "status": "paid",
      "package_name": "30 day access",
      "package_duration_days": 30,
      "original_price": "29.99",
      "discount_amount": "5.00",
      "final_price": "24.99",
      "currency": "GBP",
      "paid_at": "2026-10-26T10:00:42Z",
      "refunded_at": null
    }
  ]
}
```

`status` is the order's own status — `pending`, `paid`, `failed` or `refunded`. The amounts are the
ones copied onto the order at checkout, so a later price change or a deleted promo code cannot
rewrite what somebody was charged; `discount_amount` is what the promo code took off at the time.
Nothing about the payment itself is exposed: the Stripe session, payment intent and transaction ids
stay server-side, as does the email captured at checkout.

The list is capped at the newest 50, like notifications, because nothing in this API is paginated.
One query serves it: the package is joined in, so a long history costs no more than a short one.


### Learner video position

`POST /api/learn/videos/<slug>/progress/` saves where the signed-in learner is up to in a lesson
video, so playback can be resumed on any device. The slug is the **video block's** slug, not the
lesson's: one lesson can hold several videos, and each keeps its own position. Every `video` block
in `GET /api/learn/lessons/<slug>/` carries that `slug` with the learner's `progress` beside it, and
so does `videoProgress` on a lesson card in `GET /api/learn/topics/`.

```json
{ "positionSeconds": 205, "durationSeconds": 620 }
```

```json
{
  "slug": "braking-distances",
  "positionSeconds": 205.0,
  "durationSeconds": 620.0,
  "resumeAt": "03:25",
  "hasProgress": true,
  "finished": false,
  "updatedAt": "2026-10-26T10:00:42Z"
}
```

`positionSeconds` is required, `durationSeconds` optional — omitting it keeps the duration saved
earlier, and `position_seconds`/`duration_seconds` are accepted as aliases. A negative, non-numeric
or missing position answers 400, so an incomplete request cannot zero a real one; a position past
the end is clamped. Repeated calls update one row (learner and video are unique together, written
by `update_or_create` in a transaction), so posting every few seconds costs one UPDATE.

### Learner exam attempts

`GET /api/learn/exam-attempts/` returns every exam the signed-in learner has sat — practice exams
and mock tests together — newest first, in one request instead of one request per exam.

```json
{
  "count": 2,
  "attempts": [
    {
      "id": 412,
      "exam": { "id": 7, "slug": "mock-1", "title": "Mock test 1", "kind": "mock" },
      "completedAt": "2026-10-26T10:00:42Z",
      "score": 42,
      "total": 50,
      "percent": 84,
      "passed": true,
      "questionsPassed": true,
      "hazardPassed": true,
      "hazardScore": 44,
      "hazardMaxScore": 75,
      "timeTakenSeconds": 1820
    }
  ]
}
```

### Learner notifications

`GET /api/notifications/` returns the signed-in learner's notifications, newest first, as
`{"unread_count": n, "results": [...]}`. Each row carries `id`, `kind`, `title`, `text`, `link`
(a frontend route, or `null`), `created_at` and `is_read`. The count covers the whole account; the
list is capped at the newest 50, because nothing in this API is paginated.
`POST /api/notifications/<id>/read/` marks one as read and answers 404 for an id that does not
exist *or* belongs to somebody else; `POST /api/notifications/read-all/` marks the rest in one
UPDATE and answers `{"updated": n}`. Both are safe to repeat. All three need a token, and every
query is scoped to `request.user` — no learner id is ever read from a request.

What creates them:

| `kind` | Written by |
|---|---|
| `package_activated` | fulfilment, after a paid order is turned into access |
| `package_ending_soon` | the 09:00 expiry-reminder job, alongside the email |
| `package_expired` | the 09:30 job, for access that ended in the last two days and was not replaced or refunded |
| `streak_milestone` | a worker, after a day of study is recorded — at 3, 7, 14, 30, 60 and 100 days |

Each one carries an `event_key` that names the event rather than the message, unique per learner
in the database. That is what makes every trigger safe to reprocess: a redelivered Stripe webhook,
a reminder job rerun, or a retried Celery task finds the row already there and writes nothing.
Notification writing never fails the thing that triggered it — a purchase that could not write to
the bell has still granted access, and a reminder whose notification failed is still marked as
sent so the customer is not emailed twice.

There is no theory-test-date reminder. The profile now records `theory_test_date`, but nothing
reads it to schedule a notification; that is a decision for whoever adds the reminder.

### Uploaded pictures carry no metadata

A phone photograph carries the GPS fix of wherever it was taken, so a selfie taken at home is the
learner's address, and an image link hands that to anyone who opens it — staff included. Nothing
uploaded is therefore stored as it arrived: `apps.core.images.without_metadata` rewrites every
picture from its pixels and keeps only what affects how it renders (transparency, gamma, DPI, ICC
profile, compression, resolution, loop count and frame timing — handed to the encoder by name,
because most of Pillow's writers never read them off the image). EXIF, the GPS IFD, camera make
and model, timestamps, XMP and PNG text chunks are all gone because nothing carries them into the
new file.

### Running without Docker

Point `DATABASE_URL` at any PostgreSQL 16 database and `REDIS_URL` at a Redis instance, then:

```bash
uv sync
cp .env.example .env         # then adjust DATABASE_URL etc.
uv run python manage.py migrate
uv run python manage.py runserver
```

### Back office

The Django admin at `/api/admin/` is the back office, themed with Jazzmin.

Site content — the FAQs and the testimonials — is managed under **Site content**, on Django's own
model permissions rather than a role, so a superuser can edit it without granting anything first.
Each list edits the sort order and the published flag in place, and carries publish and unpublish
actions for a selection.

The landing page is managed under **Landing page**, on the same model permissions. Its wording is
a flat key → value store, so marketing can reword the page without a frontend deploy; the note
beside each key says where the text appears and is never sent to the site. Its pictures and videos
are a library with slots: a file only appears once staff assign it to a section and tick
*Published / active*, so a replacement can be uploaded and reviewed while the old one is still
live. Where a slot holds several active files the lowest display order wins, and the most recently
updated breaks a tie — which is why the activate action saves each row rather than updating them
in bulk.

Questions take their pictures either way. The media library still holds every file, and a
question or one of its answers can point at one already there, but the question form also carries
an upload box — one for the question, one beside each answer in *Question options* — so a picture
is attached in the same save that writes the question rather than in a trip to the library first.
Anything uploaded that way becomes an ordinary library asset, and the same file uploaded twice
finds the asset already there instead of making a second one. JPG, PNG, WebP and GIF, under 10 MB.

Videos carry subtitles. A video in the media library has a *Subtitle tracks* table on its page
where staff upload a WebVTT (`.vtt`) file and say which language it is in — British English is
offered first, and the other languages on the list are picked from the same selector. The tracks
hang off the library asset rather than off a lesson, so one upload covers every lesson, e-book
section and hazard clip that plays the file, and the API hands them out with the video's own URL.
Only videos show the table, only `.vtt` files whose contents really are WebVTT are accepted, and a
video with no tracks is served exactly as it was before.

Reaching the CRM is granted on purpose. `apps/staff` gates the areas holding customer data on
permissions actually recorded against an account, so being a Django superuser is not by itself a
way in — a superuser must grant themselves the permission first. That is least privilege by
default, not a wall against whoever administers the system.

Roles are declared in code, in each app's `roles.py`, and created by a migration:

| Role | May |
|---|---|
| CRM Manager | Look customers up and read their records |

`uv run python manage.py sync_roles` sets each role group's permissions to exactly what the code
declares, so permissions added by hand in the admin are taken back. No role carries
`crm.change_candidate` yet; grant it to an account directly, or add a role for it, once you decide
who may edit customer details.

When a purchase grants a student access, the staff who hold `crm.view_candidate` are emailed: the
student's name and address, the package, when they paid, how long access runs, when they
registered, the order reference and links to their CRM page and to the order. It is sent from
fulfilment, after the subscription exists, so registering tells nobody and an abandoned checkout
or failed payment tells nobody either.

A candidate's orders and access history appear on their page only for staff who also hold
`billing.view_order` / `entitlements.view_subscription`, so CRM access alone does not reveal
commercial history.

Staff changes to customer records go through `apps/crm/services.py`, which checks the permission
and writes an `AuditEvent` recording who changed what, from and to. Orders, subscriptions and the
audit trail itself are read-only in the admin: changing an order's price there would alter the
record of what somebody paid without moving any money.

### Settings

| Module | Used by |
|---|---|
| `config.settings.local` | `manage.py` (default) |
| `config.settings.test` | pytest |
| `config.settings.production` | `wsgi.py` / `asgi.py` (default); every deployed environment |

`manage.py` is the only entry point that reads `.env`. The production settings refuse to start
without `SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASE_URL` and `FRONTEND_BASE_URL`. See
`.env.example` for the full variable catalogue.

### Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest                # needs PostgreSQL; set DATABASE_URL if not on localhost:5432
```

CI (`.github/workflows/ci.yml`) runs these four commands and the deployment-waiter shell tests
on every pull request and on pushes to `develop`, `stage` and `main`.

### AWS Dev deployment

Jenkins `uk-backend-dev` runs `scripts/deploy_aws_dev.sh` with `ENV_VARIABLE=dev`.
It preserves the current Dev task settings and waits for the newly registered revision, not
just any healthy revision. The wait allows 30 minutes for startup and old-task draining, logs
progress every 15 seconds, and fails if ECS reports failure, rollback or a replacement deployment.
It never marks a timed-out rollout successful. If it times out, check ECS before retrying.

`ECS_DEPLOY_TIMEOUT_SECONDS` can override the limit (maximum 3600 seconds);
`ECS_DEPLOY_POLL_SECONDS` controls polling (default 15, maximum 60). Neither changes AWS settings.
Run the isolated AWS-stub regression checks with `bash tests/deploy/test_wait_ecs_dev.sh`.
