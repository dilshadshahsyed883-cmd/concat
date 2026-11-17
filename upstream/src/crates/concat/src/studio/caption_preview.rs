// SPDX-License-Identifier: AGPL-3.0-or-later
//! A caption look auditioned on the monitor, outside document, echo and undo state.
use super::Studio;
use crate::ui::{App, CaptionPresetPreview, CaptionPreviewCard};
use concat_core::frame::Frame;
use concat_export::ExportClip;
use concat_project::caption_preview::{card_sample, stage_actual, stage_sample};
use concat_project::model::{Clip, ClipKind, CustomFont, Project, Track};
use slint::{ComponentHandle, ModelRc, VecModel};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::sync::Arc;

#[derive(Default)]
struct CaptionIndex {
    revision: u64,
    timeline: String,
    project: String,
    selection: String,
    clips: Vec<Arc<Clip>>,
    latest_ends: Vec<f64>,
}
impl CaptionIndex {
    fn active(&self, at: f64) -> Option<Arc<Clip>> {
        let mut end = self.clips.partition_point(|c| c.start <= at);
        while end > 0 {
            end -= 1;
            if self.latest_ends[end] <= at {
                break;
            }
            let clip = &self.clips[end];
            if at < clip.start + clip.duration {
                return Some(Arc::clone(clip));
            }
        }
        None
    }
}
#[derive(Default)]
struct RenderedPreview {
    key: String,
    suppressed: HashSet<String>,
    source_window: Option<(usize, f64, f64)>,
    tiles: Vec<(ExportClip, Arc<Frame>)>,
    message: String,
    sample: bool,
}

/// The pending look and small caches. No saved project objects live here.
#[derive(Default)]
pub(crate) struct CaptionPreview {
    pub(crate) preset: Option<String>,
    browsing: bool,
    demo_timer: slint::Timer,
    demo_playing: bool,
    demo_override: bool,
    demo_time: f64,
    demo_anchor: f64,
    card_model: RefCell<Option<(String, ModelRc<CaptionPreviewCard>)>>,
    index: RefCell<CaptionIndex>,
    rendered: RefCell<RenderedPreview>,
    cards: RefCell<HashMap<String, slint::Image>>,
}
impl CaptionPreview {
    /// Drop staged styling without changing a document or its history.
    pub(crate) fn clear(&mut self) {
        self.preset = None;
        self.demo_timer.stop();
        self.demo_playing = false;
        self.demo_override = false;
        self.demo_time = 0.0;
        *self.rendered.get_mut() = RenderedPreview::default();
    }
}

fn one_caption(project: &Project, clip: Clip, font: Option<&std::path::Path>) -> Project {
    let mut staged = Project::with_video(project.active().video);
    staged.fonts = project.fonts.clone();
    if let Some(path) = font {
        if let Some(style) = clip.text.as_ref() {
            staged.fonts.push(CustomFont {
                family: style.font_family.trim_matches('"').to_owned(),
                path: path.to_string_lossy().into_owned(),
            });
        }
    }
    let timeline = staged.active_mut();
    timeline.tracks = project.active().tracks.clone();
    if !timeline.tracks.iter().any(|t| t.id == clip.track_id) {
        timeline.tracks.push(Track {
            id: clip.track_id.clone(),
            visible: true,
            muted: false,
            extra: Default::default(),
        });
    }
    timeline.clips = vec![Arc::new(clip)];
    staged
}

impl Studio {
    /// Build an interval index only when the document, timeline or caption selection changes.
    fn caption_at_preview_time(&self, at: f64) -> Option<Arc<Clip>> {
        let selected = self.sole_selection().unwrap_or_default();
        let mut index = self.caption_preview.index.borrow_mut();
        let project_path = self
            .session
            .as_ref()
            .map(|s| s.path().to_owned())
            .unwrap_or_default();
        if index.project != project_path
            || index.revision != self.revision
            || index.timeline != self.timeline().id
            || index.selection != selected
        {
            let lane =
                concat_project::caption_preview::caption_lane(self.timeline(), Some(&selected));
            let mut clips: Vec<_> = self
                .timeline()
                .clips
                .iter()
                .filter(|c| {
                    c.kind == ClipKind::Text
                        && lane.as_deref() == Some(c.track_id.as_str())
                        && self
                            .timeline()
                            .track(&c.track_id)
                            .is_some_and(|t| t.visible)
                })
                .cloned()
                .collect();
            clips.sort_by(|a, b| a.start.total_cmp(&b.start));
            let mut end = 0.0_f64;
            let latest_ends = clips
                .iter()
                .map(|c| {
                    end = end.max(c.start + c.duration);
                    end
                })
                .collect();
            *index = CaptionIndex {
                revision: self.revision,
                timeline: self.timeline().id.clone(),
                project: project_path,
                selection: selected,
                clips,
                latest_ends,
            };
        }
        index.active(at)
    }

