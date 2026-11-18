from __future__ import annotations

import socket
import ssl
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, Optional
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import httplib2
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .auth_manager import AuthManager
from .database import Database
from .token_utils import mark_runtime_invalid_grant
from .upload_status import UploadStatus
from .upload_pipeline import LeaseLostError, UploadPipeline, UploadStopped
try:
    from .upload_reporting import export_uploaded_links_csv
except Exception:  # keep uploader usable even if reporting is unavailable
    export_uploaded_links_csv = None

ProgressCallback = Callable[[int, str], None]
StopCheck = Callable[[], bool]


class YouTubeUploader:
    NETWORK_TEST_URLS = (
        "https://www.googleapis.com/generate_204",
        "https://www.youtube.com/generate_204",
        "https://www.google.com/generate_204",
    )
    NETWORK_RETRY_SECONDS = 10
    NETWORK_LOG_EVERY_SECONDS = 60

    def __init__(self, db: Optional[Database] = None, auth: Optional[AuthManager] = None, worker_id: str | None = None):
        self.db = db or Database()
        self.auth = auth or AuthManager(self.db)
        self.worker_id = str(worker_id or f"uploader-{uuid.uuid4().hex}")

    def _renew_claim(self, video_id: int) -> None:
        """Renew ownership or stop immediately to prevent duplicate uploads."""

        try:
            renewed = self.db.renew_claim(video_id, self.worker_id)
        except Exception as exc:
            message = f"Queue lease renewal failed for worker {self.worker_id}: {exc}"
            try:
                self.db.add_log(video_id, "ERROR", message, None)
            except Exception:
                pass
            raise LeaseLostError(message) from exc
        if not renewed:
            message = (
                f"Queue lease lost for worker {self.worker_id}; upload stopped to prevent duplicate processing."
            )
            try:
                self.db.add_log(video_id, "ERROR", message, None)
            except Exception:
                pass
            raise LeaseLostError(message)

    @staticmethod
    def _tags_to_list(tags: str | None) -> list[str]:
        if not tags:
            return []
        return [tag.strip() for tag in tags.split(",") if tag.strip()]

    @staticmethod
    def _proxy_config_from_video(video: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "proxy_scheme": video.get("proxy_scheme"),
            "proxy_host": video.get("proxy_host"),
            "proxy_port": video.get("proxy_port"),
            "proxy_username": video.get("proxy_username"),
            "proxy_password": video.get("proxy_password"),
        }

    @staticmethod
    def _proxy_url(proxy_config: Optional[Dict[str, Any]]) -> str | None:
        config = proxy_config or {}
        host = str(config.get("proxy_host") or "").strip()
        port = str(config.get("proxy_port") or "").strip()
        if not host or not port:
            return None
        scheme = str(config.get("proxy_scheme") or "http").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "http"
        username = str(config.get("proxy_username") or "").strip()
        password = str(config.get("proxy_password") or "")
        auth = ""
        if username or password:
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{auth}{host}:{port}"

    @classmethod
    def internet_available(
        cls, proxy_config: Optional[Dict[str, Any]] = None, timeout: int = 12, max_urls: int | None = None
    ) -> bool:
        """Return True only when an HTTPS request can complete.

        When a channel has a proxy, the check uses the same proxy route. This keeps
        per-channel proxy outages from being mistaken for a global app problem.
        """
        opener = None
        proxy_url = cls._proxy_url(proxy_config)
        if proxy_url:
            opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        urls = cls.NETWORK_TEST_URLS[:max(1, int(max_urls))] if max_urls else cls.NETWORK_TEST_URLS
        for url in urls:
            try:
                req = Request(url, headers={"User-Agent": "LocalYouTubeUploadManager/1.0"})
                if opener:
                    with opener.open(req, timeout=timeout) as resp:
                        return 200 <= int(getattr(resp, "status", 200)) < 500
                with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed Google/YouTube HTTPS URLs only
                    return 200 <= int(getattr(resp, "status", 200)) < 500
            except Exception:
                continue
        return False

    @staticmethod
    def _is_transient_http_error(exc: HttpError) -> bool:
        status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
        return status in {408, 429, 500, 502, 503, 504}

    @staticmethod
    def _is_network_error(exc: BaseException) -> bool:
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)):
            return False
        network_types = (
            TimeoutError,
            ConnectionError,
            socket.timeout,
            socket.gaierror,
            ssl.SSLError,
            URLError,
            httplib2.ServerNotFoundError,
        )
        if isinstance(exc, network_types):
            return True
        name = exc.__class__.__name__.lower()
        text = str(exc).lower()
        needles = [
            "eof occurred in violation of protocol",
            "ssl",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "connection refused",
            "connection closed",
            "remote end closed connection",
            "temporarily unavailable",
            "network is unreachable",
            "name or service not known",
            "failed to establish a new connection",
            "server not found",
            "proxy error",
            "proxyconnectionerror",
            "cannot connect to proxy",
            "tunnel connection failed",
            "[errno 11001]",
            "[errno 10054]",
            "[errno 10060]",
            "[errno 10061]",
        ]
        return any(n in text for n in needles) or "proxy" in name or "socket" in name

    def _wait_for_connection(
        self,
        video_id: int,
        proxy_config: Optional[Dict[str, Any]],
        progress: int,
        reason: str,
        stop_check: Optional[StopCheck],
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        last_log_at = 0.0
        shown_reason = reason.strip() or "Internet/proxy connection is not available."
        while True:
            self._renew_claim(video_id)
            if stop_check and stop_check():
                self.db.update_video_status(video_id, status=UploadStatus.PAUSED, progress=progress, error_message="Stopped by user while waiting for internet/proxy connection.")
                self.db.add_log(video_id, "WARNING", "Stopped by user while waiting for internet/proxy connection", progress)
                raise UploadStopped("Stopped by user while waiting for internet/proxy connection.")

            self.db.update_video_status(video_id, status=UploadStatus.WAITING_NETWORK, progress=progress, error_message=shown_reason)
            now = time.monotonic()
            if now - last_log_at >= self.NETWORK_LOG_EVERY_SECONDS or last_log_at == 0:
                self.db.add_log(video_id, "WARNING", f"Waiting for internet/proxy connection: {shown_reason}", progress)
                last_log_at = now
            if progress_callback:
                progress_callback(progress, "Waiting for internet/proxy connection...")

            if self.internet_available(proxy_config):
                self.db.add_log(video_id, "INFO", "Internet/proxy connection restored. Resuming upload.", progress)
                if progress_callback:
                    progress_callback(progress, "Connection restored. Resuming upload...")
                return
            time.sleep(self.NETWORK_RETRY_SECONDS)

    def _build_youtube_service_with_network_wait(
        self,
        video_id: int,
        token_file_path: str,
        proxy_config: Dict[str, Any],
        stop_check: Optional[StopCheck],
        progress_callback: Optional[ProgressCallback],
    ):
        while True:
            if stop_check and stop_check():
                raise UploadStopped("Stopped by user before upload started.")
            if not self.internet_available(proxy_config):
                self._wait_for_connection(
                    video_id,
                    proxy_config,
                    0,
                    "No internet/proxy connection before upload start.",
                    stop_check,
                    progress_callback,
                )
            try:
                return self.auth.build_youtube_service(token_file_path, proxy_config=proxy_config)
            except Exception as exc:
                if self._is_network_error(exc):
                    self._wait_for_connection(video_id, proxy_config, 0, str(exc), stop_check, progress_callback)
                    continue
                raise

    def upload_video(
        self,
        video_id: int,
        progress_callback: Optional[ProgressCallback] = None,
        stop_check: Optional[StopCheck] = None,
    ) -> Dict[str, Any]:
        """Run one queue item through the checkpointed upload pipeline."""

        pipeline = UploadPipeline(
            db=self.db,
            worker_id=self.worker_id,
            build_service=self._build_youtube_service_with_network_wait,
            wait_for_connection=self._wait_for_connection,
            is_transient_http_error=self._is_transient_http_error,
            is_network_error=self._is_network_error,
            format_http_error=self._format_http_error,
            media_upload_factory=MediaFileUpload,
            export_uploaded_links=export_uploaded_links_csv,
        )
        return pipeline.run(
            video_id,
            progress_callback=progress_callback,
            stop_check=stop_check,
        ).as_dict()

    @staticmethod
    def _format_http_error(exc: HttpError) -> str:
        try:
            return exc.content.decode("utf-8", errors="replace")
        except Exception:
            return str(exc)

    def _process_one(
        self,
        item: Dict[str, Any],
        stop_check: Optional[StopCheck],
        progress_callback: Optional[Callable[[int, int, str], None]],
    ) -> None:
        vid = int(item["id"])
        try:
            self.upload_video(
                vid,
                progress_callback=lambda pct, msg, v=vid: progress_callback(v, pct, msg) if progress_callback else None,
                stop_check=stop_check,
            )
        except UploadStopped as exc:
            self.db.update_video_status(vid, status=UploadStatus.PAUSED, error_message=str(exc))
            self.db.add_log(vid, "WARNING", str(exc), None)
            if progress_callback:
                progress_callback(vid, 0, f"Paused: {exc}")
        except LeaseLostError as exc:
            # Change state only if this worker still owns the lease. If ownership
            # is already gone, touching the row could corrupt another worker.
            failed = False
            try:
                failed = self.db.fail_claimed_job(vid, self.worker_id, str(exc))
            except Exception as state_exc:
                try:
                    self.db.add_log(None, "ERROR", f"Could not persist lease-loss state for job #{vid}: {state_exc}", None)
                except Exception:
                    pass
            if progress_callback:
                suffix = "Job marked FAILED." if failed else "Job ownership changed; state left untouched."
                progress_callback(vid, 0, f"Stopped safely: {exc} {suffix}")
            time.sleep(1)
        except Exception as exc:
            error_text = str(exc)
            if "invalid_grant" in error_text.lower() or "invalid_scope" in error_text.lower():
                # Google can raise invalid_grant from an automatic refresh inside
                # AuthorizedHttp, after the service was already created. Mark the
                # token and its owning channel immediately so the Channels table
                # no longer reports a structurally present but unusable token as OK.
                try:
                    video = self.db.get_video(vid) or {}
                    token_path = str(video.get("token_file_path") or "").strip()
                    if token_path:
                        mark_runtime_invalid_grant(token_path)
                    channel_db_id = int(video.get("channel_db_id") or 0)
                    if channel_db_id:
                        self.db.set_channel_status(channel_db_id, "INVALID_TOKEN")
                except Exception:
                    # Never hide the original upload failure if status persistence
                    # itself encounters an unrelated filesystem/database problem.
                    pass
            self.db.update_video_status(
                vid,
                status=UploadStatus.FAILED,
                progress=0,
                error_message=error_text,
                increment_retry=True,
            )
            self.db.add_log(vid, "ERROR", str(exc), 0)
            if progress_callback:
                progress_callback(vid, 0, f"Failed: {exc}")
            time.sleep(1)
        finally:
            self.db.release_claim(vid, self.worker_id, requeue_if_claimed=True)

    def process_queue(
        self,
        stop_check: Optional[StopCheck] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        max_concurrent: int = 1,
        auto_engine_only: bool = False,
        exclude_auto_engine: bool = False,
        wait_for_future_schedules: bool = True,
        recovery_only: bool = False,
        only_video_id: int | None = None,
    ) -> None:
        """Process queue jobs with a rolling concurrency window.

        As soon as one active upload finishes, the next eligible job is claimed
        and started without waiting for the other uploads in the current group.
        Database leases still prevent duplicate jobs and same-channel overlap.
        """
        workers = max(1, min(5, int(max_concurrent or 1)))
        active = {}
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yt-upload") as executor:
                while True:
                    stopping = bool(stop_check and stop_check())

                    # Do not start new work after Stop is requested. Existing
                    # uploads receive stop_check and exit at their safe checkpoint.
                    if not stopping:
                        if only_video_id is None:
                            self.db.activate_due_scheduled_jobs(
                                auto_engine_only=auto_engine_only,
                                exclude_auto_engine=exclude_auto_engine,
                            )
                        free_slots = workers - len(active)
                        if free_slots > 0:
                            claimed = self.db.claim_available_jobs(
                                self.worker_id,
                                free_slots,
                                auto_engine_only=auto_engine_only,
                                exclude_auto_engine=exclude_auto_engine,
                                recovery_only=recovery_only,
                                **({"only_video_id": only_video_id} if only_video_id is not None else {}),
                            )
                            for item in claimed:
                                future = executor.submit(
                                    self._process_one, item, stop_check, progress_callback
                                )
                                active[future] = int(item["id"])

                    if active:
                        # FIRST_COMPLETED is the key rolling-pool behavior: when
                        # upload 1 of 5 completes, slot 1 is immediately refilled.
                        done, _ = wait(
                            tuple(active), timeout=1.0,
                            return_when=FIRST_COMPLETED,
                        )
                        for future in done:
                            video_id = active.pop(future, None)
                            try:
                                future.result()
                            except Exception as exc:
                                self.db.add_log(
                                    video_id, "ERROR",
                                    f"Queue worker error: {exc}", None,
                                )
                        continue

                    if stopping:
                        return

                    future = None if only_video_id is not None else self.db.next_scheduled_upload_at(
                        auto_engine_only=auto_engine_only,
                        exclude_auto_engine=exclude_auto_engine,
                    )
                    if future:
                        if not wait_for_future_schedules:
                            return
                        time.sleep(5)
                        continue

                    if self.db.queued_work_exists(
                        auto_engine_only=auto_engine_only,
                        exclude_auto_engine=exclude_auto_engine,
                        recovery_only=recovery_only,
                        **({"only_video_id": only_video_id} if only_video_id is not None else {}),
                    ):
                        # Jobs may temporarily be blocked by a live lease or by
                        # same-channel protection. Recheck without ending worker.
                        time.sleep(1)
                        continue
                    return
        finally:
            self.db.release_worker_claims(self.worker_id, requeue_claimed=True)
