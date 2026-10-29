# UK-254 mobile backend implementation

Base: `develop` at `d32c17c`; implementation branch: `UK-254`.

## Work plan and ownership

1. Push agent: UK-265 device API and UK-266 Firebase deliveries, using existing payment,
   access-ending and streak events. Persist deliveries for retries and remove invalid tokens.
2. Apple agent: UK-268 verified Apple sign-in, registration, explicit account linking and
   one-use nonce challenges. Verify signatures and claims; never link by email alone.
3. Account deletion agent: UK-267 authenticated deletion, fresh identity confirmation,
   personal-data cleanup and retained payment-record anonymisation. Prevent delayed payment
   processing from recreating a deleted account.
4. Account deletion agent assesses UK-269 mobile auth/origin requirements; integration owner
   verifies the bearer-token flow, exact-origin CORS and frontend handoff.
5. Push agent records UK-270 trial decisions; Apple agent records UK-271 payment decisions.
   These already exist as separate Jira subtasks and remain pending business confirmation.
6. Integration owner wires routes/settings and runs validation. A separate review agent checks
   the completed changes; actionable findings are repaired and checked again.

## Frontend handoff / UK-269

The API already uses `Authorization: Bearer <access>` and returns refresh tokens in JSON.
Use the existing login/refresh endpoints, keep credentials in app secure storage, and send the
bearer header for protected calls. Session cookies and CSRF tokens are unnecessary for this
API flow. Admin session security remains separate.

The frontend owner must confirm the **actual release build Origin headers** on both platforms
and whether networking uses the WebView or a native HTTP plugin. Configure those exact origins
in `CORS_ALLOWED_ORIGINS` for each environment; for example, `capacitor://localhost` on an iOS
WebView, or `https://localhost` on Android, only when the release build actually uses them.
Existing configuration accepts these schemes and includes the Authorization header and DELETE
method. Do not enable wildcard origins, `null` origins, credential cookies or CSRF exemptions.
No speculative origin has been enabled by this change. Frontend confirmation is still pending.

Automated HTTP tests exercise preflight, protected calls, refresh and logout with real JWTs
and CSRF checks enabled. Real iPhone/Android builds still need the UK-259 manual checks.

## Configuration / release dependencies

Supply Firebase project credentials via secret management / Application Default Credentials,
configure `FIREBASE_PROJECT_ID`, and enable `FIREBASE_PUSH_ENABLED` only when ready.
Run the worker and the existing single beat scheduler for delivery recovery.

Apple requires company Developer Program setup (UK-255), configured `APPLE_CLIENT_IDS` and
provider credentials for revocation. Never commit private signing keys or service-account JSON.

Account deletion defaults off until the privacy/retention policy and external data handling
are approved. See the deletion documentation for retained records and remaining obligations.

UK-270: business chooses duration versus limited-content trial, eligibility/reuse limits and
conversion rules before entitlement logic is implemented.
UK-271: business chooses the iPhone purchase model before store validation is implemented.
No receipt endpoint grants access based on an unverified client assertion.

Source scope: [UK-254](https://orangeuni.atlassian.net/browse/UK-254) and
[UK-231 frontend estimate](https://orangeuni.atlassian.net/browse/UK-231?focusedCommentId=15172).

## Validation and review (29 October 2026)

- Final affected suites: **631 passed** across accounts, billing, notifications and mobile HTTP.
- Initial full suite: 2065 passed; two new test-fixture errors were repaired and covered by
  the successful final run above. Unaffected suites were not repeated.
- Ruff lint/format, mypy, Django system checks, migration consistency, OpenAPI validation
  and deployment-waiter regression checks passed.
- Independent review found and repaired post-deletion email publication, unbounded device
  registration, pre-commit token publication and welcome-email failure recovery. Final review
  reported no remaining blocking findings.
- Regression tests include a separate worker database connection for token visibility,
  simultaneous device registration at the cap, deletion between payment activation and email,
  and retry after both queued and fallback welcome delivery fail.
- No deployment or live Firebase/Apple/device tests were performed. Changes are local;
  UK-189's pre-existing settings edits remain preserved in the named Git stash.
