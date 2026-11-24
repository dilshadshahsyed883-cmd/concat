# Sheet-controlled upload drafts

The existing manual Upload Engine form, its Add Video to Queue button, manual
thumbnail upload and Thumbnail Library remain available.

The metadata sheet's first tab uses these headers:

| video name | title | description | group | channel | visibility | upload start | timezone |
| --- | --- | --- | --- | --- | --- | --- | --- |
| example.mp4 | Example title | Description | Tennis | Tennis Frontline | public | 2099-12-01 18:00 | Asia/Karachi |

Video names match exactly, ignoring case and an optional `.mp4` suffix.
Group/channel must identify the owner's current inventory. Channel may be a
unique channel name, YouTube channel ID or database ID within the selected group.
Blank visibility defaults to public; blank timezone defaults to Asia/Karachi.
Blank upload start uses normal queue eligibility. A date/time schedules the
start of uploading, not YouTube publication. Schedules must be in the future.

- Selecting a Concat Media video and pressing Sync Sheet fills its manual form
  fields and creates one editable preview row.
- Leaving the dropdown blank and pressing Sync Sheet previews all available,
  owner-verified, completed, unreserved media, including rows with match errors.
- Sync never inserts queue entries. Review title/description, destination,
  visibility, schedule and each row's optional library thumbnail first.
- Preview rows have independent Thumbnail Library selection. The existing manual
  file selection is used only by manual admission, never copied across a batch.
- Correct invalid fields, explicitly select ready rows, remove unwanted drafts,
  then press Add videos to Queue. Duplicate/missing sheet matches need fixing in
  the sheet followed by another sync.
- Each row submits through the existing authenticated admission endpoint,
  rechecking channel ownership/current inventory, group membership, source hash,
  duplicate claims, schedule and thumbnail ownership. Admissions are serial,
  with independent success/failure statuses. A lost/unknown response stops the
  batch; check Queue and refresh/sync before retrying.
- Existing five-minute cancellation eligibility, explicit pause, upload timing,
  YouTube verification and cleanup rules continue to apply.

Tags, category, playlist and Made for Kids retain the manual form's current
values when batch rows are submitted. Clear preview discards drafts only;
accepted queue entries are managed from Queue.
