# Concat Web Drive Bridge access — issue #16

## Source of discrepancy (2026-11-19)

VPS_1 `apps/scene-builder/part01/cloud_render.py` resolves rclone from its Scene worker HOME. The independent Concat Web service runs as `concatweb` rather than `vps1scene`. The Scene worker HOME is private and is not an authorized credential source for `concatweb` without a deliberately scoped host access grant. Previous Concat logic blindly set HOME/XDG to that private directory, even when its service user could not traverse it. Every remote `lsjson` permission error was misreported as a **destination** permission error, even though folder scan is a **read**.

## Corrected contract

- Prefer a host-approved `RCLONE_CONFIG` explicitly configured on the isolated Concat unit and **readable as `concatweb`**. Use the remote name already configured by `CLOUD_RENDER_DRIVE_REMOTE` (default `gdrive`).
- If no explicit config is set, reuse the existing Scene worker config only if its directory/file is actually accessible to the Concat identity. Do not assume matching HOME grants access.
- If inaccessible, return a sanitized, actionable error **before invoking rclone**. Do not fall back to anonymous Drive, a second OAuth flow, or another account.
- Folder `lsjson` and Drive `copyid` failures are **source access** errors. Verified MP4 `copyto` failures remain **destination access** errors. Raw token/config path information must never reach a browser.
- No changes to VPS_1's rclone config, OAuth secrets, service identity, Nginx, Salad worker or queues.

## Host-only acceptance gate (not established by source tests)

A host administrator must verify that the `concatweb` service identity can read an *approved* canonical rclone configuration and that its `gdrive` remote addresses the intended account. This is a runtime permission/configuration task, **not a reason to chmod the Scene worker HOME or copy OAuth files into the repository**. Verify with a read-only `rclone listremotes` and `rclone lsjson` against an approved test folder from the same systemd identity/environment. Confirm authorized folder access, then Scan & Add to Queue creates owner-scoped ZIP rows. Negative check: an unshared folder must fail without creating rows. No credential contents or Drive folder URLs should be printed in logs or issue comments.

Until this scoped host check passes, **issue #16 is not confirmed fixed in production**. Do not automatically merge or deploy source changes to claim success.
