# UK-153 manual test plan

Exercise the API on local branch `UK-153`. Frontend checks are called out where the storefront must change; details are in `docs/UK-153-frontend-security-changes.md`.

Base URL is `http://localhost:8000`. Send JSON as `Content-Type: application/json`. A signed-in call sends `Authorization: Bearer <access>`.

## Prerequisites

From the repo root, with [uv](https://docs.astral.sh/uv/) and Docker:

```bash
docker compose up
docker compose run --rm web python manage.py migrate
```

That starts Postgres on port 5432, Redis on 6379, the API on port 8000, a Celery worker, and Celery beat. Emails are printed to the logs (`EMAIL_BACKEND` defaults to the console backend), not to a mail catcher:

```bash
docker compose logs -f web worker
```

`CELERY_TASK_ALWAYS_EAGER` defaults to false in compose, so queued work runs in the `worker` service. There is no Celery result backend. Job arguments show up in the worker log line `Task … received`, and on the broker Redis database `redis://redis:6379/1`. The email body is printed afterwards and is supposed to contain the link; the `received` line is what must not.

Payments need `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. Forward Stripe to the app with:

```bash
stripe listen --forward-to localhost:8000/api/payments/webhook/stripe/
```

Put the signing secret that command prints into `STRIPE_WEBHOOK_SECRET` and restart `web`. Leave `S3_BUCKET_NAME` empty so uploads stay on local disk and Django serves them.

Create an active package in `/api/admin/` if `GET /api/packages/` is empty. Note its `id` and price.

Shell commands below assume `docker compose exec web python manage.py shell`.

## 7. Order status does not return the buyer email

No auth.

```bash
curl -s -X POST http://localhost:8000/api/payments/checkout/ \
  -H 'Content-Type: application/json' \
  -d '{"package_id": PACKAGE_ID, "email": "buyer@example.com", "first_name": "Buyer"}'
```

`201` body: `session_url`, `order_id`. The order id is the capability URL.

```bash
curl -s http://localhost:8000/api/payments/orders/ORDER_ID/
```

`200`. Body has `id`, `status` (`pending`), `package_name`, `package_duration_days`, `original_price`, `discount_amount`, `final_price`, `paid_at` (null), `created_at`. It has no `email`, `first_name`, or `last_name`, and the address `buyer@example.com` appears nowhere in the body.

Frontend: the order-status page must not render that address. See the frontend doc, issue 7.

## 8. A payment must match the stored order

Currency is `gbp`. Amount is `final_price` in pence (`£29.99` is `2999`). The Checkout Session id on the event must equal `Order.stripe_session_id`. There is no management command; the event shape is `completed_event` in `apps/billing/tests/test_webhook.py`. The endpoint is `POST /api/payments/webhook/stripe/`. An unsigned body returns `400` `{"detail": "Invalid signature."}` and does not change the order.

**Match.** Pay the checkout from issue 7 with a Stripe test card, with `stripe listen` forwarding. Stripe sends `checkout.session.completed` with `payment_status` `paid`. The HTTP response is `200` `{"detail": "ok"}`.

`GET /api/payments/orders/ORDER_ID/` then shows `status` `paid` and a non-null `paid_at`. In the admin, the order has a subscription. The web log does not contain `Refusing to mark order`.

**Mismatch.** On a new pending order, send a signed `checkout.session.completed` (a new Stripe event id each time) whose session has this order's id in `metadata.order_id` and `client_reference_id`, `payment_status` `paid`, and one of:

- `amount_total` one penny off `final_price` in pence, or
- `currency` `usd`, or
- `id` different from the order's `stripe_session_id`.

The HTTP response is still `200` `{"detail": "ok"}` (Stripe must not retry a permanent mismatch). The order stays `pending`, `paid_at` stays null, and no subscription is created. The web log contains `Refusing to mark order <id> paid: payment does not match the stored order`.

Sending the same Stripe event id again logs `Ignoring Stripe event … already handled` and still does not pay the order.

## 9. Enumeration replies and the Google-only lockout

`POST /api/auth/login/` body: `{"email": "…", "password": "…"}`. No auth. Same IP for the lockout steps. Sign-in and register share `10/minute` per IP (`auth_anon`); password reset is `5/hour` per IP. Stay under those or the next call is a throttle `429`, which is a different check.

**Unknown and wrong password.** Against no such user, and against a real password account with the wrong password:

`401` `{"detail": "Invalid credentials."}` for both. Do not use a disabled account: a correct password on a disabled account is `403` `{"detail": "This account is disabled."}`.

**Register 409, unchanged.** `POST /api/auth/register/` with `email`, `first_name`, `password`, `confirm_password` (8+ characters, one uppercase letter, one digit).

- New address: `201`.
- That address again: `409` `{"detail": "An account with this email already exists."}`.
- A Google-only address (created in the shell as below): `409` `{"detail": "This email is registered via Google. Please sign in with Google."}`.

**Password reset, unchanged.** `POST /api/auth/password/reset/` `{"email": "…"}`.

- Unknown address, and a password account: `200` `{"detail": "If this email is registered, a reset link has been sent."}` and no `google_account` key. The password account also gets the reset email in the worker log.
- Google-only address: `200` with that same `detail` plus `"google_account": true`, and the detail text `Your account is linked to Google. Manage your password in Google settings.`

**Google-only password login now locks.** In the shell:

```python
from apps.accounts.models import AuthMethod, User

User.objects.create_user(
    email="google-only@example.com",
    first_name="Georgie",
    auth_method=AuthMethod.GOOGLE,
    google_id="manual-uk-153",
    email_verified=True,
)
```

`POST /api/auth/login/` for that address with any password. The first 5 calls (`MAX_FAILED_LOGINS`) are `400` `{"detail": "This account uses Google Sign-In. Please sign in with Google."}`. The 6th is `429` `{"detail": "Too many attempts. Please try again in 15 minutes."}`. The window is 15 minutes from the latest failure. A real Google sign-in is not required for this check.

## 10. Payment does not mark the email verified

Use a buyer address that has no account yet. Complete the matching payment from issue 8.

Worker logs show three messages for that address: the welcome / set-password mail (`Welcome to 1Theory — set up your account`), the receipt (`Your order has been confirmed`), and `Please confirm your email address`. The confirmation mail's link is `{FRONTEND_BASE_URL}auth/verify-email?token=…`.

Open the setup link (`auth/reset-password?token=…`) and set a password with `POST /api/auth/password/reset/confirm/` (`token`, `password`, `confirm_password`). Sign in with `POST /api/auth/login/`.

`GET /api/cabinet/profile/` returns `"email_verified": false`.

`GET /api/cabinet/subscription/` returns `"status": "Active"` and `"online_platform_activated": true` while `email_verified` is still false. Access does not wait on the flag.

`GET /api/auth/email/verify/?token=THE_CONFIRMATION_TOKEN` then returns `200`, and the profile field becomes `true`. Do not call that until after the check above.

Frontend: do not show the buyer as verified because payment succeeded. See the frontend doc, issue 10.

## 11. Unfinished orders are deleted after 15 days

`UNPAID_ORDER_RETENTION` is `CHECKOUT_SESSION_LIFETIME` (24 hours) plus `DELAYED_PAYMENT_WINDOW` (14 days): 15 days from `created_at`. Beat runs `apps.billing.tasks.delete_unfinished_orders` as `delete-unfinished-orders` daily at 01:00. Call it yourself; do not wait for 01:00.

Create three orders (checkout for the pending and the paid one; mark one failed in the shell with `status="failed"`). Age with `QuerySet.update`, not `save()`, so `created_at` actually moves:

```python
from datetime import timedelta
from django.utils import timezone
from apps.billing.checkout import UNPAID_ORDER_RETENTION
from apps.billing.models import Order
from apps.billing.tasks import delete_unfinished_orders

stale = timezone.now() - UNPAID_ORDER_RETENTION - timedelta(minutes=1)
Order.objects.filter(pk="PENDING_OR_FAILED_UUID").update(created_at=stale)
print(delete_unfinished_orders())
```

The pending order and the failed order older than 15 days are gone. Return value counts only those deletes.

Leave one pending order newer than 15 days, and the paid order (age it the same way if you want). Run the task again. Both remain. A pending order that already has a subscription also remains.

## 12. Uploads are typed from their bytes

Leave `S3_BUCKET_NAME` empty. Grant your staff user the **Content Editor** role (`uv run python manage.py sync_roles` from a machine with `DATABASE_URL`, or the equivalent `docker compose run --rm web python manage.py sync_roles`) and add the user to that group. Learning admin ignores bare superuser status.

In `/api/admin/learning/mediaasset/add/`:

| Upload | Result |
|---|---|
| A real PNG, even if the browser type is `text/html`, kind Image | Saved. Readonly `content_type` is `image/png`. |
| HTML bytes named `notes.pdf`, kind Document, declared type `application/pdf` | Form redisplays. No row. Error says the file is not a format the site can store. |
| A real PDF, kind Document | Saved. `content_type` is `application/pdf`. |
| A real MP4, kind Video | Saved. `content_type` is `video/mp4`. |
| A `.vtt` file whose first line is `WEBVTT`, on a video's subtitle row | Saved as `text/vtt`. |

In `/api/admin/landing/landingmedia/add/`, an SVG whose source contains `<script>` (or `javascript:`, `onload=`, `onerror=`) is rejected. An SVG without those is stored as `image/svg+xml`.

Django serves local files at `/api/media/…` while `DEBUG` is true and no bucket is set. `curl -I` that URL:

- PNG, JPEG, GIF, WebP, MP4, WebM, QuickTime, WebVTT: `Content-Disposition` starts with `inline`, `X-Content-Type-Options: nosniff`, and `Content-Type` is the sniffed type.
- PDF and SVG: `Content-Disposition` starts with `attachment`, same `nosniff`, `Content-Type` `application/pdf` or `image/svg+xml`.

Staff image preview `GET /api/learning/media/<id>/` (admin session, image assets only) sends the same three headers with `inline`. A signed-in API user without `learning.view_mediaasset` gets `403`.

With a bucket configured, Django does not serve the file. `Content-Disposition` is stored on the object; `nosniff` has to be set on the bucket or CDN. See the frontend doc, issue 12. Do not iframe the ebook PDF URL; pictures, video, and `<track>` stay inline.

## 13. Reset and verification codes are not job arguments

`POST /api/auth/password/reset/` for a password account, and `POST /api/auth/register/` for a new address (or `POST /api/auth/email/verify/resend/` while signed in and unverified).

In `docker compose logs worker`, the `received` line for `apps.accounts.tasks.send_security_link` has only `token_id` (an integer) and `template` (`password_reset` or `email_verification`). It does not contain `token=` or the code. The console email printed after that line still contains the link, and `GET /api/auth/email/verify/?token=…` or the reset confirm endpoint still accepts it.

There is no result backend to inspect. If you watch Redis database 1, the task message has the same two arguments.

Refusal, from the shell (this is not an HTTP endpoint):

```python
from apps.core import email

email.send(
    template="password_changed",
    subject="Hello",
    to="sam@example.com",
    context={"verification_url": "https://example.test/verify?token=abc"},
)
```

Raises `ValueError: Refusing to queue an email that contains a security code.` No `send_email` task is queued.

## 14. A bad answer is a 400, and access checks stay

`POST /api/learn/answer/`.

**No plan.** Register a user, sign in, do not buy. Valid body and a nonsense body both return `402` `{"detail": "An active plan is needed for this"}`. The plan check runs before validation.

**With a plan.** Use the buyer from issue 10. `GET /api/learn/practice/` and take a question `id` and an option id such as `"a"`.

- `{"questionId": QUESTION_ID, "selected": ["a"]}` → `200` with `correct`, `correctIds`, `explanation`, `lesson`.
- `{"questionId": "nope", "selected": ["a"]}` → `400`. Body includes `questionId` (a valid integer is required). Not `500`.
- `{"questionId": QUESTION_ID, "selected": "a"}` → `400` with a `selected` field error. Not `{"detail": "selected must be a list of option ids."}`.
- `{"questionId": QUESTION_ID, "selected": [{"id": "a"}]}` → `400` with a `selected` error.
- `{"questionId": 99999999, "selected": ["a"]}` → `404` `{"detail": "Not found."}`.

Frontend: read the field errors, not the old `detail` string. See the frontend doc, issue 14.
