# C Builder Home workflow

The Home header, output actions and three tab buttons remain fixed outside the scroll viewport; only the selected tab's content scrolls. The media folder is remembered and refreshed on opening. Narration naming, duration, manual overrides, Use Audio Name and collision-safe project/ZIP/MP4 naming are retained.

Timeline Planning has two opening/ending sliders in one row, four duration fields in one row and four sequence fields in one row. Apply validates and saves one configuration in the application config directory (c-builder-planning.json); Reroll persists the new seed. Named profiles, import/export, advanced JSON and storyboard pinning are removed from C Builder. The internal shared planner remains, using a linear distribution between endpoints. The overall video duration target and keyword shares remain in Media Selection; keyword shares are not global planning preferences.

Independent 100% image/video keyword budgets redistribute on deselection; unchecked rows are zero and disabled. Actual video duration and target constraints are reported.

Generate verifies a native project, then moves unique consumed visual sources into used/<project name>/, updates media paths and refreshes inventory. Narration and unused sources remain. Journal recovery, locks and rollback protect the move; Cancel, Retry used-media and Open in Editor remain. No GPU change is required. Windows layout and interaction require desktop acceptance.