    /// Add a native Titles overlay to this monitor frame only. All video/audio clips survive unchanged.
    pub(crate) fn caption_preview_overlay(
        &self,
        base: &[ExportClip],
        width: u32,
        height: u32,
    ) -> Option<Vec<ExportClip>> {
        let id = self.caption_preview.preset.as_deref()?;
        let preset = self.text_presets.iter().find(|p| p.id == id)?;
        let at = f64::from(self.preview_time());
        let source = self.caption_at_preview_time(at);
        let staged = source
            .as_ref()
            .map(|c| stage_actual(c, &preset.style, id))
            .unwrap_or_else(|| stage_sample(&preset.style, id, 0.0, preset.offset_y));
        let (shown_w, shown_h) =
            crate::panes::monitor::MonitorPane::frame_size(self.quality_of(), (width, height));
        let key = format!(
            "{id}|{}|{width}|{height}|{shown_w}|{shown_h}|{:?}",
            self.revision, staged.clip
        );
        let mut rendered = self.caption_preview.rendered.borrow_mut();
        if rendered.key != key {
            let mut suppressed = HashSet::new();
            let source_window = source.as_ref().and_then(|c| {
                self.timeline()
                    .tracks
                    .iter()
                    .position(|t| t.id == c.track_id)
                    .map(|rank| (rank, c.start, c.start + c.duration))
            });
            if let Some(original) = source.as_ref() {
                let unchanged = one_caption(self.project(), (**original).clone(), None);
                for title in self.host.titles.clips(&unchanged, width, height) {
                    suppressed.insert(title.clip.path);
                }
                for title in self.host.titles.clips_live(&unchanged, shown_w, shown_h) {
                    suppressed.insert(title.clip.path);
                }
            }
            let project = one_caption(self.project(), staged.clip.clone(), preset.font.as_deref());
            let mut tiles = Vec::new();
            for title in self.host.titles.clips_live(&project, shown_w, shown_h) {
                if let Some(frame) = title.frame {
                    tiles.push((title.clip, frame));
                }
            }
            let message = staged.message.unwrap_or_else(|| {
                if staged.exact_words {
                    "Previewing actual caption text and speech word timing; project unchanged"
                        .into()
                } else {
                    "Previewing actual caption text; word timing unavailable; project unchanged"
                        .into()
                }
            });
            *rendered = RenderedPreview {
                key,
                suppressed,
                source_window,
                tiles,
                message,
                sample: staged.sample,
            };
        }
        let mut clips: Vec<_> = base
            .iter()
            .filter(|c| {
                !(rendered.suppressed.contains(&c.path)
                    && rendered.source_window.is_some_and(|(rank, start, end)| {
                        c.track == rank && c.start < end && c.start + c.duration > start
                    }))
            })
            .cloned()
            .collect();
        let top = clips.iter().map(|c| c.track).max().map_or(0, |t| t + 1);
        let sample_time = if self.caption_preview.demo_override
            && (at - self.caption_preview.demo_anchor).abs() < 1e-5
        {
            self.caption_preview.demo_time
        } else {
            at.rem_euclid(2.4)
        };
        for (clip, frame) in &rendered.tiles {
            if rendered.sample
                && !(clip.start <= sample_time && sample_time < clip.start + clip.duration)
            {
                continue;
            }
            self.host
                .monitor
                .hold_still(std::path::Path::new(&clip.path), Arc::clone(frame));
            let mut clip = clip.clone();
            if rendered.sample {
                clip.track = top;
                // Select one already-painted demo state and place it at the real monitor time.
                // The independent demo clock never seeks or edits the project transport.
                clip.start = 0.0;
                clip.duration = f64::from(self.duration()).max(at + 1.0).max(1.0);
            }
            clips.push(clip);
        }
        Some(clips)
    }

