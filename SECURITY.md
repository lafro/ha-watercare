# Security policy

## Supported versions

Only the latest release is supported with security fixes.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/lafro/ha-watercare/security/advisories/new). Do not open a public issue that contains a Watercare email address, password, token, account or meter number, address, balance, usage figures, a bill, or logs or diagnostics you have not reviewed.

If you think your Watercare password has been exposed, change it in the Watercare app or My Account, then complete the re-authentication prompt in Home Assistant.

## Credential model

- The integration asks for the Watercare email and password, because Watercare's sign-in (Azure AD B2C) offers no other credential flow for its customer app. Home Assistant stores them in the config entry.
- After sign-in, a refresh token is stored in the config entry so restarts renew the session instead of signing in again. Short-lived access tokens stay in memory.
- Credentials are sent only to Watercare's sign-in service. Each sign-in runs in a short-lived session created by Home Assistant's own helper (Home Assistant's SSL context and connector) with its own cookie jar, detached afterwards; other requests use Home Assistant's shared session.
- Logs never include credentials, tokens, account or meter numbers, request URLs that contain them, or response bodies.
- Diagnostics redact the email, password and token, and contain no account or meter numbers, balances, usage or costs.

The OAuth client id in the code is the public identifier of Watercare's mobile app, not a secret.

## Supply chain

- Every GitHub Action is pinned to a full commit SHA, and every checkout drops the job token (`persist-credentials: false`).
- Known residual: two validators run container images that the SHA pin does not freeze. `hacs/action` runs `ghcr.io/hacs/action:main`, and the Hassfest action runs `ghcr.io/home-assistant/hassfest` without a tag, so either image can change under the same pin. They run in their own jobs with read-only contents permission, and the Release workflow's only job with write permission uses nothing from them but their pass or fail. Keep them in separate, read-only jobs.
- The test suite rejects anything shaped like a Watercare account number or meter id, and any email address outside `example.com`, `example.org` and `example.invalid`. Contributors run `gitleaks` before pushing (see CONTRIBUTING.md).
