# Dashboard live data contract — Agents 2, 3 and 5

The Dashboard is a read-only consumer of existing VPS_1 Cloud Render / Production / Media and Concat Uploader stores. Do not introduce a dashboard database or global counters. The reused scene and uploader code remains the canonical queue implementation; Dashboard does not duplicate queue transitions.

## Registration

After the respective feature services have real owner-filtered repositories, register two providers on FastAPI app state:

- Agent 2: app.state.concat_dashboard_provider
- Agent 3: app.state.uploader_dashboard_provider

Each provider exposes dashboard_snapshot(*, owner_username: str, limit: int) -> mapping. Sync and async supported. Agent 5's verified auth resolver supplies Principal.username; never derive the owner from URL, headers, body, or admin role. No demo/test adapters are mounted by default. If either source is absent, the overview API returns 503 rather than fabricated zero. Cross-owner/invalid data returns 502.

Agent 5: install verified identity_resolver; register sources only after owner-schema and access-control gates. Preserve no-store responses, session revocation, activation, access grants. Admin personal jobs remain owner-scoped.

## Agent 2 snapshot (Cloud Render)

Root object:
- owner_username: authenticated owner.
- totals: object of non-negative integer total, completed, queued, active, failed. Compute from owner-filtered production_jobs COUNT queries, never the first page.
- queue: object state (running, stopped, paused, idle, disabled, unknown), and owner-specific queued/active integer counts. Original ProductionQueue.snapshot is an upstream source; its global counters cannot be forwarded as per-owner data.
- jobs: at most limit records with id, owner_username, status, name/title, updated_at (RFC3339 timezone-aware ISO string or null). Accepted states include queued, downloading, uploading to gpu, rendering, downloading output, completed, failed, cancelled.
- videos: object total (count of verified ready owner media) and items (at most limit records with id, owner_username, name/title, state=ready, sha256 of 64 hex digits, updated_at). Use existing Media/Copy Video Link verified identity, not legacy ownerless files.
- gpu: object available (true/false/null), status string, checked_at RFC3339 or null. Available=true requires a recent authenticated health check of the worker configured for this owner. Merely enabled or configured is NOT online; if unprobed use null and unknown.

Owner field required on every record and snapshot. Exclude unassigned legacy data until explicit, audited migration. Owner predicate belongs in the data query itself. Never send renderer tokens, endpoints, Drive paths, credentials, or URLs to Dashboard.

## Agent 3 snapshot (Uploader)

Root object:
- owner_username.
- totals: owner-scoped integers total, completed, queued, active, failed.
- queue: object state and owner-scoped queued/active counts, reusing persistent uploader queue logic and uploader tenant/ownership store.
- jobs: at most limit records with id, owner_username, status, name/title, updated_at. States accepted: pending, queued, uploading, scheduled, processing, completed, failed, cancelled, waiting network.

Video links are identifiers, never proof of ownership. No OAuth, tokens, foreign channels, source paths, or sensitive metadata in responses.

## Read-only API

- GET /dashboard: verified-session responsive page; no synthetic numeric counters.
- GET /api/v1/dashboard/overview?limit=8: protected JSON, 1 <= limit <= 25, no owner parameter, no-store.
- GET /dashboard/assets/dashboard.css and /dashboard/assets/dashboard.js: public static UI (no user data).

Both providers receive identical server-derived owner. Validation rejects mismatched owner fields, unverified media, invalid counts, overly large pages and unknown states. No fallback to copied global Cloud Render tables.

## Verification

tests/test_reused_dashboard.py is included in the existing foundation CI test_reused_*.py pattern:

python -m unittest discover -s tests -p 'test_reused_dashboard.py' -v

Agents 2 and 3 must add integration tests against real owner-aware persistence to verify Alice, Bob and admin can only see their own counts and rows, including queue, verified media and GPU. Until providers are registered, the UI remains explicitly unavailable rather than presenting mock data.
