# Saved Drive source and manual sync

Production's Add Concat Projects section has Save / Edit Drive Link and Sync
Drive. The popup stores one validated Google Drive folder/ZIP link for the
verified owner in `config.production_drive_source`. A folder's resource key is
preserved. Saving does not scan Drive, download files, or start a worker.

Sync Drive scans a snapshot of that saved link using the existing canonical
Drive Bridge and queue admission flow. Existing Drive IDs, including completed
entries still in the queue, are skipped. Pause remains paused. Manual Scan &
Add to Queue accepts a different one-off link without changing the saved link.
No scheduled/automatic sync is added. Saving a new source in another tab affects
the next sync, not an in-flight scan.

The UI shows an indeterminate busy label, disables repeated scan/sync actions,
and reports added/duplicate/failed counts after successful discovery. Discovery
or permission failures display the error and restore controls. Counts concern
queue admission; subsequent ZIP download/render failures appear on queue items.
A local per-owner sync lock rejects simultaneous sync requests in one web
process; the existing database unique Drive ID and transaction prevent duplicate
queue entries across processes and manual scans.

Endpoints: GET/POST `/api/cloud-render/production/drive-source` and POST
`/api/cloud-render/production/sync-drive`. All require current owner access;
mutations require CSRF/same-origin validation. Saved-link GET is private/no-store.
Only HTTPS drive.google.com folder/ZIP links accepted by existing validation
are stored. Network discovery runs in the thread pool and does not hold the
manager/database lock. Links are not included in performance-report exports.

Offline coverage: save/edit validation, owner isolation and revoked access,
CSRF/cross-origin rejection, persistence, no scan on save, duplicate admission,
pause preservation, one-off manual links, empty results, permission failures,
overlap lock recovery, busy-state/double-click guards and form state restoration.
Chromium CI exercises the popup, save, sync, duplicate results, keyboard close,
manual input preservation and responsiveness at 1440/760/390/320px.
