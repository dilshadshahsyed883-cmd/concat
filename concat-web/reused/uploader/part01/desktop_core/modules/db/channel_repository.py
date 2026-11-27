from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .repository_base import BaseRepository
from ..secret_store import protect_secret
from ..token_utils import VALID_TOKEN, classify_google_json


class ChannelRepository(BaseRepository):
    def list_sheet_sources(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM sheet_sources ORDER BY sheet_name COLLATE NOCASE"
            ).fetchall()]

    def get_sheet_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sheet_sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def save_sheet_source(
        self,
        *,
        sheet_name: str,
        sheet_url_or_id: str,
        gid: str = "0",
        json_folder_path: str = "",
        enabled: bool = True,
        source_id: int | None = None,
    ) -> int:
        name = (sheet_name or "").strip() or "Google Sheet"
        url = (sheet_url_or_id or "").strip()
        if not url:
            raise ValueError("Sheet URL/ID is required.")
        gid = (gid or "0").strip() or "0"
        folder = (json_folder_path or "").strip()
        with self.connect() as conn:
            if source_id:
                conn.execute(
                    """
                    UPDATE sheet_sources SET sheet_name=?, sheet_url_or_id=?, gid=?, json_folder_path=?,
                        enabled=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (name, url, gid, folder, 1 if enabled else 0, int(source_id)),
                )
                return int(source_id)
            cur = conn.execute(
                """
                INSERT INTO sheet_sources(sheet_name, sheet_url_or_id, gid, json_folder_path, enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, url, gid, folder, 1 if enabled else 0),
            )
            return int(cur.lastrowid)

    def delete_sheet_source(self, source_id: int) -> None:
        with self.connect() as conn:
            # Removing a source removes its rows from current inventory without
            # destroying OAuth/token history or changing app-owned status.
            conn.execute("UPDATE channels SET sheet_present=0 WHERE sheet_source_id=?", (int(source_id),))
            conn.execute(
                """
                DELETE FROM channel_group_members
                WHERE channel_db_id IN (SELECT id FROM channels WHERE sheet_source_id=?)
                """,
                (int(source_id),),
            )
            conn.execute("DELETE FROM sheet_sources WHERE id=?", (int(source_id),))
            conn.execute(
                """
                DELETE FROM channel_groups
                WHERE COALESCE(notes, '') LIKE '%Google Sheet sync%'
                  AND id NOT IN (SELECT DISTINCT group_id FROM channel_group_members)
                """
            )

    def update_sheet_source_json_folder(self, source_id: int, json_folder_path: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sheet_sources SET json_folder_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(json_folder_path or "").strip(), int(source_id)),
            )

    def update_sheet_sync_status(self, source_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sheet_sources
                SET last_sync_at=CURRENT_TIMESTAMP, last_sync_status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (status, int(source_id)),
            )

    def upsert_sheet_channel(self, source_id: int, row: Dict[str, Any]) -> int:
        incoming_channel_id = str(row.get("channel_id") or "").strip()
        row_key = str(row.get("sheet_row_key") or incoming_channel_id or row.get("gmail")).strip().lower()
        if not incoming_channel_id:
            raise ValueError("channel_id is required for sheet channel upsert")

        with self.connect() as conn:
            # The sheet/source row identity is primary. OAuth must enrich this
            # exact row instead of creating or moving a visible row.
            existing = conn.execute(
                "SELECT * FROM channels WHERE sheet_source_id=? AND sheet_row_key=?",
                (int(source_id), row_key),
            ).fetchone()

            # Sr/row keys may legitimately change when a Sheet is reordered.
            # A verified YouTube Channel ID is the stronger identity, so retain
            # the same database row, queue history, and operational status.
            if not existing and incoming_channel_id.startswith("UC"):
                existing = conn.execute(
                    """
                    SELECT * FROM channels
                    WHERE sheet_source_id=? AND channel_id=?
                    ORDER BY COALESCE(sheet_present, 1) DESC, id ASC
                    LIMIT 1
                    """,
                    (int(source_id), incoming_channel_id),
                ).fetchone()

            # Do not reuse an old row merely because it already owns the UC ID.
            # Current Sheet inventory must keep its own Sheet-linked row. Any old
            # owner is detached below so the new/current Sheet row can claim the
            # unique real channel ID without creating a duplicate current row.
            if not existing:
                gmail = str(row.get("gmail") or "").strip().lower()
                name = str(row.get("channel_name") or "").strip().lower()
                if gmail and name:
                    existing = conn.execute(
                        """
                        SELECT * FROM channels
                        WHERE LOWER(COALESCE(google_account_email,''))=?
                          AND LOWER(COALESCE(channel_name,''))=?
                          AND (sheet_source_id IS NULL OR COALESCE(sheet_present,1)=0)
                        ORDER BY CASE WHEN channel_id LIKE 'UC%' THEN 0 ELSE 1 END, id ASC
                        LIMIT 1
                        """,
                        (gmail, name),
                    ).fetchone()

            incoming_token_path = str(row.get("token_file_path") or "").strip()
            incoming_is_synthetic = incoming_channel_id.lower().startswith("sheet:")
            canonical_channel_id = (
                f"sheet:{int(source_id)}:{row_key}" if incoming_is_synthetic else incoming_channel_id
            )
            canonical_token_path = incoming_token_path
            canonical_channel_url = str(row.get("channel_url") or "").strip()
            app_status = "ACTIVE"

            # A real YouTube channel ID can belong to only one current row.
            # If the ID is still attached to an old/manual/removed row, the
            # current Sheet inventory row wins. Detach the historical row from
            # the unique UC ID while preserving its token/history. A conflict
            # between two rows that are both currently present in Sheets is a
            # genuine duplicate and must not be silently reassigned.
            if canonical_channel_id.startswith("UC"):
                owner = conn.execute(
                    "SELECT * FROM channels WHERE channel_id=? AND id<>? LIMIT 1",
                    (canonical_channel_id, int(existing["id"]) if existing else -1),
                ).fetchone()
                if owner:
                    owner_is_current_sheet = (
                        owner["sheet_source_id"] is not None
                        and int(owner["sheet_present"] if owner["sheet_present"] is not None else 1) == 1
                    )
                    if owner_is_current_sheet:
                        # Duplicate real channel IDs in current Sheet inventory must
                        # never abort the whole sync. Keep the first current owner
                        # of the unique UC identity and sync this duplicate row under
                        # its stable Sheet-row identity instead. The original channel
                        # URL remains visible as inventory data, while upload safety
                        # continues to rely on a single canonical real channel ID.
                        canonical_channel_id = f"sheet:{int(source_id)}:{row_key}"
                    else:
                        owner_token_path = str(owner["token_file_path"] or "").strip()
                        if classify_google_json(canonical_token_path) != VALID_TOKEN and classify_google_json(owner_token_path) == VALID_TOKEN:
                            canonical_token_path = owner_token_path

                        historical_id = f"history:{int(owner['id'])}:{incoming_channel_id}"
                        conn.execute(
                            "UPDATE channels SET channel_id=?, sheet_present=0, last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                            (historical_id, int(owner["id"])),
                        )

                    # Current/current duplicates are intentionally left as separate
                    # Sheet inventory rows. Do not detach either current row.
                    if owner_is_current_sheet:
                        owner = None

                    owner_token_path = str(owner["token_file_path"] or "").strip() if owner else ""

            if existing:
                # Operational status belongs to the app and survives every sheet
                # refresh. A verified OAuth identity/token also survives a sheet
                # row that still contains only a handle or blank link.
                app_status = str(existing["status"] or "ACTIVE").strip().upper() or "ACTIVE"
                existing_channel_id = str(existing["channel_id"] or "").strip()
                existing_token_path = str(existing["token_file_path"] or "").strip()
                existing_url = str(existing["channel_url"] or "").strip()
                existing_is_verified = existing_channel_id.startswith("UC")
                if incoming_is_synthetic and existing_is_verified:
                    canonical_channel_id = existing_channel_id
                    canonical_channel_url = existing_url or canonical_channel_url
                same_channel_identity = canonical_channel_id == existing_channel_id
                if (
                    same_channel_identity
                    and classify_google_json(incoming_token_path) != VALID_TOKEN
                    and classify_google_json(existing_token_path) == VALID_TOKEN
                ):
                    canonical_token_path = existing_token_path
                elif existing_is_verified and not incoming_is_synthetic and not same_channel_identity:
                    # Never carry a valid token across a changed real Channel ID,
                    # even when the Sheet Sr/row identity stayed the same.
                    app_status = "TOKEN_MISSING"
                    conn.execute(
                        "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (NULL, 'WARNING', ?, NULL)",
                        (
                            f"Channel identity changed for Sheet row {row_key}: "
                            f"{existing_channel_id} -> {canonical_channel_id}. Reauthorization required.",
                        ),
                    )

            values = (
                canonical_channel_id,
                str(row.get("channel_name") or "").strip() or str(row.get("gmail") or canonical_channel_id),
                str(row.get("gmail") or "").strip(),
                canonical_token_path,
                canonical_channel_url,
                str(row.get("browser_profile_name") or "").strip(),
                str(row.get("proxy_scheme") or "").strip(),
                str(row.get("proxy_host") or "").strip(),
                str(row.get("proxy_port") or "").strip(),
                str(row.get("proxy_username") or "").strip(),
                protect_secret(str(row.get("proxy_password") or "").strip()),
                app_status,
                int(source_id),
                row_key,
                str(row.get("sheet_notes") or "").strip(),
                1,
                str(row.get("sheet_sr") or "").strip(),
                str(row.get("sheet_ip_raw") or "").strip(),
                protect_secret(str(row.get("sheet_password") or "").strip()),
                protect_secret(str(row.get("sheet_backup_code") or "").strip()),
                protect_secret(str(row.get("sheet_backup_key") or "").strip()),
                str(row.get("sheet_status") or "").strip(),
            )
            if existing:
                conn.execute(
                    """
                    UPDATE channels SET
                        channel_id=?, channel_name=?, google_account_email=?, token_file_path=?, channel_url=?,
                        browser_profile_name=?, proxy_scheme=?, proxy_host=?, proxy_port=?, proxy_username=?, proxy_password=?,
                        status=?, sheet_source_id=?, sheet_row_key=?, sheet_notes=?, sheet_present=?,
                        sheet_sr=?, sheet_ip_raw=?, sheet_password=?, sheet_backup_code=?, sheet_backup_key=?, sheet_status=?,
                        sheet_last_seen_at=CURRENT_TIMESTAMP, last_used_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (*values, int(existing["id"])),
                )
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO channels(
                    channel_id, channel_name, google_account_email, token_file_path, channel_url,
                    browser_profile_name, proxy_scheme, proxy_host, proxy_port, proxy_username, proxy_password,
                    status, sheet_source_id, sheet_row_key, sheet_notes, sheet_present,
                    sheet_sr, sheet_ip_raw, sheet_password, sheet_backup_code, sheet_backup_key, sheet_status,
                    sheet_last_seen_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                values,
            )
            return int(cur.lastrowid)

    def mark_missing_sheet_channels_inactive(self, source_id: int, active_keys: Iterable[str]) -> int:
        """Hide sheet rows no longer present without changing app-owned status."""
        keys = sorted({str(k).strip().lower() for k in active_keys if str(k).strip()})
        with self.connect() as conn:
            if keys:
                placeholders = ",".join("?" for _ in keys)
                cur = conn.execute(
                    f"""
                    UPDATE channels
                    SET sheet_present=0, last_used_at=CURRENT_TIMESTAMP
                    WHERE sheet_source_id=? AND COALESCE(sheet_row_key, '') NOT IN ({placeholders})
                      AND COALESCE(sheet_present, 1)<>0
                    """,
                    [int(source_id), *keys],
                )
            else:
                cur = conn.execute(
                    "UPDATE channels SET sheet_present=0, last_used_at=CURRENT_TIMESTAMP WHERE sheet_source_id=? AND COALESCE(sheet_present,1)<>0",
                    (int(source_id),),
                )
            return int(cur.rowcount)

    def add_or_update_channel(
        self,
        *,
        channel_id: str,
        channel_name: str,
        google_account_email: str | None,
        token_file_path: str,
        status: str = "ACTIVE",
        channel_url: str = "",
        browser_profile_name: str = "",
        proxy_scheme: str = "",
        proxy_host: str = "",
        proxy_port: str = "",
        proxy_username: str = "",
        proxy_password: str = "",
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO channels(
                    channel_id, channel_name, google_account_email, token_file_path, channel_url,
                    browser_profile_name, proxy_scheme, proxy_host, proxy_port, proxy_username, proxy_password,
                    status, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel_id) DO UPDATE SET
                    channel_name=excluded.channel_name,
                    google_account_email=CASE
                        WHEN TRIM(COALESCE(excluded.google_account_email, '')) <> ''
                        THEN excluded.google_account_email
                        ELSE channels.google_account_email
                    END,
                    token_file_path=excluded.token_file_path,
                    channel_url=excluded.channel_url,
                    browser_profile_name=excluded.browser_profile_name,
                    proxy_scheme=excluded.proxy_scheme,
                    proxy_host=excluded.proxy_host,
                    proxy_port=excluded.proxy_port,
                    proxy_username=excluded.proxy_username,
                    proxy_password=excluded.proxy_password,
                    status=excluded.status,
                    last_used_at=CURRENT_TIMESTAMP
                """,
                (
                    channel_id, channel_name, google_account_email, token_file_path, channel_url,
                    browser_profile_name, proxy_scheme, proxy_host, proxy_port, proxy_username, protect_secret(proxy_password),
                    status,
                ),
            )
            row = conn.execute("SELECT id FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
            return int(row["id"])

    def update_channel_network_profile(
        self,
        channel_db_id: int,
        *,
        browser_profile_name: str = "",
        proxy_scheme: str = "",
        proxy_host: str = "",
        proxy_port: str = "",
        proxy_username: str = "",
        proxy_password: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE channels SET
                    browser_profile_name=?, proxy_scheme=?, proxy_host=?, proxy_port=?,
                    proxy_username=?, proxy_password=?, last_used_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    browser_profile_name, proxy_scheme, proxy_host, proxy_port,
                    proxy_username, protect_secret(proxy_password), channel_db_id,
                ),
            )

    def list_channels(self, active_only: bool = False) -> List[Dict[str, Any]]:
        # Current channel inventory is defined by rows presently available in
        # connected Google Sheets. Historical rows remain stored for token reuse.
        query = "SELECT * FROM channels WHERE sheet_source_id IS NOT NULL AND COALESCE(sheet_present,1)=1"
        params: tuple[Any, ...] = ()
        if active_only:
            query += " AND status='ACTIVE'"
        query += " ORDER BY id ASC"
        with self.connect() as conn:
            return [self._decode_sensitive_record(r) for r in conn.execute(query, params).fetchall()]

    def get_channel(self, channel_db_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_db_id,)).fetchone()
            return self._decode_sensitive_record(row) if row else None

    def set_channel_status(self, channel_db_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE channels SET status=? WHERE id=?", (status, channel_db_id))

    def update_channel_after_oauth(
        self,
        channel_db_id: int,
        *,
        channel_id: str,
        channel_name: str,
        channel_url: str,
        token_file_path: str,
        google_account_email: str | None = None,
        browser_profile_name: str = "",
        proxy_scheme: str = "",
        proxy_host: str = "",
        proxy_port: str = "",
        proxy_username: str = "",
        proxy_password: str = "",
    ) -> int:
        """Update an existing sheet/imported channel after OAuth without creating duplicates."""
        with self.connect() as conn:
            existing_same_channel = conn.execute(
                "SELECT id FROM channels WHERE channel_id=? AND id<>?",
                (str(channel_id).strip(), int(channel_db_id)),
            ).fetchone()
            if existing_same_channel:
                # Keep the selected row as the canonical row and avoid UNIQUE conflicts.
                other_id = int(existing_same_channel["id"])
                conn.execute(
                    "UPDATE videos SET channel_db_id=? WHERE channel_db_id=?",
                    (int(channel_db_id), other_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO channel_group_members(group_id, channel_db_id) SELECT group_id, ? FROM channel_group_members WHERE channel_db_id=?",
                    (int(channel_db_id), other_id),
                )
                conn.execute("DELETE FROM channel_group_members WHERE channel_db_id=?", (other_id,))
                conn.execute("DELETE FROM channels WHERE id=?", (other_id,))

            current = conn.execute("SELECT * FROM channels WHERE id=?", (int(channel_db_id),)).fetchone()
            if not current:
                raise ValueError("Selected channel was not found in database.")

            email = (google_account_email or current["google_account_email"] or "").strip() or None
            profile = browser_profile_name if str(browser_profile_name).strip() else (current["browser_profile_name"] or "")
            pscheme = proxy_scheme if str(proxy_scheme).strip() else (current["proxy_scheme"] or "")
            phost = proxy_host if str(proxy_host).strip() else (current["proxy_host"] or "")
            pport = proxy_port if str(proxy_port).strip() else (current["proxy_port"] or "")
            puser = proxy_username if str(proxy_username).strip() else (current["proxy_username"] or "")
            # ``current`` comes directly from SQLite, so an existing proxy
            # password is already DPAPI-protected. Re-encrypting it here would
            # corrupt the credential every time OAuth refreshes channel details.
            ppass = protect_secret(proxy_password) if str(proxy_password).strip() else (current["proxy_password"] or "")

            app_status = str(current["status"] or "ACTIVE").strip().upper() or "ACTIVE"
            previous_token_status = classify_google_json(str(current["token_file_path"] or "").strip())
            # A successful OAuth flow has already verified the real channel and
            # saved a reusable token. Recover token-derived states. An INACTIVE
            # row is also reactivated when its previous token file was missing,
            # which distinguishes automatic token-safety deactivation from a
            # deliberate INACTIVE state that still has a valid token.
            if app_status in {"INVALID_TOKEN", "TOKEN_MISSING"} or (
                app_status == "INACTIVE" and previous_token_status != VALID_TOKEN
            ):
                app_status = "ACTIVE"
            conn.execute(
                """
                UPDATE channels SET
                    channel_id=?, channel_name=?, google_account_email=?, token_file_path=?, channel_url=?,
                    browser_profile_name=?, proxy_scheme=?, proxy_host=?, proxy_port=?, proxy_username=?, proxy_password=?,
                    status=?, last_used_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    str(channel_id).strip(), str(channel_name).strip(), email, str(token_file_path).strip(), str(channel_url).strip(),
                    profile, pscheme, phost, pport, puser, ppass, app_status, int(channel_db_id),
                ),
            )
            return int(channel_db_id)

    def update_channel_token_file(self, channel_db_id: int, token_file_path: str, status: Optional[str] = None) -> None:
        with self.connect() as conn:
            if status:
                conn.execute(
                    """
                    UPDATE channels
                    SET token_file_path=?, status=?, last_used_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (str(token_file_path or "").strip(), str(status).strip().upper(), int(channel_db_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE channels
                    SET token_file_path=?, last_used_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (str(token_file_path or "").strip(), int(channel_db_id)),
                )

    def set_channel_status_bulk(self, channel_db_ids: List[int], status: str) -> int:
        ids = sorted({int(x) for x in channel_db_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE channels SET status=?, last_used_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                [status, *ids],
            )
            return int(cur.rowcount)

    def delete_manual_channels(self, channel_db_ids: List[int]) -> Dict[str, int]:
        ids = sorted({int(x) for x in channel_db_ids})
        if not ids:
            return {"deleted": 0, "blocked_with_queue": 0, "not_manual": 0}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            manual_rows = conn.execute(
                f"SELECT id FROM channels WHERE id IN ({placeholders}) AND sheet_source_id IS NULL",
                ids,
            ).fetchall()
            manual_ids = [int(r["id"]) for r in manual_rows]
            not_manual = len(ids) - len(manual_ids)
            if not manual_ids:
                return {"deleted": 0, "blocked_with_queue": 0, "not_manual": not_manual}
            manual_placeholders = ",".join("?" for _ in manual_ids)
            blocked_rows = conn.execute(
                f"SELECT DISTINCT channel_db_id AS id FROM videos WHERE channel_db_id IN ({manual_placeholders})",
                manual_ids,
            ).fetchall()
            blocked_ids = {int(r["id"]) for r in blocked_rows}
            delete_ids = [cid for cid in manual_ids if cid not in blocked_ids]
            if not delete_ids:
                return {"deleted": 0, "blocked_with_queue": len(blocked_ids), "not_manual": not_manual}
            delete_placeholders = ",".join("?" for _ in delete_ids)
            conn.execute(f"DELETE FROM channel_group_members WHERE channel_db_id IN ({delete_placeholders})", delete_ids)
            cur = conn.execute(
                f"DELETE FROM channels WHERE id IN ({delete_placeholders}) AND sheet_source_id IS NULL",
                delete_ids,
            )
            conn.execute(
                """
                DELETE FROM channel_groups
                WHERE COALESCE(notes, '') LIKE '%Google Sheet sync%'
                  AND id NOT IN (SELECT DISTINCT group_id FROM channel_group_members)
                """
            )
            return {"deleted": int(cur.rowcount), "blocked_with_queue": len(blocked_ids), "not_manual": not_manual}

    def channel_group_names_map(self) -> Dict[int, str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.channel_db_id, GROUP_CONCAT(g.group_name, ', ') AS groups
                FROM channel_group_members m
                JOIN channel_groups g ON g.id = m.group_id
                GROUP BY m.channel_db_id
                """
            ).fetchall()
            return {int(r["channel_db_id"]): (r["groups"] or "") for r in rows}

    def add_channels_to_group(self, group_id: int, channel_db_ids: List[int]) -> int:
        ids = sorted({int(x) for x in channel_db_ids})
        if not ids:
            return 0
        with self.connect() as conn:
            group = conn.execute("SELECT id FROM channel_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise ValueError("Selected group was not found.")
            conn.executemany(
                "INSERT OR IGNORE INTO channel_group_members(group_id, channel_db_id) VALUES (?, ?)",
                [(int(group_id), cid) for cid in ids],
            )
            conn.execute("UPDATE channel_groups SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (group_id,))
            return len(ids)

    def ensure_channel_group(self, group_name: str, notes: str = "") -> int:
        name = group_name.strip()
        if not name:
            raise ValueError("Group name is required.")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_groups(group_name, notes, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_name) DO UPDATE SET
                    notes=CASE
                        WHEN COALESCE(channel_groups.notes, '')='' OR COALESCE(channel_groups.notes, '') LIKE '%Google Sheet sync%'
                        THEN excluded.notes
                        ELSE channel_groups.notes
                    END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (name, notes),
            )
            row = conn.execute("SELECT id FROM channel_groups WHERE group_name=?", (name,)).fetchone()
            return int(row["id"])

    def clear_sheet_channel_group_memberships(self, source_id: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM channel_group_members
                WHERE channel_db_id IN (SELECT id FROM channels WHERE sheet_source_id=?)
                """,
                (int(source_id),),
            )
            return int(cur.rowcount)

    def delete_empty_sheet_sync_groups(self) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM channel_groups
                WHERE COALESCE(notes, '') LIKE '%Google Sheet sync%'
                  AND id NOT IN (SELECT DISTINCT group_id FROM channel_group_members)
                """
            )
            return int(cur.rowcount)

    def dashboard_stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            channel_rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM channels "
                "WHERE sheet_source_id IS NOT NULL AND COALESCE(sheet_present,1)=1 GROUP BY status"
            ).fetchall()
            video_rows = conn.execute("SELECT status, COUNT(*) AS total FROM videos GROUP BY status").fetchall()
            token_rows = conn.execute("SELECT token_file_path FROM channels WHERE sheet_source_id IS NOT NULL AND COALESCE(sheet_present,1)=1").fetchall()
            groups = conn.execute("SELECT COUNT(*) AS total FROM channel_groups").fetchone()["total"]
            sheets = conn.execute("SELECT COUNT(*) AS total FROM sheet_sources").fetchone()["total"]
            return {
                "channels": {str(r["status"]): int(r["total"]) for r in channel_rows},
                "videos": {str(r["status"]): int(r["total"]) for r in video_rows},
                "groups": int(groups),
                "sheets": int(sheets),
                "token_files": [str(r["token_file_path"] or "") for r in token_rows],
            }

    def list_channel_groups(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.*, COUNT(m.channel_db_id) AS channel_count
                FROM channel_groups g
                LEFT JOIN channel_group_members m ON m.group_id = g.id
                GROUP BY g.id
                ORDER BY g.group_name COLLATE NOCASE
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_channel_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM channel_groups WHERE id=?", (group_id,)).fetchone()
            return dict(row) if row else None

    def get_channel_group_by_name(self, group_name: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_groups WHERE LOWER(group_name)=LOWER(?)",
                (group_name.strip(),),
            ).fetchone()
            return dict(row) if row else None

    def save_channel_group(self, group_name: str, channel_db_ids: List[int], notes: str = "") -> int:
        name = group_name.strip()
        if not name:
            raise ValueError("Group name is required.")
        if not channel_db_ids:
            raise ValueError("Select at least one channel for this group.")
        unique_ids = sorted({int(x) for x in channel_db_ids})
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_groups(group_name, notes, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_name) DO UPDATE SET
                    notes=excluded.notes,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (name, notes),
            )
            row = conn.execute("SELECT id FROM channel_groups WHERE group_name=?", (name,)).fetchone()
            group_id = int(row["id"])
            conn.execute("DELETE FROM channel_group_members WHERE group_id=?", (group_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO channel_group_members(group_id, channel_db_id) VALUES (?, ?)",
                [(group_id, cid) for cid in unique_ids],
            )
            return group_id

    def delete_channel_group(self, group_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM channel_group_members WHERE group_id=?", (group_id,))
            conn.execute("DELETE FROM channel_groups WHERE id=?", (group_id,))

    def list_channel_group_members(self, group_id: int, active_only: bool = False) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            where = "WHERE m.group_id=?"
            if active_only:
                where += " AND c.status='ACTIVE'"
            rows = conn.execute(
                f"""
                SELECT c.*
                FROM channel_group_members m
                JOIN channels c ON c.id = m.channel_db_id
                {where}
                ORDER BY c.id ASC
                """,
                (group_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_channel_group_members_by_name(self, group_name: str, active_only: bool = False) -> List[Dict[str, Any]]:
        group = self.get_channel_group_by_name(group_name)
        if not group:
            return []
        return self.list_channel_group_members(int(group["id"]), active_only=active_only)
