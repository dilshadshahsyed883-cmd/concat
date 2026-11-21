// SPDX-License-Identifier: AGPL-3.0-or-later
//! A caption look auditioned on the monitor, outside document, echo and undo state.
use super::Studio;
use crate::ui::{App, CaptionPresetPreview, CaptionPreviewCard};
use concat_core::frame::Frame;
use concat_export::ExportClip;
use concat_project::caption_preview::{card_sample, stage_actual, stage_sample};
use concat_project::model::{Clip, ClipKind, CustomFont, Project, TextStyle, Track};
use slint::{ComponentHandle, Model, ModelRc, VecModel};
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
    pub(crate) style_override: Option<TextStyle>,
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
        self.style_override = None;
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
    pub(crate) fn applied_caption_id(&self) -> Option<&str> {
        self.project().extra.get("captionPreset")?
            .get("presetId")?.as_str()
    }

    /// Staged changes beat saved changes; otherwise the original library look.
    pub(crate) fn caption_effective_style(&self, id: &str) -> Option<TextStyle> {
        if self.caption_preview.preset.as_deref() == Some(id) {
            if let Some(staged) = self.caption_preview.style_override.as_ref() {
                return Some(staged.clone());
            }
        }
        self.project().extra.get("captionPreset")
            .filter(|v| v.get("presetId").and_then(|x| x.as_str()) == Some(id))
            .and_then(|v| v.get("style"))
            .and_then(|v| serde_json::from_value::<TextStyle>(v.clone()).ok())
            .or_else(|| self.text_presets.iter()
                .find(|p| p.id == id).map(|p| p.style.clone()))
    }

    /// The inspector controls only modify the audition until Apply.
    fn edit_caption_preview_style(&mut self, change: impl FnOnce(&mut TextStyle)) {
        let Some(id) = self.caption_preview.preset.clone()
            .or_else(|| self.applied_caption_id().map(str::to_owned)) else { return; };
        let Some(mut style) = self.caption_effective_style(&id) else { return; };
        change(&mut style);
        self.caption_preview.preset = Some(id);
        self.caption_preview.style_override = Some(style);
        self.request_preview();
    }

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
            let clips = concat_project::caption_preview::caption_candidates(
                self.timeline(),
                Some(&selected),
            );
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
        let style = self.caption_effective_style(id)?;
        let at = f64::from(self.preview_time());
        let source = self.caption_at_preview_time(at);
        let staged = source
            .as_ref()
            .map(|c| stage_actual(c, &style, id))
            .unwrap_or_else(|| stage_sample(&style, id, 0.0, preset.offset_y));
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
        let applied = self.applied_caption_id().unwrap_or_default();
        let chosen = self.caption_preview.preset.as_deref().unwrap_or(applied);
        ui.set_chosen(chosen.into());
        ui.set_applied(applied.into());
        ui.set_previewing(self.caption_preview.preset.is_some());
        let rendered = self.caption_preview.rendered.borrow();
        let status = if self.caption_preview.preset.is_some() {
            if rendered.message.is_empty() { "Sample preview — project unchanged" }
            else { rendered.message.as_str() }
        } else if !applied.is_empty() {
            if self.project().extra.contains_key("gpuCaptionRequest") {
                "Preset Applied — captions will be generated on GPU"
            } else {
                "Preset Applied — captions will be generated on GPU once narration is added"
            }
        } else { "" };
        ui.set_status(status.into());
        ui.set_sample(self.caption_preview.preset.is_some() && rendered.sample);
        ui.set_demo_playing(self.caption_preview.demo_playing);
        if let Some(style) = self.caption_effective_style(chosen) {
            let index = match style.font_family.as_str() {
                "Inter" => if style.font_weight >= 900.0 { 2 } else { 3 },
                "Montserrat" => 4,
                "Archivo Black" => 5,
                "Anton" => 6,
                _ => if style.font_weight >= 700.0 { 1 } else { 0 },
            };
            ui.set_font_index(index);
            ui.set_text_hex(style.color.into());
            ui.set_background_hex(if style.background.is_empty() {
                "#000000".into()
            } else {
                style.background.into()
            });
        }
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
        // A project-level preset is legal with zero generated caption segments.
        ui.set_can_apply_all(true);
        if !self.caption_preview.browsing {
            return;
        }
        // Keep the original rendered-card cache intact: filtering changes only
        // the visible catalogue and its aligned star states, never thumbnails,
        // staged caption style, selection, or project data.
        let show_cards = |model: &ModelRc<CaptionPreviewCard>| {
            let mut shown = Vec::new();
            let mut stars = Vec::new();
            for index in 0..model.row_count() {
                if let Some(card) = model.row_data(index) {
                    let starred = self.prefs.favourites.iter()
                        .any(|id| id == card.id.as_str());
                    if !ui.get_favourites_only() || starred {
                        shown.push(card);
                        stars.push(starred);
                    }
                }
            }
            ui.set_favourites(ModelRc::new(VecModel::from(stars)));
            ui.set_cards(ModelRc::new(VecModel::from(shown)));
        };
        let signature = self
            .text_presets
            .iter()
            .filter(|p| p.id.starts_with("cuda-caption."))
            .map(|p| format!("{}|{:?}", p.id, p.style))
            .collect::<Vec<_>>()
            .join(";");
        if let Some((old, model)) = self.caption_preview.card_model.borrow().as_ref() {
            if old == &signature {
                show_cards(model);
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
        show_cards(&model);
        *self.caption_preview.card_model.borrow_mut() = Some((signature, model));
    }
}

