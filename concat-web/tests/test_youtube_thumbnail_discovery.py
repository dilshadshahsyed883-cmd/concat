"""Regression tests for Google's September 2026 YouTube thumbnail size change.

The pinned google-api-python-client bundles an older discovery descriptor, so
cache_discovery=False alone does not update thumbnails.set's client-side cap.
These tests use no Google credentials, outbound requests, or real uploads.
"""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import MediaUploadSizeError
from googleapiclient.http import MediaIoBaseUpload

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reused" / "uploader" / "part01"))

from desktop_core.modules import auth_manager


class ReportedSizeUpload(MediaIoBaseUpload):
    """Small in-memory body with a controlled size, avoiding 50 MB allocations."""

    def __init__(self, reported_size):
        super().__init__(io.BytesIO(b"fake-image"), mimetype="image/png", resumable=True)
        self.reported_size = reported_size

    def size(self):
        return self.reported_size


class YoutubeThumbnailDiscoveryTests(unittest.TestCase):
    def test_old_client_limit_is_corrected_before_thumbnail_method_generation(self):
        service = build("youtube", "v3", http=httplib2.Http(), cache_discovery=False)
        thumbnail_media = service._rootDesc["resources"]["thumbnails"]["methods"]["set"]["mediaUpload"]
        # Explicitly emulate the pinned 2024 library's stale static descriptor.
        thumbnail_media["maxSize"] = "2MB"

        above_two_mb = ReportedSizeUpload(2 * 1024 * 1024 + 1)
        with self.assertRaises(MediaUploadSizeError):
            service.thumbnails().set(videoId="existing_youtube_video", media_body=above_two_mb)

        self.assertIs(auth_manager.apply_current_youtube_thumbnail_limit(service), service)
        self.assertEqual(thumbnail_media["maxSize"], "50MB")
        # The Google client can now construct a request. Never execute it here.
        request = service.thumbnails().set(
            videoId="existing_youtube_video",
            media_body=above_two_mb,
        )
        self.assertIn("upload", request.uri)

        at_limit = ReportedSizeUpload(50 * 1024 * 1024)
        service.thumbnails().set(videoId="existing_youtube_video", media_body=at_limit)
        with self.assertRaises(MediaUploadSizeError):
            service.thumbnails().set(
                videoId="existing_youtube_video",
                media_body=ReportedSizeUpload(50 * 1024 * 1024 + 1),
            )

    def test_actual_service_builder_applies_patch_for_every_oauth_channel(self):
        mock_service = MagicMock()
        mock_service._rootDesc = {
            "resources": {"thumbnails": {"methods": {"set": {
                "mediaUpload": {"maxSize": "2MB"},
            }}}}
        }
        with (
            patch.object(auth_manager, "build_proxied_http", return_value=MagicMock()),
            patch.object(auth_manager, "AuthorizedHttp", return_value=MagicMock()),
            patch.object(auth_manager, "build", return_value=mock_service) as builder,
        ):
            result = auth_manager.AuthManager.build_youtube_service_from_credentials(MagicMock())
        self.assertIs(result, mock_service)
        self.assertEqual(
            result._rootDesc["resources"]["thumbnails"]["methods"]["set"]["mediaUpload"]["maxSize"],
            "50MB",
        )
        builder.assert_called_once()
        self.assertFalse(builder.call_args.kwargs["cache_discovery"])

    def test_missing_discovery_descriptor_fails_closed(self):
        service = MagicMock()
        service._rootDesc = {"resources": {}}
        with self.assertRaisesRegex(RuntimeError, "thumbnails.set discovery metadata"):
            auth_manager.apply_current_youtube_thumbnail_limit(service)


if __name__ == "__main__":
    unittest.main()
