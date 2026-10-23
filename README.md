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
| Cabinet | `GET/PATCH /api/cabinet/profile/`, `POST /api/cabinet/email/change/`, `GET /api/cabinet/email/change/confirm/`, `POST /api/cabinet/password/change/`, `GET /api/cabinet/subscription/`, `GET /api/cabinet/learning-url/` |
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

Scheduled jobs run from the `beat` service: expiry reminders at 09:00, closing expired accounts at
00:30 (never staff accounts), fulfilling lost orders every ten minutes, and telling the CRM about
access it was never told about, hourly. There is deliberately no job to revoke learning access,
because access is derived from the dates every time it is checked.

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
