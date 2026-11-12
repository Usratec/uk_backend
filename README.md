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
- Blog and SEO content
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
| Site content | `GET /api/content/faqs/`, `GET /api/content/testimonials/`, `POST /api/content/contact/` |
| Landing page | `GET /api/landing/`, `GET /api/landing/text/`, `GET /api/landing/media/` |
| Blog | `GET /api/blog/`, `GET /api/blog/<slug>/`, `GET /api/blog/<slug>/cover-image/`, `GET /api/blog/<slug>/social-image/`, `GET /api/blog/images/<uuid>/` |
| Sitemap | `GET /api/sitemap.xml` |
| Payments | `POST /api/payments/intent/`, `POST /api/payments/checkout/`, `POST /api/payments/promo-codes/validate/`, `GET /api/payments/orders/<uuid>/`, `POST /api/payments/webhook/stripe/` |
| Cabinet | `GET/PATCH /api/cabinet/profile/`, `POST/DELETE /api/cabinet/profile/photo/`, `POST /api/cabinet/email/change/`, `GET /api/cabinet/email/change/confirm/`, `POST /api/cabinet/password/change/`, `GET /api/cabinet/subscription/`, `GET /api/cabinet/learning-url/`, `GET /api/cabinet/orders/` |
| Notifications | `GET /api/notifications/`, `POST /api/notifications/read-all/`, `POST /api/notifications/<id>/read/` |
| Learning | `GET /api/learn/topics/`, `GET /api/learn/lessons/<slug>/`, `POST /api/learn/videos/<slug>/progress/`, `GET /api/learn/offline/practice/`, `POST /api/learn/answers/batch/` (and the rest of `/api/learn/`) |
| Public e-books | `GET /api/learn/ebook/public/`, `GET /api/learn/ebook/public/samples/<slug>/`, `POST /api/learn/ebook/public/sample-request/` |
| Documentation | `/api/swagger/`, `/api/redoc/`, `/api/schema/` |
| Admin | `/api/admin/` |

The catalogue, site-content and public e-book reads are public and unpaginated: the pages that show
them show them all at once, and unpublished items are never returned. Everything else needs a
token.

The blog is the one public list that *is* paginated, because it grows without limit: `GET /api/blog/`
returns ten posts a page (`?page=`, `?page_size=` up to 50) and filters on `?category=<slug>` and
`?featured=true|false`. `GET /api/blog/<slug>/` adds the post's rich text. Both return only posts
that are published *and* whose publish date has passed, so a draft, an unpublished post and a post
scheduled for next week are all 404 — the status and the date are checked in the query, not after it.

Each post carries what the blog design shows: `excerpt` (the summary on the cards, written
separately from `meta_description`, which is for search engines), `cover_image`
(`{url, alt, width, height}`, or null — the cover on the cards and at the top of the article, which
is not the sharing image in `social_image_url`), `reading_time` in minutes, `featured` for the
Featured read at the top of the blog, and `updated_at` for `<lastmod>`. The detail response adds
`study_tip` (`{title, text, button_label, button_link}`, or null) for the Study tip box under the
article. Reading time is worked out from the content on every save, so the listing carries it
without the content; a cover needs a description, and a study tip needs both a title and its words,
with a button label and link that stand or fall together.

When a post's slug changes, the old address keeps working: it answers `301 Moved Permanently` with a
`Location` header for the post's new API address and a body of
`{"detail": …, "slug": "<new-slug>", "path": "/blog/<new-slug>/"}`, so the frontend can send the
reader on and search engines see a permanent move. Every slug a post has ever had is kept, redirects
always point at the post rather than at another slug (so there are no chains and no loops), and a
slug taken by a live post always wins over a redirect of the same name.

`GET /api/sitemap.xml` lists the published blog posts as addresses on `FRONTEND_BASE_URL`, because
the pages themselves are rendered by the storefront. Drafts, unpublished posts and posts whose
publish date has not arrived are left out. A post with no `canonical_url` is listed at
`/blog/<current slug>/`; one whose canonical URL points at the storefront is listed at that path
instead; one canonical to another site is left out altogether, since the original lives elsewhere.
Each app with public pages declares its own entries in `<app>/sitemaps.py`, collected by
`apps/core/sitemaps.py`.

