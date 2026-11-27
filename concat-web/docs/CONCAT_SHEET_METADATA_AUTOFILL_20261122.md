# Concat metadata sheet — basic explicit title/description sync

## Approved workflow

1. On Uploader > Sheets Sync, use **Add Metadata Sheet** to open the URL popup and save one metadata sheet for the authenticated workspace. The button becomes **Edit Metadata Sheet**. Saving again replaces the same workspace's configured sheet.
2. Put these columns in the sheet's **first worksheet tab**:

   | video name | title | description |
   | --- | --- | --- |
   | u1 | Example title | Example description |

3. On Upload Engine, choose a completed available video from **Concat Media**. Selection only fills the video URL; it does not fetch or overwrite metadata.
4. Press **Sync Sheet** beside Video Metadata. The server resolves the selected owner's real media name and reads the configured sheet using the existing first-tab CSV reader. `u1` matches `u1.mp4`, ignoring case and surrounding whitespace. There is no fuzzy, prefix or row-order matching.
5. Exactly one matching row fills **only Title and Description**. Explicit sync replaces those two fields, which remain manually editable. It preserves other upload fields and does not add a queue item or start an upload. New metadata applies only to the form being edited, never an existing queued upload.

## Boundaries and error handling

- Channel, group, visibility, schedule/timezone, tags, category, playlist, kids setting, thumbnail, queue admission, rendering, Salad/GPU and verified-upload cleanup keep their existing behavior.
- Metadata configuration uses an additive `concat_metadata_sheets` table in the uploader ProjectStore, one row per owner (case-insensitive primary key). It is separate from channel inventory sheets and cannot import/remove channels or mutate channel groups. Restart and repeated initialization retain configuration.
- Only canonical Google Sheets URLs/IDs are accepted. Only a validated spreadsheet ID is stored; the existing reader constructs the fixed Google export URL. URL query/GID fragments do not change the first-tab selection. Sheets must be accessible to the existing CSV reader (sharing/publishing); no new OAuth credentials are introduced.
- New endpoints are authenticated, owner-scoped, Concat-only and CSRF-protected POSTs, explicitly allowlisted by the existing outer gateway and child middleware:
  - `/actions/concat/metadata-sheet` saves configuration.
  - `/api/concat/metadata` returns only the selected URL, title and description.
- The browser supplies the selected canonical video URL, never an authoritative filename or another owner's identity. The server verifies the owner-specific available media list and reservations before reading the sheet. Missing, queued, busy or foreign media is rejected.
- Missing configuration, inaccessible sheets, no matching row, duplicate normalized names, missing columns or invalid lengths show a message and leave form text intact. Titles must be 1–100 characters; descriptions at most 5000. An explicitly blank description is permitted. Long values are rejected rather than silently truncated.
- Sheet text is assigned to form values and status messages via textContent, never HTML. Configuration changes during a sheet fetch invalidate the result. Selection/text edits or navigation while a request is pending prevent stale responses from changing the form. There is no background metadata sync or sheet writeback.

## Verification

Executed locally:

- `python -m unittest discover -s tests -v`: 161 maintained top-level tests pass, including the new isolated runner with eight metadata cases and the existing 20 media regression cases.
- `python -m unittest app.modules.concat.test_routes -v`: 12 pass.
- `python -m unittest discover -s app/modules/uploader/tests -v`: eight pass.
- Node metadata, media picker, channel picker and thumbnail regression suites pass. Metadata checks cover explicit-only requests, POST/CSRF payloads, failures, duplicate-click blocking, stale video/text protection, soft-navigation lifecycle and popup saving.
- Python compile, JS syntax and `git diff --check` pass.

CI also runs the real Chromium layout test at 1440, 1024, 760, 390 and 320 pixels. Extended browser assertions cover the actual Upload Engine and Sheets Sync templates/scripts/styles, no metadata request on selection, explicit successful sync, failed sync preserving values, untouched tags/category/schedule, responsive Sync button, and popup save/edit behavior. Chromium runs in CI because the local executor denies browser socket creation. PR check results are authoritative before merge.

All Python HTTP tests use real disposable SQLite/media stores, authentication/CSRF/gateway and mocked Google CSV responses. Browser requests are locally fulfilled. No live Google Sheet, production server, YouTube upload, queue, GPU or production file is modified during verification. Public-sheet accessibility requires a later user-run sync against their actual sheet.

## Release and rollback

Use the repository's existing exact-main-SHA manual deployment procedure after the approved merge. Do not deploy as part of implementation. The service's normal startup adds the table; no manual migration or secret change is required. The additive table may remain on rollback; older code ignores it. Keep all owner queue/history/media state and existing upload-cleanup receipts intact.
