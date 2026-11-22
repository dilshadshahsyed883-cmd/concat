# Issue 16 — canonical read-only Drive intake

Concat runs as `concatweb` and cannot read Scene's private worker HOME.
`ProductionQueue` now uses a Unix-socket client instead of running rclone for
Drive scans/downloads. No credentials are copied, no permissions on Scene HOME
are changed, and there is no direct-rclone or anonymous fallback for intake.

The companion VPS_1 implementation is on `feature/shared-drive-bridge` in
`dilshadshahsyed883-cmd/vps1-deploy-control`:
`production/concat_drive_bridge.py`, an inert systemd template, tests and
`docs/CONCAT_DRIVE_BRIDGE.md`. It runs separately as the existing canonical
Scene identity and reuses the original DriveIntake/rclone contract. Only
`concatweb`'s kernel UID can call health, scan or bounded ZIP download; no caller
paths, arbitrary commands, upload/delete or remote selectors are accepted.

`CONCAT_DRIVE_BRIDGE_SOCKET` defaults to
`/run/concat-drive-bridge/intake.sock`. This non-secret setting can be supplied
by the host environment. An absent/unhealthy bridge stops intake safely;
GPU config checks the canonical remote through the bridge rather than merely
checking whether a local rclone executable exists.

Owner identity remains derived from authenticated Concat principals. Socket
requests carry the existing tenant SHA256; owner queues and staging remain
separate, with no admin cross-owner bypass. ZIP streams are written exclusively
to new private local files, checked for exact byte count and SHA256, then checked
by the existing queue for scanned size/MD5, bundle validity and GPU dispatch.
Corrupt/truncated transfers remove the partial file. Folder resource keys and
direct-link discovery use the existing canonical parser and downloader.
Source permission, missing item, auth and configuration errors are sanitized;
failed scans create no queue rows. No changes to output MP4 Drive upload,
verified cleanup, Salad, OAuth, YouTube publishing or queue schema.

## Activation and verification

Merge source into `integration/personal-release-20261119` only after CI passes.
Do not merge the release PR to main or deploy automatically. The companion
bridge must be installed from its immutable reviewed VPS1 feature-branch SHA
using its documented dedicated service setup; existing VPS1 services stay live.
No Scene or OAuth change is required. Rollback stops only the new bridge and
restores the previous Concat release, preserving databases and credentials.

Offline tests cover the protocol, peer UID rejection, sanitized denial, resource
keys, private spool cleanup, no caller paths, socket outage, integrity failures,
no-rclone intake, owner-specific queue creation and zero rows on denied scans.
Real allowed/unshared Drive folders and GPU acceptance still require manual
host activation. Source merge does not establish that production is repaired.
