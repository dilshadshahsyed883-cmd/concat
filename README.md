# Concat Web

Independent FastAPI application for the existing VPS_1 host. The original Scene website is unchanged. Integrated PR #3 (Uploader), #4 (Concat Studio), #5 (Dashboard), and #6 (Admin/SSO) are copied from their verified source trees.

## Routes and source boundaries

The sidebar has exactly three top-level sections: Dashboard, Concat Studio, and Admin. Concat Studio contains Production, Media, GPU Config, and Copy Video Link. Concat Uploader exposes Sheets Sync, Channels, Upload Engine and Queue by calling the copied uploader in process after verified authentication.

Dashboard uses read-only owner-scoped queries against the real Cloud Render and uploader SQLite stores, not mock counters. SSO consumer checks active Scene users, revocable sessions and Concat grants on every protected request; without the upstream Scene issuer it fails closed.

Concat stores each user's videos and jobs in a private per-owner SQLite directory under $CONCAT_WEB_DATA_ROOT/owners/sha256(canonical_username). The copied uploader's verified media admission checks the same owner-specific directory and copies a SHA-verified ready MP4 to private staging. The legacy global single-database source is supported only when its owner_username field matches the authenticated owner; ownerless data is never migrated.

## Offline verification and wheel

    python -m pip install -e '.[test]'
    python -m compileall -q app reused run.py
    python -m unittest discover -s tests -p 'test_foundation.py' -v
    python -m unittest discover -s tests -p 'test_reused_*.py' -v
    python -m unittest app.modules.concat.test_routes -v
    python -m unittest discover -s app/modules/uploader/tests -p 'test_*.py' -v
    python -m unittest discover -s tests -p 'test_admin_security.py' -v
    python -m pip wheel --no-deps --wheel-dir /tmp/concat-web-wheel .

Fixtures verify a signed-in receiver's simulated output, verified MP4, copied persistent queue, dashboard, owner isolation, CSRF, and legacy quarantine. Tests do not publish live YouTube videos or invoke Salad GPU.

## Release status

No merge or deployment authorized. Upstream Scene SSO issuer/session table is pending in VPS_1 issue #772. Real OAuth/YouTube/Salad and full multi-user deployed verification require controlled non-production staging after SSO. See docs/FINAL_INTEGRATION_BLOCKERS_20261118.md and deploy/RELEASE_GATES.md. Main, VPS_1 production data, secrets, Nginx, and GPU images are untouched.
