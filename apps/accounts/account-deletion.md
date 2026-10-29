# Mobile account deletion (UK-254)

`DELETE /api/cabinet/account/` requires a bearer access token and a JSON body containing
`confirm: true`. Password accounts must provide their current `password`. Google-only
accounts must provide `google_id_token` from the linked Google account. Every Apple-linked
account must provide a fresh `apple_authorization_code` and its `apple_client_id`, including
accounts that also have a password: Apple grant revocation must succeed before data deletion.
Staff accounts use the existing administrator process instead of this endpoint.

The endpoint returns 204 after local deletion. Existing access and refresh tokens no longer
work because their user no longer exists. Profile, credentials, learning progress, saved
questions, app notifications, device tokens and security links are deleted by cascade.
The profile photo is removed from configured storage. If storage deletion fails, database
deletion is rolled back and no successful response is returned. Audit snapshots describing
this account and Django administration log entries for it are scrubbed/deleted.

Orders keep amounts, package, timestamps and Stripe payment/checkout references for payment
reconciliation, refunds and accounting. Their customer email and names are replaced by an
order-specific reserved alias; no user link remains. Subscription rows are unlinked rather
than deleted so the payment recovery worker cannot recreate an account for an old purchase.
Late payment fulfilment must refuse the `@deleted-account.invalid` reserved aliases.
A deletion does not issue a refund. Current purchases are fixed periods of access, not a
Stripe recurring subscription; a future store subscription cancellation policy is separate.

## Release gate: policy and external data

`ACCOUNT_DELETION_ENABLED` defaults to false. Enable it only after the privacy owner confirms
this retention scheme and the published privacy policy. The repository does not contain an
approved privacy policy or an accounting retention period, so this change does not invent one.
The privacy owner must decide the retention and eventual purge of retained transaction IDs,
backup copies, infrastructure/application logs and audit timestamps. External Stripe records,
email provider history, already delivered emails and queued generic email jobs are not erased
by this database operation. Provider erasure and retention procedures must cover them. Generic
email jobs already queued with an email address/context cannot reliably be withdrawn by this
endpoint; token-based security emails stop because their token row has been deleted.

Apple provider keys and project configuration are needed to revoke authorization. Test deletion
on an actual Apple account before release. Firebase device deletion is local and cascades from
the user; it does not attempt to erase the device's Firebase installation.

## Mobile request settings

The API already authenticates with `Authorization: Bearer <access>` and returns JWTs as JSON;
it does not depend on session cookies. Native HTTP clients do not enforce browser CORS. A
WebView or Capacitor app must confirm its exact Origin with FE; then configure that exact
origin in `CORS_ALLOWED_ORIGINS` for the environment and test preflight requests. Do not enable
wildcard origins, credentials or disable secure cookie/CSRF defaults to make mobile login work.
Physical iPhone/Android verification still requires the app, app origins and deployed server.
