# Security policy

## Supported versions

Only the latest release is supported with security fixes.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/lafro/ha-watercare/security/advisories/new). Do not open a public issue that contains a Watercare email address, password, token, account or meter number, address, balance, usage figures, a bill, or logs or diagnostics you have not reviewed.

If you think your Watercare password has been exposed, change it in the Watercare app or My Account, then complete the re-authentication prompt in Home Assistant.

## Credential model

- The integration asks for the Watercare email and password, because Watercare's sign-in (Azure AD B2C) offers no other credential flow for its customer app. Home Assistant stores them in the config entry.
- After sign-in, a refresh token is stored in the config entry so restarts renew the session instead of signing in again. Short-lived access tokens stay in memory.
- Credentials are sent only to Watercare's sign-in service. The sign-in runs in a private session with its own cookie jar; other requests use Home Assistant's shared session.
- Logs never include credentials, tokens, account or meter numbers, request URLs that contain them, or response bodies.
- Diagnostics redact the email, password and token, and contain no account or meter numbers, balances, usage or costs.

The OAuth client id in the code is the public identifier of Watercare's mobile app, not a secret.
