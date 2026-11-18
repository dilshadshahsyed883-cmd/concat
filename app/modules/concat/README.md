# Concat Studio — independent, owner-scoped Cloud Render

Copied VPS_1 Cloud Render engine source is in `engine/cloud_render.py` and
`engine/cloud_render_production.py`, from `reused/scene/part01` at the
foundation baseline. The original source and reference tests are retained.

## Runtime

The existing Concat Web `app/main.py` includes this router. The browser uses
`/concat/production`, `/concat/media` and `/concat/gpu-config`. The original
`/api/cloud-render/**` endpoints, receiver contract and Copy Video Link route
are preserved. GET aliases are also available under `/api/v1/concat/**`.

`current_principal` must be supplied by Agent 5's **verified and active**
Scene session resolver. No identity headers or query/body owner values are trusted.
UI mutations require a strict same-site CSRF cookie and matching custom header.
The worker callback instead validates the owner's rotating receiver capability.

Configure `CONCAT_WEB_DATA_ROOT=/var/lib/concat-web` with a dedicated service
user and permissions. Each verified username hashes to a private owner directory
and SQLite instance; jobs, files, GPU configuration, receiver tokens and queue
state cannot mix. Scheduler state recovers on lifespan startup. No direct
cross-repository imports, credential copy, global admin bypass, or test login.

The original Drive Bridge uses the existing Scene service rclone identity
and checksum/MD5/remote-stat confirmation; owner uploads are placed under
`CLOUD_RENDER_DRIVE_FOLDER/concat-web/<owner-hash>/` without altering the
canonical remote credentials. Rendering still uses the existing Salad API and
ZIP validation, queue persistence and verified MP4 transfer.

**Legacy data stays untouched** in the original VPS_1 application. Ownerless
legacy `files` and `production_jobs` cannot safely be attributed and must be
explicitly owner-verified before any optional migration. Do not point
`CONCAT_WEB_DATA_ROOT` directly at VPS_1's historical cloud-render directory.

Run offline focused tests: `python -m unittest app.modules.concat.test_routes -v`.
Do not deploy until Agent 5's production verified SSO and negative cross-owner
integration tests are green. No direct VPS_1 production changes are necessary.
