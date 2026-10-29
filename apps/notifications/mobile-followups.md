# UK-254 mobile push and business follow-ups

## Push API

- `POST /api/notifications/devices/`: Bearer access token; body `{"token": "FCM registration token", "platform": "ios"}` (`android` also supported). Returns 204. Re-register on FCM token rotation and after login. A token belongs to the account most recently registering it.
- `DELETE /api/notifications/devices/`: Bearer access token; body `{"token": "FCM registration token"}`. Returns 204, including an already removed/foreign token. The app must remove the old token when FCM rotates it.
- Device endpoints share the authenticated-action rate limit. An account can save at most 20 installations; refreshing an existing token still works at the limit. Remove an old installation before adding a replacement when full.
- Logout must send the current `device_token` so only that installation stops receiving notifications; removing all installations would sign other devices out of push.

Enable `FIREBASE_PUSH_ENABLED` and set `FIREBASE_PROJECT_ID` only after Firebase is ready (UK-255). Supply Google Application Default Credentials, e.g. a securely mounted service account through `GOOGLE_APPLICATION_CREDENTIALS`. Never commit the service account file. The account needs FCM send permission for the target project. Configure APNs credentials in Firebase for iOS.

Uses existing events: package activation following payment, access ending soon, access expired, and study streak milestones (3, 7, 14, 30, 60, 100 days). Existing event timing remains unchanged and needs product agreement. No new marketing notifications are introduced. UK-265/UK-266 track the push work.

Worker and beat are required. A durable per-device outbox recovers queue failures and retries provider failures up to five attempts with exponential backoff and Retry-After support. Successful devices are not retried when another device fails. Exhausted deliveries remain inspectable with a safe error code. Firebase acceptance does not prove device display; a worker crash after acceptance can redeliver, so deduplicate `notification_id` in the app. Invalid `UNREGISTERED` tokens are removed; generic validation/configuration errors do not remove devices.

Before release, test real Firebase deliveries on iPhone and Android, permission denial, logout, token rotation, multiple devices and switching accounts. No live credentials or device delivery were exercised by automated tests.

## Trial period (UK-270)

Deferred until the business confirms duration, eligibility, what access is included, whether payment details are required, and whether/when a trial renews. Then implement against agreed acceptance criteria in UK-270. UK-254 does not grant free access or start automatic billing.
