# Owner tools were dead in production — API Gateway was missing 5 routes (FIXED 2026-09-21)

**Status: RESOLVED 2026-09-21 ~21:30 local.** Five explicit routes created on HTTP API `v348t5hg3i`
(`orion-license-api`) through the console with the owner signed in — no IAM change, no new
credentials, `venice-automation` still has no `apigateway` permission. Stage `$default` has
auto-deploy on, so each route was live seconds after its integration attached.

| route | id | attached to |
|---|---|---|
| `GET  /api/admin/metrics` | `zy6jx2q` | `orion-activate` (integration `gpamg89`, payload 2.0) |
| `GET  /api/admin/config`  | `akysde7` | same |
| `POST /api/admin/config`  | `ba37po8` | same |
| `GET  /api/staff/audit`   | `2uqu0uo` | same |
| `GET  /api/admin/audit`   | `twds0zl` | same |

No authorizer on any of them, like every sibling: the Lambda enforces role capability itself and
writes an audit row per mutation. No `$default` catch-all was added.

## Verification (unauthenticated, through `https://api.zaeorion.com`)

Every (method, path) `AdminToolController.cpp` and `tools/admin/orion_admin.py` call. 404 = the
gateway dropped it; 401/403/400 = the Lambda answered.

```
GET   /api/admin/whoami         403   GET   /api/admin/audit          403
GET   /api/staff/whoami         401   GET   /api/staff/audit          401
POST  /api/staff/enroll         400   GET   /api/admin/config         403
POST  /api/staff/login          400   POST  /api/admin/config         403
GET   /api/admin/metrics        403   POST  /api/admin/tamper-report  403
GET   /api/admin/license        403   POST  /api/staff/tamper-report  401
POST  /api/admin/license        403   GET   /api/admin/staff          403
POST  /api/staff/license        401   POST  /api/admin/staff          403
                                      unrouted: 0
```

Owner-side check still owed (needs the admin secret, which Claude does not handle): launch
`OrionOwner.exe`, sign in, confirm Dashboard metrics load, Audit lists rows with actors, Config reads
MOTD / version gate / kill state, and a kill engage → disengage round-trips through Config.

## What was actually wrong (two of my earlier claims were off)

The first survey probed every path with POST and reported **11 missing routes and a one-way kill
switch**. Both were wrong in the details:

- `/api/admin/whoami`, `/api/admin/search`, `/api/staff/whoami` already existed as **GET**, which is
  the method the tools use. Probe with the tool's method, not a default.
- The console's flattened route tree hides nesting: what read as `/api/admin/audit GET` was
  `/api/admin/staff/audit`. The owner audit route (`GET /api/admin/audit`) really was missing — a
  GET probe (404) settled it, the tree text could not.
- **The tools never call `/api/admin/kill` or `/unkill`.** Both drive the kill switch through
  `config.global_kill` on `/api/admin/config`, which was missing in both directions — a dead
  switch, not a one-way one. Those two Lambda routes are legacy; no tool needs them routed.
- Only what a tool calls was created. `/api/admin/{status,unkill,provision,staff/audit}` exist in
  the Lambda but nothing calls them; an unused route is surface for nothing.

## Console traps (so the next route takes 2 minutes, not 40)

- Clicking a tree link by accessibility ref only scrolls it into view; the click that selects is the
  second one, on the now-visible node. With duplicate names (`/audit` ×3) filter the tree first —
  the search box narrows to the nodes and the target is the one with the lone method.
- The success banner shifts the page ~46 px; coordinates measured with it present miss without it.
- "Unable to load content" on Integrations pages is intermittent; the route-details → Attach
  integration path always loaded. The integration list never appears in the accessibility tree —
  open the dropdown and click the option (`orion-activate (gpamg89)` is first).
- The Create form pre-fills `/`; select-all before typing the path or you get `//api/...`.

## Fixed while measuring (local, not deployed)

- `backend/lambda_function.py`: subscription DM said "$20/month"; now `$19.99/month`.
  `tests/backend` 360 passed, `tests/test_backend_staff_auth.py` 17 passed.
- `tools/admin/orion_admin.py`: "3-day trial"/"$25" comments → 7-day/$19.99. 76 passed.
- `tools/admin/create_gateway_routes.py`: idempotent, method-aware creator/verifier for the five
  routes (dry run by default; needs `apigateway:GET/POST` on this API — policy in
  `venice-automation-apigateway-policy.json`; not attached, and not needed now).