    /// Publish card pixels drawn by the same native title painter used by preview/export.
    pub(crate) fn publish_caption_preview(&self, app: &App) {
        let ui = app.global::<CaptionPresetPreview>();
        ui.set_chosen(
            self.caption_preview
                .preset
                .as_deref()
                .unwrap_or_default()
                .into(),
        );
        let rendered = self.caption_preview.rendered.borrow();
        ui.set_status(rendered.message.as_str().into());
        ui.set_sample(rendered.sample);
        ui.set_demo_playing(self.caption_preview.demo_playing);
        ui.set_can_apply_selected(self.selection.iter().any(|id| {
            self.clip(id).is_some_and(|c| {
                c.kind == ClipKind::Text
                    && !c.extra.contains_key("canvasOwner")
                    && self
                        .timeline()
                        .track(&c.track_id)
                        .is_some_and(|t| t.visible)
                    && !self.locked(&c.track_id)
            })
        }));
        let caption_lane = concat_project::caption_preview::caption_lane(
            self.timeline(),
            self.sole_selection().as_deref(),
        );
        ui.set_can_apply_all(self.timeline().clips.iter().any(|c| {
            c.kind == ClipKind::Text
                && caption_lane.as_deref() == Some(c.track_id.as_str())
                && !c.extra.contains_key("canvasOwner")
                && self
                    .timeline()
                    .track(&c.track_id)
                    .is_some_and(|t| t.visible)
                && !self.locked(&c.track_id)
        }));
        if !self.caption_preview.browsing {
            return;
        }
        let signature = self
            .text_presets
            .iter()
            .filter(|p| p.id.starts_with("cuda-caption."))
            .map(|p| format!("{}|{:?}", p.id, p.style))
            .collect::<Vec<_>>()
            .join(";");
        if let Some((old, model)) = self.caption_preview.card_model.borrow().as_ref() {
            if old == &signature {
                ui.set_cards(model.clone());
                return;
            }
        }
        let mut cached = self.caption_preview.cards.borrow_mut();
        let mut cards = Vec::new();
        for preset in self
            .text_presets
            .iter()
            .filter(|p| p.id.starts_with("cuda-caption."))
        {
            let key = format!("{}|{:?}", preset.id, preset.style);
            let thumbnail = cached
                .entry(key)
                .or_insert_with(|| {
                    let mut clip = Clip::blank(
                        "__caption_card",
                        "__caption_preview",
                        ClipKind::Text,
                        "Hello",
                        0.0,
                        1.0,
                    );
                    clip.text = Some(card_sample(&preset.style));
                    let project = one_caption(self.project(), clip, preset.font.as_deref());
                    self.host
                        .titles
                        .clips_live(&project, 320, 180)
                        .into_iter()
                        .find_map(|t| t.frame)
                        .map(|frame| crate::host::image_of(&frame))
                        .unwrap_or_default()
                })
                .clone();
            cards.push(CaptionPreviewCard {
                id: preset.id.as_str().into(),
                name: preset.name.as_str().into(),
                thumbnail,
            });
        }
        let model = ModelRc::from(Rc::new(VecModel::from(cards)));
        ui.set_cards(model.clone());
        *self.caption_preview.card_model.borrow_mut() = Some((signature, model));
    }
}

/// Install isolated callbacks; the root window calls this once during setup.
pub(crate) fn install(app: &App) {
    let ui = app.global::<CaptionPresetPreview>();
    ui.on_stage(|id| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                if !studio
                    .text_presets
                    .iter()
                    .any(|p| p.id == id.as_str() && p.id.starts_with("cuda-caption."))
                {
                    return;
                }
                studio.flush_commit();
                studio.close_source();
                if studio.caption_preview.preset.as_deref() == Some(id.as_str()) {
                    studio.caption_preview.clear();
                } else {
                    studio.caption_preview.preset = Some(id.to_string());
                }
                studio.request_preview();
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_cancel(|| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                studio.caption_preview.clear();
                studio.request_preview();
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_scope(|active| {
        // Scope changes can be emitted by a Slint property during publish;
        // defer the state borrow to avoid re-entering the current publish borrow.
        let _ = slint::invoke_from_event_loop(move || {
            crate::host::Shell::with(|shell, app| {
                {
                    let mut studio = shell.studio.borrow_mut();
                    studio.caption_preview.browsing = active;
                    if !active && studio.caption_preview.preset.is_some() {
                        studio.caption_preview.clear();
                        studio.request_preview();
                    }
                }
                shell.studio.borrow().publish(&app, &shell.models);
            })
        });
    });
    ui.on_demo_toggle(|| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                if studio.caption_preview.preset.is_none()
                    || !studio.caption_preview.rendered.borrow().sample
                {
                    return;
                }
                studio.caption_preview.demo_playing = !studio.caption_preview.demo_playing;
                if studio.caption_preview.demo_playing {
                    studio.caption_preview.demo_override = true;
                    studio.caption_preview.demo_anchor = f64::from(studio.preview_time());
                    studio.caption_preview.demo_timer.start(
                        slint::TimerMode::Repeated,
                        std::time::Duration::from_millis(80),
                        || {
                            crate::host::Shell::with(|shell, app| {
                                {
                                    let mut studio = shell.studio.borrow_mut();
                                    if !studio.caption_preview.rendered.borrow().sample
                                        || studio.caption_preview.preset.is_none()
                                        || (f64::from(studio.preview_time())
                                            - studio.caption_preview.demo_anchor)
                                            .abs()
                                            > 1e-5
                                    {
                                        studio.caption_preview.demo_timer.stop();
                                        studio.caption_preview.demo_playing = false;
                                    } else {
                                        studio.caption_preview.demo_time =
                                            (studio.caption_preview.demo_time + 0.08)
                                                .rem_euclid(2.4);
                                        studio.request_preview();
                                    }
                                }
                                shell.studio.borrow().publish(&app, &shell.models);
                            });
                        },
                    );
                } else {
                    studio.caption_preview.demo_timer.stop();
                }
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_apply(|all| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                if let Some(id) = studio.caption_preview.preset.clone() {
                    // Explicit Apply uses the existing atomic batch and scope handling.
                    studio.caption_preview.clear();
                    studio.apply_caption_preset(&id, all);
                    studio.request_preview();
                }
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
}
