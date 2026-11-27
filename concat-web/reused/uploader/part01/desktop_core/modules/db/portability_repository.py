from __future__ import annotations

from typing import Any, Dict, List

from .repository_base import BaseRepository
from ..path_utils import (
    APP_ROOT, infer_old_app_root_from_path, load_portable_state,
    repair_path_value, save_portable_state,
)


class PortabilityRepository(BaseRepository):
    def repair_portable_paths(self) -> Dict[str, Any]:
        state = load_portable_state()
        old_roots: List[str] = []
        saved_root = str(state.get("app_root") or "").strip()
        if saved_root and saved_root != str(APP_ROOT):
            old_roots.append(saved_root)

        with self.connect() as conn:
            channel_rows = conn.execute("SELECT id, token_file_path FROM channels").fetchall()
            video_rows = conn.execute("SELECT id, local_video_path, thumbnail_path FROM videos").fetchall()
            sheet_rows = conn.execute("SELECT id, json_folder_path FROM sheet_sources").fetchall()

            for row in list(channel_rows) + list(video_rows) + list(sheet_rows):
                for key in row.keys():
                    if key == "id":
                        continue
                    inferred = infer_old_app_root_from_path(row[key])
                    if inferred and inferred != str(APP_ROOT) and inferred.lower() not in {r.lower() for r in old_roots}:
                        old_roots.append(inferred)

            changed_channels = 0
            changed_videos = 0
            changed_sheets = 0
            for row in channel_rows:
                old = str(row["token_file_path"] or "")
                new = repair_path_value(old, old_roots=old_roots)
                if new and new != old:
                    conn.execute("UPDATE channels SET token_file_path=? WHERE id=?", (new, row["id"]))
                    changed_channels += 1
            for row in video_rows:
                updates: Dict[str, str] = {}
                for column in ("local_video_path", "thumbnail_path"):
                    old = str(row[column] or "")
                    new = repair_path_value(old, old_roots=old_roots)
                    if new != old:
                        updates[column] = new
                if updates:
                    assignments = ", ".join(f"{col}=?" for col in updates)
                    conn.execute(
                        f"UPDATE videos SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        [*updates.values(), row["id"]],
                    )
                    changed_videos += 1
            for row in sheet_rows:
                old = str(row["json_folder_path"] or "")
                new = repair_path_value(old, old_roots=old_roots)
                if new and new != old:
                    conn.execute("UPDATE sheet_sources SET json_folder_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new, row["id"]))
                    changed_sheets += 1
            if changed_channels or changed_videos or changed_sheets or old_roots:
                conn.execute(
                    "INSERT INTO upload_logs(video_id, level, message, progress) VALUES (?, ?, ?, ?)",
                    (None, "INFO", f"Portable path repair checked. Channels: {changed_channels}, queue paths: {changed_videos}, sheets: {changed_sheets}.", None),
                )
        save_portable_state({"last_repair_old_roots": old_roots})
        return {"old_roots": old_roots, "changed_channels": changed_channels, "changed_videos": changed_videos, "changed_sheets": changed_sheets}