/// Install isolated callbacks; the root window calls this once during setup.
pub(crate) fn install(app: &App) {
    let ui = app.global::<CaptionPresetPreview>();
    ui.on_toggle_favourite(|id| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                if !studio.text_presets.iter()
                    .any(|preset| preset.id == id.as_str()
                        && preset.id.starts_with("cuda-caption.")) {
                    return;
                }
                let starred = studio.prefs.favourites.iter()
                    .any(|entry| entry == id.as_str());
                studio.library_favourite(id.as_str(), !starred);
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_filter_favourites(|only| {
        crate::host::Shell::with(|shell, app| {
            app.global::<CaptionPresetPreview>().set_favourites_only(only);
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
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
                    studio.caption_preview.style_override = None;
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
    ui.on_style_font(|index| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                studio.edit_caption_preview_style(|style| {
                    let (family, weight) = match index {
                        0 => ("Hanken Grotesk", 600.0),
                        1 => ("Hanken Grotesk", 700.0),
                        2 => ("Inter", 900.0),
                        3 => ("Inter", 700.0),
                        4 => ("Montserrat", 800.0),
                        5 => ("Archivo Black", 900.0),
                        _ => ("Anton", 400.0),
                    };
                    style.font_family = family.into();
                    style.font_weight = weight;
                });
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_style_color(|field, hex| {
        if hex.len() != 7 || !hex.starts_with('#')
            || !hex.as_bytes()[1..].iter().all(|b| b.is_ascii_hexdigit())
        {
            return;
        }
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                studio.edit_caption_preview_style(|style| {
                    if field.as_str() == "text" { style.color = hex.to_string(); }
                    else if field.as_str() == "background" { style.background = hex.to_string(); }
                });
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
    ui.on_apply(|all| {
        crate::host::Shell::with(|shell, app| {
            {
                let mut studio = shell.studio.borrow_mut();
                if let Some(id) = studio.caption_preview.preset.clone()
                    .or_else(|| studio.applied_caption_id().map(str::to_owned))
                {
                    studio.apply_caption_preset(&id, all);
                    if all && studio.applied_caption_id() == Some(id.as_str()) {
                        studio.caption_preview.clear();
                    }
                    studio.request_preview();
                }
            }
            shell.studio.borrow().publish(&app, &shell.models);
        })
    });
}
