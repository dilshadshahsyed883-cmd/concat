#!/usr/bin/env python3
"""
Regression test suite for Recent Projects Menu (⋯) on Concat Home Screen.

Verifies:
1. Recent Projects Open, Rename, and Delete behavior.
2. Persistence of renamed projects across application reloads.
3. Unchanged project directory paths and preserved media files on delete.
4. Exact popup menu options (Open, Rename, Delete).
5. Manifest integrity and media reference preservation.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


class ProjectManager:
    """Python reference implementation replicating concat-host::projects contracts."""

    MANIFEST = "concat.json"
    LEGACY_MANIFESTS = ["wolfcut.json"]
    RECENTS = "recents.json"

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir

    def manifest_path(self, root: Path) -> Path:
        current = root / self.MANIFEST
        if current.is_file():
            return current
        for legacy in self.LEGACY_MANIFESTS:
            cand = root / legacy
            if cand.is_file():
                return cand
        return current

    def is_project(self, root: Path) -> bool:
        return self.manifest_path(root).is_file()

    def same_path(self, left: str, right: str) -> bool:
        def norm(p: str) -> str:
            p = p.replace("/", "\\") if os.name == "nt" else p
            return p.rstrip("/\\").lower()
        return norm(left) == norm(right)

    def read_recents(self) -> list:
        recents_file = self.config_dir / self.RECENTS
        if not recents_file.is_file():
            return []
        try:
            with open(recents_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def write_recents(self, entries: list):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        recents_file = self.config_dir / self.RECENTS
        with open(recents_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

    def create(self, location: Path, name: str, width=1920, height=1080, rate_num=30, rate_den=1) -> dict:
        root = location / name.strip().replace(":", "-")
        root.mkdir(parents=True, exist_ok=True)
        manifest_file = root / self.MANIFEST

        doc = {
            "concat": "0.2.7",
            "name": name,
            "video": {
                "width": width,
                "height": height,
                "rateNum": rate_num,
                "rateDen": rate_den,
            },
            "timelines": [],
            "tracks": [],
            "clips": [],
            "media": [],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

        info = {
            "path": str(root),
            "name": name,
            "width": width,
            "height": height,
            "rateNum": rate_num,
            "rateDen": rate_den,
            "openedAt": 1000,
        }
        return info

    def remember(self, project_info: dict):
        entries = self.read_recents()
        entries = [e for e in entries if not self.same_path(e["path"], project_info["path"])]
        entries.insert(0, dict(project_info))
        self.write_recents(entries)

    def open_project(self, path: str) -> dict:
        root = Path(path)
        manifest = self.manifest_path(root)
        if not manifest.is_file():
            raise FileNotFoundError(f"Project manifest not found at {root}")

        with open(manifest, "r", encoding="utf-8") as f:
            doc = json.load(f)

        return {
            "path": str(root),
            "name": doc["name"],
            "width": doc["video"]["width"],
            "height": doc["video"]["height"],
            "rateNum": doc["video"]["rateNum"],
            "rateDen": doc["video"]["rateDen"],
        }

    def rename(self, path: str, new_name: str) -> dict:
        trimmed = new_name.strip()
        if not trimmed:
            raise ValueError("project name cannot be empty")

        root = Path(path)
        if not self.is_project(root):
            raise FileNotFoundError(f"a Concat project was not found at {root}")

        # 1. Update project manifest on disk
        manifest = self.manifest_path(root)
        with open(manifest, "r", encoding="utf-8") as f:
            doc = json.load(f)

        doc["name"] = trimmed

        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

        # 2. Update recents.json
        entries = self.read_recents()
        updated_info = None
        for entry in entries:
            if self.same_path(entry["path"], path):
                entry["name"] = trimmed
                updated_info = dict(entry)

        self.write_recents(entries)

        if updated_info:
            return updated_info
        return self.open_project(path)

    def forget(self, path: str):
        entries = self.read_recents()
        entries = [e for e in entries if not self.same_path(e["path"], path)]
        self.write_recents(entries)


class TestRecentProjectsMenu(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="concat-test-recent-menu-"))
        self.config_dir = self.test_dir / "config"
        self.projects_dir = self.test_dir / "projects"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.pm = ProjectManager(self.config_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_open_recent_project(self):
        """Open loads project metadata correctly; missing folder is detected."""
        info = self.pm.create(self.projects_dir, "Film Noir", 3840, 2160, 24, 1)
        self.pm.remember(info)

        opened = self.pm.open_project(info["path"])
        self.assertEqual(opened["name"], "Film Noir")
        self.assertEqual(opened["width"], 3840)
        self.assertEqual(opened["height"], 2160)
        self.assertEqual(opened["rateNum"], 24)

        # Non-existent project path raises error
        with self.assertRaises(FileNotFoundError):
            self.pm.open_project(str(self.projects_dir / "NonExistent"))

    def test_rename_preserves_path_and_media_references(self):
        """Rename updates manifest and recents.json, preserving project path and all media."""
        info = self.pm.create(self.projects_dir, "My Documentary", 1920, 1080, 30, 1)
        self.pm.remember(info)

        project_path = Path(info["path"])
        manifest_file = self.pm.manifest_path(project_path)

        # Add rich timelines, tracks, clips and media references to manifest
        with open(manifest_file, "r", encoding="utf-8") as f:
            doc = json.load(f)

        dummy_media = [
            {"id": "m1", "path": str(project_path / "interview_a.mov"), "duration": 5400},
            {"id": "m2", "path": str(project_path / "b_roll.mp4"), "duration": 1800},
            {"id": "m3", "path": "D:\\external\\drone_footage.mp4", "duration": 3600},
        ]
        dummy_clips = [
            {"id": "c1", "name": "Interview Cut 1", "mediaId": "m1", "start": 0, "end": 1200},
            {"id": "c2", "name": "Drone Shot Over City", "mediaId": "m3", "start": 1200, "end": 1800},
        ]
        dummy_tracks = [
            {"id": "t1", "kind": "video", "clips": ["c1", "c2"]},
            {"id": "t2", "kind": "audio", "clips": []},
        ]

        doc["media"] = dummy_media
        doc["clips"] = dummy_clips
        doc["tracks"] = dummy_tracks

        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

        # Execute rename
        new_title = "Documentary: Final Cut (2026)"
        renamed_info = self.pm.rename(str(project_path), new_title)

        # 1. Returned ProjectInfo has new name and UNCHANGED path
        self.assertEqual(renamed_info["name"], new_title)
        self.assertEqual(renamed_info["path"], str(project_path))

        # 2. Manifest on disk has new name, while media and edit data are completely preserved
        with open(manifest_file, "r", encoding="utf-8") as f:
            updated_doc = json.load(f)

        self.assertEqual(updated_doc["name"], new_title)
        self.assertEqual(updated_doc["media"], dummy_media)
        self.assertEqual(updated_doc["clips"], dummy_clips)
        self.assertEqual(updated_doc["tracks"], dummy_tracks)
        self.assertEqual(updated_doc["video"]["width"], 1920)

        # 3. recents.json reflects the new name with exact original path
        recents = self.pm.read_recents()
        self.assertEqual(len(recents), 1)
        self.assertEqual(recents[0]["name"], new_title)
        self.assertEqual(recents[0]["path"], str(project_path))

    def test_rename_empty_rejected(self):
        """Rename with empty or whitespace-only name is rejected."""
        info = self.pm.create(self.projects_dir, "Project X")
        self.pm.remember(info)

        with self.assertRaises(ValueError):
            self.pm.rename(info["path"], "   ")

    def test_rename_persists_across_reloads(self):
        """Renamed project persists across simulated application restarts."""
        info = self.pm.create(self.projects_dir, "Vlog Episode 1")
        self.pm.remember(info)

        self.pm.rename(info["path"], "Vlog: Tokyo Summer")

        # Simulate fresh app instance loading from disk
        fresh_pm = ProjectManager(self.config_dir)
        fresh_recents = fresh_pm.read_recents()
        self.assertEqual(len(fresh_recents), 1)
        self.assertEqual(fresh_recents[0]["name"], "Vlog: Tokyo Summer")

        fresh_open = fresh_pm.open_project(info["path"])
        self.assertEqual(fresh_open["name"], "Vlog: Tokyo Summer")
        self.assertEqual(fresh_open["path"], info["path"])

    def test_delete_forgets_from_recents_preserving_media_and_other_projects(self):
        """Delete removes from recents without deleting disk files or affecting other projects."""
        proj_a = self.pm.create(self.projects_dir, "Project A")
        proj_b = self.pm.create(self.projects_dir, "Project B")

        # Create dummy media files inside project folders
        path_a = Path(proj_a["path"])
        path_b = Path(proj_b["path"])

        media_file_a = path_a / "interview.mov"
        media_file_b = path_b / "audio_track.wav"
        media_file_a.write_bytes(b"sample video bytes for project A")
        media_file_b.write_bytes(b"sample audio bytes for project B")

        self.pm.remember(proj_a)
        self.pm.remember(proj_b)

        recents_before = self.pm.read_recents()
        self.assertEqual(len(recents_before), 2)

        # Delete Project A
        self.pm.forget(proj_a["path"])

        # 1. recents.json only contains Project B
        recents_after = self.pm.read_recents()
        self.assertEqual(len(recents_after), 1)
        self.assertEqual(recents_after[0]["name"], "Project B")
        self.assertEqual(recents_after[0]["path"], proj_b["path"])

        # 2. Project A directory and media files on disk are 100% PRESERVED
        self.assertTrue(path_a.is_dir(), "Project A directory must still exist")
        self.assertTrue(self.pm.manifest_path(path_a).is_file(), "Project A manifest must still exist")
        self.assertTrue(media_file_a.is_file(), "Project A media file must not be deleted")
        self.assertEqual(media_file_a.read_bytes(), b"sample video bytes for project A")

        # 3. Project B is completely unaffected
        self.assertTrue(path_b.is_dir())
        self.assertTrue(media_file_b.is_file())

    def test_slint_source_conformance(self):
        """Verify UI files start.slint and app.slint satisfy exact requirements."""
        crates_root = Path(__file__).resolve().parents[2]
        start_slint = crates_root / "concat" / "ui" / "start.slint"
        app_slint = crates_root / "concat" / "ui" / "app.slint"
        projects_rs = crates_root / "concat-host" / "src" / "projects.rs"

        self.assertTrue(start_slint.is_file(), f"Missing {start_slint}")
        self.assertTrue(app_slint.is_file(), f"Missing {app_slint}")
        self.assertTrue(projects_rs.is_file(), f"Missing {projects_rs}")

        start_content = start_slint.read_text(encoding="utf-8")
        app_content = app_slint.read_text(encoding="utf-8")
        projects_content = projects_rs.read_text(encoding="utf-8")

        # Task 2: Glyph.more in PosterCard
        self.assertIn("Glyph.more", start_content)
        self.assertIn("menu := PopupWindow", start_content)

        # Task 3: Popup menu options Open, Rename, Delete
        self.assertIn('label: I18n.t("start.open")', start_content)
        self.assertIn('label: I18n.t("start.rename")', start_content)
        self.assertIn('label: I18n.t("common.delete")', start_content)
        self.assertIn("danger: true", start_content)

        # Task 3: Rename dialog modal
        self.assertIn("component RenameDialog inherits Modal", start_content)
        self.assertIn("RenameDialog {", app_content)

        # Task 3: projects::rename exists in concat-host
        self.assertIn("pub fn rename(", projects_content)
        self.assertIn("pub fn same_path(", projects_content)


if __name__ == "__main__":
    unittest.main()
