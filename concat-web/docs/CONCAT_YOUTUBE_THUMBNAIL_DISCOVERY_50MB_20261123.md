# YouTube thumbnails.set: September 2026 50 MB discovery fix

## Root cause

YouTube increased the Data API `thumbnails.set` maximum from 2 MB to 50 MB
on September 14, 2026 (official API reference:
https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set ).

Concat Web already admitted thumbnails up to 50 MB using
`desktop_core.modules.thumbnail_limits.MAX_THUMBNAIL_BYTES`.
However the pinned `google-api-python-client==2.137.0` uses a bundled YouTube
discovery document when `build("youtube", "v3", cache_discovery=False)`
is called; `cache_discovery=False` does **not** force live discovery.
The stale `thumbnails.set.mediaUpload.maxSize="2MB"` descriptor causes
`MediaUploadSizeError: Media larger than: 2097152` **locally, before any
YouTube thumbnail HTTP request**. This is not evidence YouTube rejected 50 MB.

## Scoped correction

`AuthManager.build_youtube_service_from_credentials` updates only the
`thumbnails.set` maxSize descriptor to `50MB` immediately after building
the YouTube service, before the dynamic `thumbnails()` resource is generated.
It leaves the normal offline/static discovery, OAuth, proxy settings and all
other YouTube method descriptors untouched. Fail closed if the discovery
structure changes unexpectedly. This covers both the normal upload pipeline
and the Concat thumbnail-library cleanup/recovery path using this service.

Regression tests recreate the old 2 MB client-side rejection, verify that a
request for a thumbnail just above 2 MB and at 50 MB can be constructed,
reject sizes above 50 MB, and verify that the centralized OAuth service builder
applies the correction. No test sends a real thumbnail or video to YouTube.

## Recover four reported queue failures (#9–#12)

1. **Before retry or cleanup**, back up uploader/project SQLite and the
   thumbnail-library/staged files. Inspect project ID, owner/channel, the
   corresponding core upload ID, `youtube_video_id`, `remote_verified_at`,
   `thumbnail_stage_state`, thumbnail use/application receipt, and
   `concat_cleanup_pending`. A UI 100% indicator alone does *not* prove the
   video exists on YouTube.
2. For jobs with an independently verified YouTube ID and a pending library
   thumbnail receipt, keep the video ID and existing files. The existing
   `_retry_concat_cleanup()` path (or a verified-job Queue Retry) performs
   **only `thumbnails.set`** followed by receipt/cleanup. It does not call
   `videos.insert` or create a new core video row.
3. If the YouTube ID exists but `remote_verified_at` is missing, do not
   resume media upload automatically. Verify the remote video with the exact
   owner account/channel first; use the existing review-required safeguards.
4. If the core has no YouTube ID, do not assume an upload succeeded just
   because progress is 100%. Review actual backend logs and YouTube Studio
   before authorizing retry.
5. Verify thumbnail is visible on the *existing* YouTube video, the applied
   receipt is durable, queue state is completed, and cleanup removed only
   allowed staged copies. Confirm no duplicate YouTube video was created.

## Validation and release

- CI: `python -m unittest discover -s tests -p 'test_youtube_thumbnail_discovery.py' -v`
- Existing regression coverage: `test_reused_thumbnail_library.py`
  checks thumbnail-only recovery without repeating video upload.
- Merge only after the essential workflow is green; production VPS and
  real OAuth/YouTube operations require a separate guarded release and
  post-release observation.
- Source patch does not itself repair production rows or deploy code.
