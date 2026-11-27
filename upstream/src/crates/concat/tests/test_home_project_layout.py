#!/usr/bin/env python3
"""Issue #67: Home Recent Project cards stay bounded and safe (source contract).

Actual Slint compilation is covered by tools/slint-source-check in the matching
source-only GitHub workflow. This does not pretend to be a Windows screenshot test.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOME = (ROOT / "concat/ui/start.slint").read_text(encoding="utf-8")
MODAL = (ROOT / "concat/ui/primitives/modal.slint").read_text(encoding="utf-8")
PANE = (ROOT / "concat/src/panes/start.rs").read_text(encoding="utf-8")

CARD = HOME.split("component PosterCard inherits Pressable {", 1)[1].split(
    "component ProjectsGrid inherits Rectangle {", 1
)[0]
ROW = HOME.split("component ProjectRow inherits Pressable {", 1)[1].split(
    "component SearchField inherits Rectangle {", 1
)[0]
MENU = HOME.split("component RecentMenuRow inherits Rectangle {", 1)[1].split(
    "component PosterAction inherits Rectangle {", 1
)[0]
DELETE_BUTTON = CARD.split("// This is a child hit target", 1)[1]


class HomeProjectLayoutRegression(unittest.TestCase):
    def test_wide_delete_has_an_explicit_compact_hitbox(self):
        # The dialog Button's min/max-height does not supply an absolute
        # child's geometry: without height Slint stretched it vertically.
        self.assertRegex(
            DELETE_BUTTON,
            r"Button\s*\{\s*x: root.width - 94px;\s*"
            r"y: poster.y \+ poster.height \+ 63px;\s*"
            r"width: 82px;\s*height: 28px;",
        )
        self.assertIn('label: "Delete";', DELETE_BUTTON)
        self.assertIn("clicked => { root.delete-requested(); }", DELETE_BUTTON)
        self.assertIn("min-height: root.large ? 34px : 28px;", MODAL)
        self.assertIn("max-height: root.large ? 34px : 28px;", MODAL)

        # Card and action use the same poster geometry, so the action has
        # a real 9px bottom margin, 12px right margin and 9px text gap.
        self.assertIn(
            "property <length> card-h: (root.card-w - 12px) * 9 / 16 + 106px;",
            HOME,
        )
        self.assertIn("width: root.width - 2 * root.inset - 103px;", CARD)
        self.assertIn("property <length> inset: 6px;", CARD)
        self.assertIn("height: self.width * 9 / 16;", CARD)

    def test_multiple_window_widths_and_dpi_keep_action_inside_card(self):
        # Exercise the same logical-pixel geometry used by WideHome/ProjectsGrid.
        # Physical width/dpi below 680px uses NarrowHome instead of a grid.
        for physical_width in (800, 1024, 1280, 1600, 1920, 2560):
            for dpi in (1.0, 1.25, 1.5):
                logical_width = physical_width / dpi
                if logical_width < 680:
                    continue
                pad = max(24, min(64, logical_width / 18))
                content_w = min(logical_width - 2 * pad, 1360)
                aside = content_w >= 1100
                aside_w = 360 if content_w >= 1280 else 330
                main_w = content_w - aside_w - 40 if aside else content_w
                cols = max(1, int((main_w + 16) // (220 + 16)))
                card_w = (main_w - (cols - 1) * 16) / cols
                poster_h = (card_w - 12) * 9 / 16
                card_h = poster_h + 106
                action_x, action_y = card_w - 94, poster_h + 6 + 63
                with self.subTest(physical_width=physical_width, dpi=dpi):
                    self.assertGreaterEqual(card_w, 220)
                    self.assertGreaterEqual(action_x, 0)
                    self.assertLessEqual(action_x + 82, card_w - 12 + 0.001)
                    self.assertLessEqual(action_y + 28, card_h - 9 + 0.001)
                    self.assertLessEqual(12 + (card_w - 12 - 103), action_x - 9 + 0.001)

    def test_missing_folder_disables_delete_without_remove(self):
        self.assertIn("enabled: !root.project.missing;", DELETE_BUTTON)
        self.assertIn("enabled: !root.project.missing;", ROW)
        self.assertIn("enabled: !root.project.missing;", CARD.split("menu := PopupWindow", 1)[1])
        self.assertIn("in property <bool> enabled: true;", MENU)
        self.assertIn("enabled: root.enabled;", MENU)
        self.assertNotIn("root.forgotten();", CARD)
        self.assertNotIn("root.forgotten();", ROW)
        # Stale model data is also rejected again at the backend boundary.
        self.assertIn("concat_host::project_delete::prepare(", PANE)
        self.assertIn("Err(error) => studio.notify(&error, true)", PANE)

    def test_project_open_and_recycle_remain_distinct_actions(self):
        self.assertIn("clicked => { root.opened(); }", CARD)
        self.assertNotIn("root.opened();", DELETE_BUTTON)
        self.assertIn("clicked => { root.delete-requested(); }", DELETE_BUTTON)
        self.assertNotIn("callback forgotten();", CARD)
        self.assertNotIn("callback forgotten();", ROW)
        self.assertIn("callback delete-requested();", CARD)
        self.assertIn("callback delete-requested();", ROW)
        self.assertIn('label: "Delete Project";', CARD)
        self.assertIn("menu.close();", CARD)
        self.assertIn("root.delete-requested();", ROW)

    def test_completed_home_launchers_remain_untouched(self):
        wide = HOME.split("component WideHome inherits Rectangle {", 1)[1].split(
            "component NarrowHome inherits Rectangle {", 1
        )[0]
        narrow = HOME.split("component NarrowHome inherits Rectangle {", 1)[1].split(
            "export component StartScreen inherits Rectangle {", 1
        )[0]
        for page in (wide, narrow):
            self.assertIn('label: "C Builder";', page)
            self.assertNotIn('I18n.t("cloudGpu.gpuQueue")', page)


if __name__ == "__main__":
    unittest.main()
