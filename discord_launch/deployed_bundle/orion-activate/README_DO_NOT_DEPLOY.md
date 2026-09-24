# DO NOT DEPLOY FROM THIS FOLDER

`lambda_function.py` and `lambda_deploy.zip` here are a **stale snapshot** of the
`orion-activate` Lambda (1,572 lines, last touched in commit `7d19686`).

As of 2026-09-23:
- The **live** Lambda is about 5,042 lines: repo commit `0eca7d2` plus the
  `session_only` redeem change.
- The **source of truth** is `backend/lambda_function.py`. It now also carries the rc1
  P-A security fixes: owner step-up, fail-closed kill state, required audit, Stripe
  event dedup, lease v2 and bound-machine resume. See
  `docs/redteam/2026-09-23-final/patches/P-A_backend.md`.

Deploying this folder would roll production back past every one of those fixes. That
includes the edge-auth, device-cap, lease-signing, TOTP and audit hardening, so it
would be a security regression and a release blocker.

To deploy, package `backend/lambda_function.py` (plus its dependencies), follow the
deploy notes in `P-A_backend.md`, and run StrictSecurity first. This folder is kept
for history only.
