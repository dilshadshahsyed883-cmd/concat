# Caption preset preview (#58)

Caption cards stage a temporary look on the current project frame. The renderer uses the existing native `Titles` painter and existing exact-word render windows. Staged clips are not added to a project, the inspector echo, the undo stack, a worker queue, or an export.

- Cards show short, readable `Hello` samples rasterized from the preset's real font, fill, outline, shadow and backgrounds. The card enlarges typography proportionally and preserves its bounding box. Names wrap below the image. Card images and their model are cached so playback does not replace click targets.
- The actual caption under the playhead supplies text, placement and speech timings. An interval index is rebuilt only when the document, caption selection, timeline or project changes. Generated canvas headline lanes are excluded from implicit caption selection.
- Without a caption at the playhead, the monitor labels the overlay **Sample caption · demo timing · preview only**. Scrubbing drives the sample phase. An independent **Play Sample Demo / Pause Sample Demo** control also works on an empty or paused timeline without seeking the project transport.
- True word times remain half-open intervals, including unhighlighted speech gaps. An untimed actual caption receives a static phrase preview and an explicit limitation; animated presets cannot be applied without true word timing.
- **Apply to Selected Captions** and **Apply to All Captions** are explicit existing command batches. Preview and Apply share the same style function. Cancel, toggling the chosen card, leaving the caption shelf, or switching projects discards the audition and stops its demo timer.
- The audition never creates stage selection grips. Underlying footage selection and normal typography editing continue after Apply or Cancel.

Source verification: headless caption staging tests, native glyph/raster tests, and full Slint compilation. Windows interaction and visual comparison against a live NVIDIA export remain hardware verification; no such execution is claimed and no EXE, container, or release is produced.
