# Apple mobile integration (UK-254 / UK-268)

## App contract

1. `POST /api/auth/apple/nonce/` returns a raw `nonce` and `expires_in` (300 seconds).
2. Pass the SHA256 hex digest of that raw nonce as the Apple authorization request's nonce.
3. Send Apple's `id_token` and the original raw `nonce` to `POST /api/auth/apple/`.
   `first_name` and `last_name` are optional profile fields, provided only once by Apple.
4. Store the returned API access/refresh tokens as for other sign-in methods.
5. If the response is 409 with `action: link_apple`, first sign in to the existing account,
   then submit a fresh challenge and Apple token to `POST /api/auth/apple/link/` using API Bearer auth.
   Email matches never automatically link accounts.

Apple subjects are permanent account identifiers. A returning subject can sign in without
an email claim; creating a new account requires Apple's verified email (including private relay).
Managed Apple accounts without email cannot create an email-based platform account.

Configure `APPLE_CLIENT_IDS` with the iOS bundle ID and any approved Services IDs.
Keep the list empty until UK-255 provides the real identifiers; no wildcard audience is accepted.
For Android or browser-based Apple authorization, FE must configure the registered HTTPS return
URL in Apple and safely return the token and original nonce to the app. This backend expects the
nonce in the identity token to contain the digest, including on those platforms.

Account deletion for an Apple-linked account requires a fresh `apple_authorization_code` and
the `apple_client_id` used to authorize it. Include `apple_redirect_uri` if the authorization
request included a redirect URI. Provider revocation uses the server's `APPLE_TEAM_ID`,
`APPLE_KEY_ID` and `APPLE_PRIVATE_KEY` (ES256 private key held in deployment secrets).
The server verifies that the code belongs to the same Apple subject before revoking its grant;
it does not delete the account if revocation fails. No Apple refresh token is retained.
FE must clear its local credentials and personal data after successful deletion.

## IAP decision (UK-271)

Do not grant entitlements from client-supplied receipt strings or introduce a second payment
path before the business decision. Confirm whether Apple IAP is required or postponed and
whether Google Play billing is in scope. If IAP proceeds, agree product IDs, subscription versus
one-off purchase rules, mapping to platform packages, restore purchases, refunds and renewal
events, sandbox/production credentials, ownership binding, and App Store Server Notifications.
Implement and verify these in UK-271 after those decisions. UK-254 adds no IAP or trial entitlements.

## References

- [Apple: authenticating users](https://developer.apple.com/documentation/signinwithapple/authenticating-users-with-sign-in-with-apple)
- [Apple: token validation](https://developer.apple.com/documentation/signinwithapplerestapi/generate-and-validate-tokens)
- [Apple: token revocation](https://developer.apple.com/documentation/signinwithapplerestapi/revoke-tokens)
- [PyJWT: JWKS validation](https://pyjwt.readthedocs.io/en/stable/usage.html#retrieve-rsa-signing-keys-from-a-jwks-endpoint)