Clients send `Authorization: Bearer <access token>`. Access tokens last 5 minutes; refresh tokens
last 30 days with `remember_me`, otherwise 24 hours. Refreshing returns a new refresh token and
invalidates the old one.

In local development emails are printed to the container logs rather than sent, so verification
and password-reset links can be copied straight from `docker compose logs web`.

Google sign-in needs `GOOGLE_CLIENT_IDS` set to the OAuth client ids the frontends use. While it
is empty, those two endpoints refuse every token.

Payments need `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. There are two ways to pay, and
neither of them lets the browser decide what anything costs — the price is always worked out from
the package and the promo code by `apps/billing/pricing.py`, and an order only becomes paid when
Stripe's signed webhook says so.

* **Embedded** (`POST /api/payments/intent/`, signed in). The card is taken on our own checkout
  page with Stripe's Payment Element, so card details never reach this server, and there is no
  redirect. Returns a `client_secret` the browser confirms, then the page polls
  `GET /api/payments/orders/<uuid>/`. The outcome arrives as `payment_intent.succeeded` or
  `payment_intent.payment_failed`.
* **Redirect** (`POST /api/payments/checkout/`, open to guests). Creates a pending order and sends
  the customer to Stripe's hosted page. The outcome arrives as `checkout.session.completed`. Kept
  as the fallback while the embedded flow is being rolled out.

Both end in the same fulfilment. The embedded intent is created with
`allowed_payment_method_types: ["card"]`, which has to match the types the frontend builds its
Payment Element with or Stripe refuses to confirm the payment. Apple Pay and Google Pay are not
listed there because they are not payment method types of their own — both are cards, and `card`
is what carries them. Each paying domain still has to be registered with Stripe before the wallet
buttons appear, in the dashboard or with:

```bash
uv run python manage.py register_stripe_apple_pay_domain stage.1theory.co.uk
```

To receive webhooks locally, forward them with the Stripe CLI:

```bash
stripe listen --forward-to localhost:8000/api/payments/webhook/stripe/
```

### Contact form

`POST /api/content/contact/` takes the contact page's form and delivers it to the support inbox.
It is public: a visitor who is not signed in may send a message, and no token is required.

```json
{ "name": "Ada Lovelace", "email": "ada@example.com", "topic": "billing", "message": "My card was charged twice." }
```

`name` is 2–60 characters, `email` must be a valid address, `message` is required and at most
5000 characters, and `topic` is optional — one of `account`, `billing`, `course`, `other`.
Leading and trailing whitespace is trimmed from all three text fields. A success is `201` with
`{"message": "Your message has been sent successfully."}`; a bad form is `400` with DRF's
field-level errors (`{"email": ["Enter a valid email address."]}`).

### Purchase consent

`POST /api/payments/checkout/` requires `terms_accepted: true`,
`immediate_access_accepted: true` (JSON booleans), and `consent_version` (up to 32 characters).
Missing or invalid values produce field errors with status 400, without creating an order or
contacting Stripe. Clients must send the version of the exact wording shown to the buyer.
The two checkboxes remain separate; accepting Terms alone never grants immediate-access consent.
The successful response is unchanged.

Before enabling purchases, add a version in **Django admin → Billing → Purchase consent versions**.
Enter the version sent by checkout, both checkbox texts, and the Terms and Privacy Policy versions.
The wording must match the frontend, and the document versions must identify the actual documents shown.
For `2026-11-draft`, UK-315 specifies these checkbox texts:

- “I have read the privacy policy and agree to the terms of 1theory.”
- “I want my access to start straight away, and I understand that once it starts I lose my 14-day right to cancel.”

Create a new version whenever wording or document versions change, and coordinate its identifier
with the frontend. Saved texts and document versions are read-only. Clear **Is active** once clients
no longer submit a version. Order snapshots remain available in the order admin.

Existing `PURCHASE_CONSENT_VERSIONS` environment entries continue to work for identifiers absent
from the admin. To move one into the admin, register the same identifier with identical wording and
document versions. An inactive admin entry also disables the matching environment entry. Admin
changes take effect without a restart. Unavailable consent configuration returns 503. An unknown
version when consent is configured, or an inactive version, returns 400 on `consent_version`.

The order stores both accepted values, server timestamp, consent version, exact checkbox wording
and Terms/Privacy versions in the same transaction as the purchase attempt. A guest order has no
user until the normal paid-order fulfilment links the account. Consent details are visible
read-only in the order admin and are not exposed by public order-status responses.

Payment confirmation emails use the order's saved immediate-access wording in both text and HTML,
so later configuration changes cannot rewrite what the buyer accepted. Historical orders without
a recorded consent omit this section. Failed Stripe requests and failed or cancelled payments
retain the snapshot with the order; the existing cleanup removes unpaid/failed orders and their
consent after 15 days. Paid and refunded orders follow their existing retention.

The validation and snapshot resolver are reusable by the future UK-330 PaymentIntent endpoint;
this change does not add that endpoint. Stage verification after deployment: configure the real
legal versions, coordinate the matching FE consent version, then check a successful purchase,
field-error responses, read-only admin details and both email formats.

### What happens after a payment

Stripe's webhook marks the order paid and queues a background job, which creates the account if
the buyer had none, grants access, and sends a welcome email with a single-use link to choose a
password plus a receipt, and tells the CRM managers that a student now has access. The job is safe
to repeat: Stripe redelivers events, and each step checks whether it has already run.

Access is two dates. `package_expires_at` ends learning access; `account_expires_at` is later by
`ACCOUNT_LIFETIME_COEFFICIENT` (1.5, editable in the admin), so a lapsed customer can still sign
in and buy again. Buying while access is live adds the new period onto the end, so nothing paid
for is lost.

Scheduled jobs run from the `beat` service: publishing blog posts whose time has come every five
minutes, expiry reminders at 09:00, telling learners their
access has ended at 09:30, closing expired accounts at 00:30 (never staff accounts), fulfilling
lost orders every ten minutes, telling the CRM about access it was never told about, hourly, and
delivering contact messages that are still pending, hourly.
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

### Offline practice in the PWA

`GET /api/learn/offline/practice/` returns every question normal practice can ask the signed-in
learner — live, and inside their package — with the answers in it, so the PWA can mark its own
work offline. No plan answers 402. Normal practice only: no exam endpoint changes, and exams
cannot be sat offline.

```json
{
  "version": "3f8c1a09d4e27b65",
  "updatedAt": "2026-11-01T09:12:00Z",
  "total": 742,
  "questions": [
    {
      "id": 123, "key": "distraction", "topic": "road-signs", "type": "single",
      "prompt": "Which of these could distract you while driving?",
      "media": { "kind": "sign", "code": "give-way" },
      "options": [{ "id": "a", "text": "A phone call" }, { "id": "b", "text": "A clear road" }],
      "pick": 1,
      "correctIds": ["a"],
      "explanation": "It takes your attention off the road.",
      "lesson": { "slug": "distractions", "title": "Distractions" }
    }
  ],
  "signs": { "give-way": { "code": "give-way", "name": "Give way", "spec": {} } },
  "nextAfter": null
}
```

- **Versioning.** Store `version` and compare it with the next response's to decide whether to
  download again. It changes when anything in the pack does, including a question leaving it.
- **Paging.** Without `limit` the whole pack comes back at once and `nextAfter` is null; that is
  the expected way to fetch it. With `limit` (1–500), follow
  `nextAfter` as `after` until it is null. `total` is the whole pack either way.
- **Images expire.** An `image` is a short-lived signed URL, so cache the bytes, not the URL.
  Signs travel in the `signs` map and are drawn by the frontend.

`POST /api/learn/answers/batch/` records answers given offline against the days they were given
on. The body is a JSON array of at most 500 answers; `{"answers": [...]}` is accepted too.

```json
[
  { "clientId": "offline-001", "questionId": 123, "selected": ["a"], "answeredAt": "2026-11-02T14:30:00Z" },
  { "clientId": "offline-002", "questionId": 124, "selected": ["b"], "answeredAt": "2026-11-02T14:35:00Z" }
]
```

```json
{
  "results": [
    { "clientId": "offline-001", "status": "synced", "correct": true, "correctIds": ["a"],
      "answeredAt": "2026-11-02T14:30:00Z" },
    { "clientId": "offline-002", "status": "rejected",
      "error": { "code": "answered_at_in_future", "message": "The answer timestamp cannot be in the future." } }
  ]
}
```

- **Marking.** Decided on the server from the question rows. There is no `correct` field in the
  request.
- **Per item.** One result per submitted item, in the order sent, so one bad item never costs the
  others. `status` is `synced`, `already_synced` or `rejected`; a rejection names a `code`:
  `malformed`, `answered_at_not_timezone_aware`, `answered_at_in_future`, `answered_at_too_old`,
  `no_access_on_that_date`, `question_not_found`, `invalid_selection`, `client_id_conflict`.
- **Study dates.** Progress, mistakes, weak topics and streaks are all worked out from the attempt
  rows, so an answer counts towards its own `answeredAt` whatever order the days arrive in.
  `answeredAt` needs a timezone offset, may not be in the future (two minutes' allowance for a
  fast clock) and may not be more than 30 days old.
- **Historical access.** The learner must be able to practise the question now *and* have held a
  plan covering its chapter on the study date. A period's start counts, its expiry does not, and
  revoked periods pay for nothing.
- **Idempotency.** `clientId` is the key, scoped to the learner by a unique constraint, so retries
  cannot double-count. A second send reports `already_synced` and changes nothing. The same
  `clientId` with a different question, selection or timestamp is refused as `client_id_conflict`.

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

### The public e-book landing page

Three endpoints serve the e-book landing page to visitors with no account. They are the only part
of the learning material anybody can reach without a token, so they are built from their own
whitelist of fields rather than by filtering the student's responses.

`GET /api/learn/ebook/public/` returns every published e-book with its chapter outline:

```json
{
  "ebooks": [
    {
      "slug": "study-guide",
      "title": "Study guide",
      "description": "Every rule, with room to write.",
      "sections": [
        {
          "slug": "chapter-one",
          "number": 1,
          "title": "Introduction",
          "summary": "Learn the fundamentals.",
          "isFreeSample": true,
          "samplePdfUrl": "https://api.example.com/api/learn/ebook/public/samples/chapter-one/"
        },
        {
          "slug": "chapter-two",
          "number": 2,
          "title": "Advanced concepts",
          "summary": "Explore advanced concepts.",
          "isFreeSample": false,
          "samplePdfUrl": null
        }
      ]
    }
  ]
}
```

That is the whole response. No chapter text, no cover, no reading progress, no link to the book's
own document and nothing about any account — the query does not even read the `body_html` column.
`isFreeSample` is the designation staff made in the back office; `samplePdfUrl` is null until the
sample PDF for that chapter has actually been uploaded, so the page can tell the two apart instead
of showing a button that cannot work.

`GET /api/learn/ebook/public/samples/<slug>/` hands over that chapter's PDF, with no token. The slug names a row, never
a path, and it is looked up only among live chapters of live books that staff flagged *and* gave a
sample PDF of their own — anything else, including the chapters of the same book, answers 404. With
a bucket configured the endpoint redirects to a short-lived signed URL for that one object, so the
bytes never pass through the single worker.

The sample is a separate file from the book, uploaded on the chapter under **The free sample** and
stored under its own `learning/ebook-sample/` prefix. The book's own document is untouched by all
of this: it is still served only to buyers, through `GET /api/learn/ebook/<slug>/`, as a signed URL
that expires. Nothing in the public path reads it, and there is no fallback to it when a sample is
missing. One chapter per book may be given away, enforced by a database constraint.

`POST /api/learn/ebook/public/sample-request/` takes the "read chapter one for free" form with
`{"email": "...", "ebookSlug": "study-guide", "studyTipsConsent": true}` and emails the public link above — never the book's own URL. It answers 201 with
`{"message": ..., "samplePdfUrl": ...}`, 404 for a book that is not published and 409 for one with
no free chapter, and is throttled to 10 requests an hour per IP address. `studyTipsConsent` is
recorded only when the visitor ticks it, dated as the record of consent; left out it is false, and
it is never a condition of the download, which needs no email address at all. The requests are
listed read-only in the back office under **Ebook sample requests**.

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

### Direct video uploads and HLS streaming

Videos no longer pass through Django. The browser is given short-lived permission to write **one**
object into the private bucket, the backend checks what actually arrived, and a Celery worker turns
it into an HLS ladder with ffmpeg. Playback is authorized per learner and served by CloudFront.

Each run produces two things under one run-scoped prefix: the adaptive ladder (`master.m3u8` plus a
360p/720p/1080p variant per rung the source is big enough for) and a single `video.mp4` written with
`-movflags +faststart`, so its `moov` atom is at the front and a player can start before the file
has finished downloading. `GET /api/learn/media/<id>/playback/` returns the manifest as `url` and
that file as `mp4Url`; the signed cookies on that response cover the whole prefix, so they cover
both.

**The frame rate is never changed.** A hazard clip's hazard windows are counted in frames, so the
pipeline encodes every output at exactly the rate the source arrived at — `fps=30000/1001` stays
`30000/1001`, not 30 — and a rate outside `VIDEO_MIN_FRAME_RATE`–`VIDEO_MAX_FRAME_RATE` is refused
with a message saying what to re-export at, rather than being quietly halved or capped to fit.

Staff upload a video on **Media assets → Add media asset** and it is queued for processing as the
page saves — the job is dispatched on commit, so the worker can never read a row that is not there
yet. The asset shows `queued`, then `processing`, then `ready` in the list, with the reason in the
admin if a run fails. Replacing the file on an existing asset queues the new one; renaming it does
not. While a first upload is being transcoded the learner is served its source file, as the site
did before there was any transcoding, so uploading a lesson video does not take it off the site for
the length of an encode.

#### Encrypted segments

Segments are AES-128 encrypted, so a `.ts` file lifted off the wire will not play in anything. Each
video has its own key, held in the database and **never** written to the bucket or served through
CloudFront — the encoder writes the key to a directory outside the one that gets uploaded. The
playlist points at this API:

```
#EXT-X-KEY:METHOD=AES-128,URI="https://api.example.com/api/learn/media/<id>/key/"
```

`GET /api/learn/media/<id>/key/` answers with the raw 16 bytes as `application/octet-stream` and
`Cache-Control: private, no-store`, for a signed-in learner who may watch *that* video — the same
check playback uses — and 401/403 otherwise.

Be clear about what this buys: it stops casual copying, not a determined ripper. Anyone allowed to
watch can read the key out of their own browser. It is not DRM and should not be described as such.

### PWA Web Push

Browser push is separate from native-app Firebase tokens. Generate one stable VAPID pair per
environment (outside the checkout), then put the file contents into that environment's secret
settings. Do not rotate keys on every deploy: browser subscriptions are tied to the public key.

```bash
uv run python manage.py generate_vapid_keys --output-dir ~/.config/1theory/web-push-dev
```

The command writes `vapid-public.key` and `vapid-private.key` with owner-only permissions and
prints only their paths. Set `WEB_PUSH_VAPID_PUBLIC_KEY`, `WEB_PUSH_VAPID_PRIVATE_KEY` and
`WEB_PUSH_VAPID_SUBJECT` (a support contact, for example `mailto:support@1theory.co.uk`) on both
web and worker services. The private setting is the generated base64 DER content, not its path.
Run migrations and restart the web and Celery worker after deploying. Empty keys disable push;
internal notifications continue to work.

Frontend contract for UK-253:

| Request | Body / response |
|---|---|
| `GET /api/notifications/push/public-key/` (public) | `{"public_key": "<applicationServerKey>"}`; 503 while unconfigured |
| `POST /api/notifications/push/subscriptions/` (authenticated) | Browser `PushSubscription.toJSON()`: `{"endpoint": "https://…", "keys": {"p256dh": "…", "auth": "…"}}`; 200 on save |
| `DELETE /api/notifications/push/subscriptions/` (authenticated) | `{"endpoint": "https://…"}`; 204, including when already removed |
| `POST /api/auth/logout/` (authenticated) | Existing `refresh` plus `push_endpoint` for this browser, so logout removes only its subscription |

Ask for notification permission from a user action, subscribe using the public key, and save the
browser subscription after login. The API checks actual P-256/auth keys and accepts HTTPS
endpoints from Google, Mozilla, Apple and Windows browser push services. A browser subscription
can move to a different signed-in account only with its existing keys; otherwise POST answers
409 and the browser should unsubscribe and create a fresh subscription. DELETE can only remove
the signed-in user's own subscription. Account deletion cascades to its subscriptions.
Blank, null or invalid logout `push_endpoint` values are ignored; refresh-token revocation still
runs first and logout succeeds.

New internal notifications queue Web Push after the database transaction commits. The JSON
payload is `{"title": "…", "body": "…", "link": "/my-package"}` (the link defaults to `/`).
The service worker must display the notification and open the frontend route on click. Delivery
covers every saved subscription; 404/410 responses remove stale subscriptions, while temporary
failures retry only the affected subscriptions, up to three times. A stable `Topic` header
collapses retries still queued by the provider, but cannot undo an already displayed notification.
Windows endpoints also receive `X-WNS-Type: wns/raw`. A worker missing VAPID settings logs an error. Queue/provider failures do
not fail the purchase or internal notification. No browser endpoints or encryption keys are
included in task arguments or application logs.

Welcome and payment confirmation emails include the home-screen prompt. Stripe success and
cancel URLs already use `FRONTEND_BASE_URL`; set it to the installed PWA's frontend domain.
Device QA with UK-253 must confirm push display/click, current-device logout, and payment return
on installed iPhone and Android PWAs. Backend URL checks alone cannot prove that the browser
will reopen the installed PWA window.

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

Messages sent through the contact form sit in the same section, as *Contact messages*, but are
read-only and gated like the CRM — the Support Agent role, or `content.view_contactsubmission`
granted to an account — because they hold a visitor's name, address and whatever they wrote. Each
row says whether the support inbox has received it, so a message the mail server rejected is
visible rather than lost. A signed-in visitor's account is shown by id rather than by email
address: reading contact messages should not also reveal the address a customer registered under,
which is the CRM's to show. Nothing on these screens can be edited or deleted, whatever
permissions an account is given.

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

E-books carry a free sample chapter. On a section's page, **The free sample** holds a tick box and
an upload: tick the box for the one chapter the business is happy to give away, and upload a PDF of
that chapter alone. The upload is refused unless the bytes really are a PDF, the form refuses the
tick box without a file, and a second free chapter in the same book is refused by the database. The
sample is stored apart from the book's own document, which is never served in its place — see *The
public e-book landing page* above. Visitors who asked for the chapter by email are listed, read
only, under **Ebook sample requests**, with whether they agreed to study tips and when. Those rows
are addresses and marketing consent rather than course material, so writing or releasing e-books
does not come with them: they are granted by the **Marketing Support** role alone.

Reaching the CRM is granted on purpose. `apps/staff` gates the areas holding customer data on
permissions actually recorded against an account, so being a Django superuser is not by itself a
way in — a superuser must grant themselves the permission first. That is least privilege by
default, not a wall against whoever administers the system.

Roles are declared in code, in each app's `roles.py`, and created by a migration:

| Role | May |
|---|---|
| CRM Manager | Look customers up and read their records |
| Marketing Support | Read the visitors who asked for a free sample chapter |
| Support Agent | Read the contact form's messages |

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
