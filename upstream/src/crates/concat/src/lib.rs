// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors

//! Concat's editor window, in Slint.
//!
//! This file is the wiring: it starts the engine's services, builds the
//! window, and binds every callback the `.slint` tree exposes to the state
//! in [`studio`]. The state reads the engine's project and writes commands
//! to it; nothing here decides what an edit means.
//!
//! A library so that every entry point is a few lines: `main.rs` on the
//! desktop and on iOS, and the `concat-android` activity on Android. Each
//! one calls [`run`].

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use slint::{Model, ModelRc, SharedString, VecModel};
use concat_project::Command;

// `DataTransfer` is what a drag carries. Slint keeps the platform's drag
// object opaque and leaves building and reading one to the host language.
use slint::private_unstable_api::re_exports::DataTransfer;

// Everything the .slint tree exports, in a module of its own: the workspace
// lints every public item for documentation, and the generated accessors are
// thousands of public items nobody documents. The allow covers them and
// nothing in this file.
#[allow(missing_docs)]
mod ui {
    slint::include_modules!();
}

mod batch_volume;
mod chips;
mod c_builder;
mod c_builder_used;
mod dock;
mod fonts;
mod format;
mod gpu;
#[cfg(windows)]
mod cloud_gpu;
mod grading;
mod host;
mod i18n;
/// The iPhone's pickers and its way to Photos; see the module.
#[cfg(target_os = "ios")]
mod ios;
mod keymap;
mod meters;
mod platform;
/// What a phone's own crate installs before the window runs: the way to
/// the system's file picker, and where a finished export is put so the
/// phone shows it. See `platform::pick_files_async` and
/// `platform::publish_export`.
#[cfg(any(target_os = "android", target_os = "ios"))]
pub use platform::{
    ExportPublisher, FilePicker, PickKinds, install_export_publisher, install_file_picker,
};
#[cfg(target_os = "android")]
pub use platform::{LinkOpener, install_link_opener};
#[cfg(any(target_os = "android", target_os = "ios"))]
pub use platform::{MicrophoneAccess, install_microphone_access};
mod panes;
mod prefs;
mod presets;
mod source;
mod studio;
mod sysinfo;
/// Native Wayland file drops, which winit does not report; see the module.
#[cfg(target_os = "linux")]
mod wayland_drop;
/// Elsewhere the listener is a name that starts nothing, so platform.rs
/// reads the same on every desktop.
#[cfg(not(any(target_os = "linux", target_os = "android")))]
mod wayland_drop {
    /// Nothing to listen to: no desktop but Linux draws through Wayland.
    pub struct Listener;

    impl Listener {
        /// Never starts; see the Linux module for what it would do.
        ///
        /// # Safety
        ///
        /// Nothing is dereferenced; the contract is the Linux module's.
        pub unsafe fn start(
            _display: std::ptr::NonNull<std::ffi::c_void>,
            _surface: std::ptr::NonNull<std::ffi::c_void>,
            _sink: impl Fn(Vec<std::path::PathBuf>) + Send + 'static,
        ) -> Option<Listener> {
            None
        }
    }
}

use dock::{Dock, SEAT_MIN_GRAB, SEAT_MIN_H, SEAT_MIN_W};
use host::{Host, Shell};
use panes::Msg;
use panes::captions::CaptionsMsg;
use panes::export::ExportMsg;
use panes::media_bin::MediaMsg;
use panes::monitor::MonitorMsg;
use panes::project::ProjectMsg;
use panes::relink::RelinkMsg;
use panes::settings::SettingsMsg;
use panes::sounds::SoundsMsg;
use panes::speech::SpeechMsg;
use panes::start::StartMsg;
use panes::timeline::TimelineMsg;
use panes::voiceover::VoiceoverMsg;
use studio::{ASPECTS, Models, OUTPUTS, RATES, SIZES, Studio};
use ui::*;

fn canvas_picker_color(hex: &str) -> slint::Color {
    let digits = hex.trim_start_matches('#');
    let rgb = digits.get(..6).and_then(|s| u32::from_str_radix(s, 16).ok()).unwrap_or(0);
    slint::Color::from_rgb_u8((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8)
}
fn media_target_flags()->(bool,bool,bool) {
    let mut flags=(true,true,false);
    Shell::with(|_,app| {
        let targets=app.global::<Keyframes>();
        flags=(targets.get_animation_images(),targets.get_animation_videos(),targets.get_animation_scope()==1);
    });
    flags
}

fn canvas_publish(app: &App, config: &concat_project::canvas_preset::Config) {
    let panel = app.global::<CanvasPresets>();
    panel.set_profile(serde_json::to_string_pretty(config).unwrap_or_default().into());
    panel.set_selected(config.preset as i32);
    panel.set_foreground_x(config.foreground.x as f32);
    panel.set_foreground_y(config.foreground.y as f32);
    panel.set_foreground_width(config.foreground.width as f32);
    panel.set_foreground_height(config.foreground.height as f32);
    panel.set_border_width(config.border_width as f32);
    panel.set_radius(config.radius as f32);
    panel.set_blur(config.blur as f32);
    panel.set_tint_opacity(config.tint_opacity as f32);
    panel.set_shadow_opacity(config.shadow_opacity as f32);
    panel.set_shadow_offset(config.shadow_offset as f32);
    panel.set_mat_bottom(config.mat_bottom as f32);
    panel.set_rotation(config.rotation as f32);
    panel.set_background_mode(match config.background.as_str() {
        "blur" => 1, "color" => 2, "none" => 3, _ => 0,
    });
    panel.set_border_color(canvas_picker_color(&config.border_color));
    panel.set_background_color(canvas_picker_color(&config.background_color));
    panel.set_tint_color(canvas_picker_color(&config.background_tint));
    panel.set_shadow_color(canvas_picker_color(&config.shadow_color));
    panel.set_mat_color(canvas_picker_color(&config.mat_color));
}
fn canvas_modify(app: &App, edit: impl FnOnce(&mut concat_project::canvas_preset::Config)) {
    let panel = app.global::<CanvasPresets>();
    let Ok(mut config) = concat_project::canvas_preset::Config::from_json(&panel.get_profile()) else {
        return; // Keep invalid Advanced JSON drafts intact.
    };
    edit(&mut config);
    if config.validate().is_ok() { canvas_publish(app, &config); }
}

/// Opens this run's log file and makes it where the app writes things down.
///
/// Each entry point calls this before [`run`], because each knows where its
/// console is: a desktop has a standard error, a phone has logcat, and that
/// is what arrives here as `extra`. Everything after it - the engine's
/// warnings, a panic, the lines below - is written down in
/// `<app data>/logs/` as well as said out loud. See `concat_host::logs`.
pub fn open_logging(extra: Option<Box<dyn log::Log>>) {
    concat_host::crash::catch();
    concat_host::logs::catch_panics();
    let opened = match concat_host::AppDirs::locate() {
        Ok(dirs) => concat_host::logs::open(&dirs, extra).map(|_| ()),
        // No directory at all: the facade still has to lead somewhere, or
        // every line disappears silently rather than loudly.
        Err(error) => concat_host::logs::open_console(extra).and(Err(error)),
    };
    if let Err(error) = opened {
        log::warn!("this run is not being written to a file: {error}");
    }
}

/// Says why the window could not start, somewhere the user will see it.
///
/// [`run`] returns its failure, and a desktop binary's `main` returns that
/// to the runtime, which prints it on standard error and exits 1 - exactly
/// what a packaged GUI build has no console for. Each entry point calls
/// this on the error before letting it go, so the reason is in the log and
/// in a dialog, not only in a console that was never there.
/// https://github.com/jub0t/Concat/issues/135
pub fn report_startup_failure(error: &slint::PlatformError) {
    let error = error.to_string();
    log::error!("could not start: {error}");
    platform::report_startup_failure(&error);
}

/// Builds the window, binds it to the engine, and runs it until it closes.
pub fn run() -> Result<(), slint::PlatformError> {
    run_with_project(None)
}

/// Builds the window and opens a project directory before its first frame.
///
/// Relative paths are resolved against the launcher's working directory. An
/// unreadable project leaves the launch screen open with the usual error notice.
/// Each invocation creates its own window; this does not control another instance.
pub fn run_with_project(project: Option<std::path::PathBuf>) -> Result<(), slint::PlatformError> {
    start(project, false)
}

/// Starts the way [`run`] does, as far as the window's first publish, and
/// exits the process with status 0 there instead of entering the event
/// loop, so nothing is shown and nobody has to close anything.
///
/// For CI: a smoke run goes through everything a launch on a fresh machine
/// does - the GPU, the engine's services, the window's component, the
/// studio and its packages, every model handed to the window - and fails
/// the way a launch would, with the reason in the log and an `Err` here.
/// No project is opened, so the recents list is not touched; the
/// preferences are read, and a log file written, as on any launch.
pub fn smoke() -> Result<(), slint::PlatformError> {
    start(None, true)
}

/// Runs one way out of the open project, in the order that keeps the work
/// safe: `close` writes the project and lets its session go, and `leave` -
/// hiding the window, quitting the loop, starting a replacement build - runs
/// only when that write landed.
///
/// Every path out goes through this: the title bar's X, the system's close
/// request, File › Close Window and a version switch. Before it existed, a
/// save that failed still hid the window and quit, taking whatever the
/// project held with it; a failure here leaves the session, its running jobs
/// and the window exactly as they were, and returns the reason.
pub(crate) fn close_window(
    close: impl FnOnce() -> Result<(), String>,
    leave: impl FnOnce(),
) -> Result<(), String> {
    let result = close();
    if result.is_ok() {
        leave();
    }
    result
}

/// [`run_with_project`], or [`smoke`] when `smoke` is set.
fn start(project: Option<std::path::PathBuf>, smoke: bool) -> Result<(), slint::PlatformError> {
    // What Import and a finished export reach on an iPhone. Android's own
    // crate installs its; iOS runs this crate's binary, so it is here.
    // https://github.com/jub0t/Concat/issues/332
    #[cfg(target_os = "ios")]
    ios::install();

    // The interface scale: a percent of the display's own, applied once
    // the window is up and kept through moves between monitors; see
    // `platform::apply_ui_scale`. https://github.com/jub0t/Concat/issues/320
    if let Ok(dirs) = concat_host::AppDirs::locate() {
        let percent = prefs::Preferences::load(&dirs)
            .ui_scale
            .filter(|percent| prefs::UI_SCALES.contains(percent));
        platform::set_ui_scale(percent);
    }

    let (startup_mark, safe) = safe_start();

    // The shell doesn't exist yet - it needs the window the backend is about
    // to help create - but `Shell::with` is a no-op until `Shell::install`
    // runs, and by the time an OS drop can actually happen, it has. Import
    // goes through the same `Studio::import` the Import menu uses, so a
    // dropped file gets the same probe, the same failure notice, and the
    // same "no project open yet" no-op that a picked one does.
    let gpu = platform::select_backend(
        |paths| {
            Shell::with(|shell, app| {
                {
                    let mut studio = shell.studio.borrow_mut();
                    // Sound files dropped while the library shows its
                    // Audio page go into the user's own sounds, as its
                    // Import would take them; anything else, or the page
                    // not up, into the project's bin.
                    let to_library = studio.sounds.shown
                        && !paths.is_empty()
                        && paths.iter().all(|path| concat_host::sounds::is_sound(path));
                    if studio.on_start {
                        // On the launch screen there is no project to
                        // import into: a dropped project opens, and media
                        // starts one.
                        studio.handle(Msg::Start(StartMsg::Dropped(paths)));
                    } else if to_library {
                        studio.handle(Msg::Sounds(SoundsMsg::Import(paths)));
                    } else {
                        studio.handle(Msg::Media(MediaMsg::Import(paths)));
                    }
                }
                shell.studio.borrow_mut().refresh_art();
                shell.studio.borrow().publish(&app, &shell.models);
            });
        },
        // A press anywhere but the field being typed into takes the focus
        // back, the way a browser does; the field commits on the way out.
        // See the handler in platform.rs for why this is a raw event and
        // not a TouchArea.
        || Shell::with(|_, app| app.invoke_blur()),
    )?;

    let host = match Host::start(gpu) {
        Ok(host) => host,
        Err(error) => {
            log::error!("{error}");
            return Err(slint::PlatformError::Other(error));
        }
    };

    // The base fonts, so the preset cards can be drawn in them.
    fonts::register();
    let app = App::new()?;
    app.set_macos(platform::MACOS);
    // A phone wears the phone shell - see ui/phone/ - and its lanes keep
    // the playhead at their middle; the strip and the dock are the desk's.
    let phone = platform::phone();
    app.set_phone(phone);
    // The launch screen's version line, and whether its drop zone is
    // offered: a drag in from the system is a desk's (see platform.rs).
    app.set_app_version(env!("CARGO_PKG_VERSION").into());
    app.set_can_drop(cfg!(not(any(target_os = "android", target_os = "ios"))));

    // The user's own packages - imported looks, and any effect folder they
    // or the community wrote - sit beside the built-ins from the first
    // frame. One that will not load is skipped, and its reason is the
    // first thing the window says.
    let mut studio = Studio::new(host);
    studio.set_phone(phone);
    if safe {
        studio.notify(&i18n::t("start.safeMode"), false);
    }
    studio.reload_packages(false);
    studio.watch_packages();
    let dark = studio.prefs.dark.unwrap_or(true);
    app.global::<Theme>().set_dark(dark);
    // The accent is remembered by name and the names live on the Theme
    // global, so the index is looked up there rather than kept in Rust too.
    // A remembered hex is a colour picked by hand, which is the slot past
    // the names.
    {
        let theme = app.global::<Theme>();
        let names = theme.get_accent_names();
        let count = slint::Model::row_count(&names);
        match studio.prefs.custom_accent().and_then(format::parse_colour) {
            Some(colour) => {
                theme.set_accent_custom(colour);
                theme.set_accent_choice(count as i32);
            }
            None => {
                let index = studio
                    .prefs
                    .accent_index(slint::Model::iter(&names).map(|name| name.to_string()));
                theme.set_accent_choice(index as i32);
            }
        }
    }
    // A picked playhead colour is remembered as its hex; none remembered,
    // or one that will not parse, is the palette's own.
    if let Some(colour) = studio
        .prefs
        .playhead
        .as_deref()
        .and_then(format::parse_colour)
    {
        let theme = app.global::<Theme>();
        theme.set_playhead_custom(colour);
        theme.set_playhead_is_custom(true);
    }
    // The interface scale: the row on Settings > Appearance reads the index
    // off the Theme global, and the window was already made at the scale the
    // environment asked for above, so this only lines the two up.
    // https://github.com/jub0t/Concat/issues/320
    app.global::<Theme>()
        .set_ui_scale(studio.prefs.scale_index() as i32);

    let shell = Rc::new(Shell {
        app: app.as_weak(),
        studio: RefCell::new(studio),
        models: Models::new(),
    });
    Shell::install(shell.clone());

    // The machine's fonts, for the Text inspector's picker. Read on a
    // worker: on a machine with a few thousand faces it is a moment, and
    // the window should not open a moment late for a list nobody is
    // looking at yet.
    host::spawn(
        || concat_text::Fonts::new().families(),
        |studio, _, _, families| studio.system_fonts_known(families),
    );

    // Handed over once, here, and never replaced: a fresh model is a reset,
    // and a reset rebuilds every row that hangs off it.
    {
        let editor = app.global::<Editor>();
        {
            let state = shell.studio.borrow();
            editor.set_transition_draft_duration(state.transition_draft_duration as f32);
            editor.set_transition_draft_intensity(state.transition_draft_intensity.unwrap_or(0.7) as f32);
        }
        let models = &shell.models;
        editor.set_timeline_tabs(ModelRc::from(models.tabs.clone()));
        editor.set_tracks(ModelRc::from(models.tracks.clone()));
        editor.set_clips(ModelRc::from(models.clips.clone()));
        editor.set_stage_items(ModelRc::from(models.stage.clone()));
        editor.set_stage_guides(ModelRc::from(models.guides.clone()));
        editor.set_media(ModelRc::from(models.media.clone()));
        editor.set_video_effects(ModelRc::from(models.video_effects.clone()));
        editor.set_audio_effects(ModelRc::from(models.audio_effects.clone()));
        editor.set_catalogue_effects(ModelRc::from(models.catalogue_effects.clone()));
        editor.set_catalogue_filters(ModelRc::from(models.catalogue_filters.clone()));
        editor.set_catalogue_luts(ModelRc::from(models.catalogue_luts.clone()));
        editor.set_catalogue_audio(ModelRc::from(models.catalogue_audio.clone()));
        editor.set_catalogue_transitions(ModelRc::from(models.catalogue_transitions.clone()));
        editor.set_effect_groups(ModelRc::from(models.effect_groups.clone()));
        editor.set_filter_groups(ModelRc::from(models.filter_groups.clone()));
        editor.set_audio_groups(ModelRc::from(models.audio_groups.clone()));
        editor.set_transition_groups(ModelRc::from(models.transition_groups.clone()));
        editor.set_applied_visual(ModelRc::from(models.applied_visual.clone()));
        editor.set_applied_audio(ModelRc::from(models.applied_audio.clone()));
        editor.set_visual_params(ModelRc::from(models.visual_params.clone()));
        editor.set_audio_params(ModelRc::from(models.audio_params.clone()));
        editor.set_adjust_params(ModelRc::from(models.adjust_params.clone()));
        editor.set_adjust_wheels(ModelRc::from(models.adjust_wheels.clone()));
        editor.set_adjust_curves(ModelRc::from(models.adjust_curves.clone()));
        editor.set_visual_wheels(ModelRc::from(models.visual_wheels.clone()));
        editor.set_visual_curves(ModelRc::from(models.visual_curves.clone()));
        editor.set_visual_colours(ModelRc::from(models.visual_colours.clone()));
        app.global::<Keyframes>()
            .set_rows(ModelRc::from(models.key_rows.clone()));
        app.global::<KeyEditor>()
            .set_rows(ModelRc::from(models.key_editor_rows.clone()));
        editor.set_key_marks(ModelRc::from(models.key_marks.clone()));
        app.global::<Library>()
            .set_views(ModelRc::from(models.library_views.clone()));
        editor.set_menu_items(ModelRc::from(models.menu.clone()));
        app.set_caption_models(ModelRc::from(models.caption_models.clone()));
        app.set_speech_models(ModelRc::from(models.speech_models.clone()));
        app.set_speech_model_details(ModelRc::from(models.speech_model_details.clone()));
        app.set_speech_voices(ModelRc::from(models.speakers.clone()));
        app.set_speech_voice_details(ModelRc::from(models.speaker_details.clone()));
        app.set_speech_voice_avatars(ModelRc::from(models.speaker_avatars.clone()));
        app.set_speech_samples(ModelRc::from(models.speech_samples.clone()));
        app.set_speech_sample_waves(ModelRc::from(models.speech_sample_waves.clone()));
        app.set_speech_sample_details(ModelRc::from(models.speech_sample_details.clone()));
        app.global::<Sounds>()
            .set_mine(ModelRc::from(models.sounds_mine.clone()));
        app.global::<Sounds>()
            .set_voices_saved(ModelRc::from(models.voices_saved.clone()));
        app.global::<Sounds>()
            .set_concat(ModelRc::from(models.sounds_concat.clone()));
        app.global::<Sounds>()
            .set_voices_builtin(ModelRc::from(models.voices_builtin.clone()));
        app.global::<Sounds>()
            .set_styles(ModelRc::from(models.voice_styles.clone()));
        app.set_voiceover_devices(ModelRc::from(models.voiceover_devices.clone()));
        app.set_app_menu_items(ModelRc::from(models.bar.clone()));
        app.set_transcribers(ModelRc::from(models.transcribers.clone()));
        app.set_voices(ModelRc::from(models.voices.clone()));
        app.set_versions(ModelRc::from(models.versions.clone()));
        app.set_version_details(ModelRc::from(models.version_details.clone()));
        editor.set_seats(ModelRc::from(models.seats.clone()));
        editor.set_dividers(ModelRc::from(models.dividers.clone()));
        app.set_recents(ModelRc::from(models.recents.clone()));
        editor.set_text_presets(ModelRc::from(models.text_presets.clone()));
        editor.set_style_presets(ModelRc::from(models.style_presets.clone()));
        editor.set_language_presets(ModelRc::from(models.language_presets.clone()));
        editor.set_font_families(ModelRc::from(models.font_families.clone()));
        app.global::<FontSearch>()
            .set_matches(ModelRc::from(models.font_matches.clone()));
    }

    // Settings > About's block, gathered once: nothing in it changes while
    // the process runs.
    let facts = sysinfo::system_facts();
    app.set_system_report(
        facts
            .iter()
            .map(|(label, value)| format!("{label}: {value}"))
            .collect::<Vec<_>>()
            .join("\n")
            .into(),
    );
    app.set_system_facts(ModelRc::from(Rc::new(VecModel::from(
        facts
            .into_iter()
            .map(|(label, value)| SystemFactData {
                label: label.into(),
                value: value.into(),
            })
            .collect::<Vec<_>>(),
    ))));

    // The ladders' labels, handed over once; the index the form reports back
    // is what carries the meaning. The shapes carry their ratio as well,
    // because the form draws each one as a box of its own proportions and
    // a label is a string Slint cannot do arithmetic with.
    app.set_start_aspects(ModelRc::from(Rc::new(VecModel::from(
        ASPECTS
            .iter()
            .map(|(label, w, h)| ChipOption {
                label: SharedString::from(*label),
                ratio: *w as f32 / *h as f32,
            })
            .collect::<Vec<_>>(),
    ))));
    // A size and a rate are words and nothing else, so their chips carry no
    // shape: a zero ratio is what the form draws nothing for.
    app.set_start_sizes(ModelRc::from(Rc::new(VecModel::from(
        SIZES
            .iter()
            .map(|(label, _)| ChipOption {
                label: SharedString::from(*label),
                ratio: 0.0,
            })
            .collect::<Vec<_>>(),
    ))));
    app.set_start_rates(ModelRc::from(Rc::new(VecModel::from(
        RATES
            .iter()
            .map(|(label, _, _)| ChipOption {
                label: SharedString::from(*label),
                ratio: 0.0,
            })
            .collect::<Vec<_>>(),
    ))));
    app.set_languages(ModelRc::from(Rc::new(VecModel::from(
        shell
            .studio
            .borrow()
            .languages
            .iter()
            .map(|language| SharedString::from(language.name.as_str()))
            .collect::<Vec<_>>(),
    ))));

    // The interface's words. Every `I18n.t` in the tree asks here by its
    // key; the answer is the active locale's line, else the English. See
    // i18n.rs.
    {
        let words = app.global::<I18n>();
        words.on_lookup(|_, key| i18n::t(&key).into());
        words.on_lookup1(|_, key, a| i18n::tf(&key, &[&a]).into());
        words.on_lookup2(|_, key, a, b| i18n::tf(&key, &[&a, &b]).into());
        // Uppercased here rather than in the tree: Slint has no text
        // transform, and casing is the language's rule and not the
        // interface's to guess at.
        words.on_lookup_upper(|_, key| i18n::t(&key).to_uppercase().into());
        words.set_lang(i18n::current().into());
    }

    // The strip's drag region and double-click. Only the platform's window
    // can do either; the scene graph forwards the gestures here.
    app.on_titlebar_begin_drag({
        let weak = app.as_weak();
        move || {
            if let Some(app) = weak.upgrade() {
                platform::begin_drag(app.window());
            }
        }
    });
    app.on_titlebar_toggle_maximize({
        let weak = app.as_weak();
        move || {
            if let Some(app) = weak.upgrade() {
                platform::toggle_maximize(app.window());
                app.set_window_maximized(platform::is_maximized(app.window()));
            }
        }
    });
    // The strip's own window buttons, on the platforms whose decorations
    // were taken off. Close goes the way the File menu's Close does - the
    // project is shut first, so an autosave in flight is not orphaned.
    app.on_titlebar_minimize({
        let weak = app.as_weak();
        move || {
            if let Some(app) = weak.upgrade() {
                platform::minimize(app.window());
            }
        }
    });
    app.on_titlebar_close(|| {
        log::info!("close: titlebar X pressed");
        Shell::with(|shell, app| {
            let outcome = close_window(
                || shell.studio.borrow_mut().close_project(),
                || {
                    log::info!("close: project closed, hiding window");
                    app.window().hide().ok();
                    slint::quit_event_loop().ok();
                    log::info!("close: quit_event_loop called");
                },
            );
            if let Err(error) = outcome {
                // The save failed and `close_project` left the session
                // open, so the window stays up. Its notice is published
                // here or nobody would see it.
                log::warn!("close: not closing, project could not be saved: {error}");
                shell.studio.borrow_mut().offer_quit_without_saving(error);
                shell.studio.borrow_mut().refresh_art();
                shell.studio.borrow().publish(&app, &shell.models);
            }
        });
    });
    // System-driven close (Alt+F4, taskbar close): the same road out, and
    // refused the same way when the project cannot be saved.
    app.window().on_close_requested(|| {
        log::info!("close: system close request (Alt+F4 / taskbar)");
        let saved = std::cell::Cell::new(true);
        Shell::with(|shell, app| {
            let outcome = close_window(
                || shell.studio.borrow_mut().close_project(),
                || {
                    slint::quit_event_loop().ok();
                },
            );
            if let Err(error) = outcome {
                saved.set(false);
                log::warn!("close: not closing, project could not be saved: {error}");
                shell.studio.borrow_mut().offer_quit_without_saving(error);
                shell.studio.borrow_mut().refresh_art();
                shell.studio.borrow().publish(&app, &shell.models);
            }
        });
        if saved.get() {
            slint::CloseRequestResponse::HideWindow
        } else {
            slint::CloseRequestResponse::KeepWindowShown
        }
    });
    // Maximised or not is read back on every resize rather than tracked:
    // the platform can maximise the window without us - a drag to the top
    // edge, Win+Up - and the size changing is the one signal every such
    // route has in common.
    app.on_window_resized({
        let weak = app.as_weak();
        move || {
            if let Some(app) = weak.upgrade() {
                let maximized = platform::is_maximized(app.window());
                if app.get_window_maximized() != maximized {
                    app.set_window_maximized(maximized);
                }
            }
        }
    });
    app.set_own_window_buttons(platform::OWN_WINDOW_BUTTONS);
    studio::caption_preview::install(&app);

    // Mutate, then republish. Every handler is one of these three: the whole
    // window, the lanes alone (for the handlers a pointer drives directly,
    // which arrive as a stream), or the dock alone (a gutter drag).
    macro_rules! handler {
        ($publish:ident, |$state:ident $(, $arg:ident : $ty:ty)*| $body:block) => {{
            move |$($arg : $ty),*| {
                Shell::with(|shell, app| {
                    {
                        let mut $state = shell.studio.borrow_mut();
                        $body
                    }
                    shell.studio.borrow_mut().refresh_art();
                    shell.studio.borrow().$publish(&app, &shell.models);
                });
            }
        }};
    }
    macro_rules! on_window {
        ($($handler:tt)*) => { handler!(publish, $($handler)*) };
    }
    macro_rules! on_lanes {
        ($($handler:tt)*) => { handler!(publish_lanes, $($handler)*) };
    }
    macro_rules! on_dock {
        ($($handler:tt)*) => { handler!(publish_dock, $($handler)*) };
    }

    let editor = app.global::<Editor>();

    // ── the launch screen ──
    app.on_start_compose(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::Compose));
    }));
    app.on_start_dismiss(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::Dismiss));
    }));
    app.on_start_name_edited(on_window!(|state, name: SharedString| {
        state.handle(Msg::Start(StartMsg::NameEdited(name.to_string())));
    }));
    app.on_start_location_edited(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::LocationEdited(path.to_string())));
    }));
    app.on_start_aspect_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Start(StartMsg::AspectChanged(index)));
    }));
    app.on_start_size_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Start(StartMsg::SizeChanged(index)));
    }));
    app.on_start_custom_width_edited(on_window!(|state, width: f32| {
        state.handle(Msg::Start(StartMsg::CustomWidth(width)));
    }));
    app.on_start_custom_height_edited(on_window!(|state, height: f32| {
        state.handle(Msg::Start(StartMsg::CustomHeight(height)));
    }));
    app.on_start_custom_fps_edited(on_window!(|state, fps: f32| {
        state.handle(Msg::Start(StartMsg::CustomFps(fps)));
    }));
    app.on_start_rate_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Start(StartMsg::RateChanged(index)));
    }));
    app.on_start_dismiss_error(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::DismissError));
    }));
    app.on_start_browse(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::Browse));
    }));
    app.on_start_create(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::Create));
    }));
    app.on_start_open(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::Open));
    }));
    app.on_start_open_recent(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::OpenRecent(path.to_string())));
    }));
    app.on_start_forget_recent(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::ForgetRecent(path.to_string())));
    }));
    app.on_start_reveal_recent(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::RevealRecent(path.to_string())));
    }));
    app.on_start_rename_recent(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::PromptRename(path.to_string())));
    }));
    app.on_start_delete_recent(on_window!(|state, path: SharedString| {
        state.handle(Msg::Start(StartMsg::PromptDelete(path.to_string())));
    }));
    app.on_start_rename_edited(on_window!(|state, name: SharedString| {
        state.handle(Msg::Start(StartMsg::RenameEdited(name.to_string())));
    }));
    app.on_start_apply_rename(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::ApplyRename));
    }));
    app.on_start_dismiss_rename(on_window!(|state| {
        state.handle(Msg::Start(StartMsg::DismissRename));
    }));
    app.on_start_search_edited(on_window!(|state, query: SharedString| {
        state.handle(Msg::Start(StartMsg::SearchEdited(query.to_string())));
    }));
    app.on_start_sort_changed(on_window!(|state, sort: i32| {
        state.handle(Msg::Start(StartMsg::Sort(sort)));
    }));
    app.on_start_link(on_window!(|state, name: SharedString| {
        state.handle(Msg::Start(StartMsg::Link(name.to_string())));
    }));

    // ── the workspace's arrangement ──
    editor.on_workspace_resized(on_dock!(|state, width: f32, height: f32| {
        state.workspace = (width, height);
        // A narrow window - a phone, a tablet held upright, a desktop
        // window squeezed - gets the compact dock: two seats, the monitor
        // over the timeline, with the switcher on each to bring the
        // library or the inspector into it. The wide dock waits, whole,
        // for the window to widen again; every dock operation works on
        // whichever is showing.
        state.set_compact(width < studio::COMPACT_WIDTH);
    }));
    editor.on_dock_set(on_dock!(|state, seat: i32, kind: PaneKind| {
        let Some(path) = state.dock.leaf_path(seat.max(0) as usize) else {
            return;
        };
        if let Dock::Leaf(held) = state.dock.at_mut(&path) {
            *held = kind;
        }
    }));
    editor.on_dock_dropped(on_dock!(|state, from: i32, onto: i32, side: DockSide| {
        let (from, onto) = (from.max(0) as usize, onto.max(0) as usize);
        if from == onto {
            return;
        }
        let (Some(taken), Some(displaced)) = (state.dock.kind_at(from), state.dock.kind_at(onto))
        else {
            return;
        };
        if side == DockSide::Centre {
            for (index, kind) in [(from, displaced), (onto, taken)] {
                let Some(path) = state.dock.leaf_path(index) else {
                    continue;
                };
                if let Dock::Leaf(held) = state.dock.at_mut(&path) {
                    *held = kind;
                }
            }
            return;
        }
        let (Some(onto_path), Some(from_path)) =
            (state.dock.leaf_path(onto), state.dock.leaf_path(from))
        else {
            return;
        };
        state.dock.split_leaf(&onto_path, taken, side);
        state.dock.remove_leaf(&from_path);
    }));
    editor.on_dock_add(on_dock!(|state, kind: PaneKind| {
        let seats = state.dock_layout().seats;
        let Some(biggest) = seats
            .iter()
            .max_by(|a, b| (a.width * a.height).total_cmp(&(b.width * b.height)))
        else {
            return;
        };
        let across = biggest.width >= SEAT_MIN_W * 2.0;
        let down = biggest.height >= SEAT_MIN_H * 2.0;
        let side = if biggest.width >= biggest.height && (across || !down) {
            DockSide::Right
        } else {
            DockSide::Bottom
        };
        let Some(path) = state.dock.leaf_path(biggest.index.max(0) as usize) else {
            return;
        };
        state.dock.split_leaf(&path, kind, side);
    }));
    editor.on_dock_remove(on_dock!(|state, seat: i32| {
        let Some(path) = state.dock.leaf_path(seat.max(0) as usize) else {
            return;
        };
        state.dock.remove_leaf(&path);
    }));
    editor.on_divider_pressed(on_dock!(|state, index: i32| {
        let index = index.max(0) as usize;
        state.divider_press = match (state.split_ratio(index), state.split_extent(index)) {
            (Some(ratio), Some(extent)) => Some((index, ratio, extent)),
            _ => None,
        };
    }));
    editor.on_divider_dragged(on_dock!(|state, index: i32, delta: f32| {
        let Some((held, from, extent)) = state.divider_press else {
            return;
        };
        if held != index.max(0) as usize || extent <= 0.0 {
            return;
        }
        let Some(path) = state.dock.split_path(held) else {
            return;
        };
        let Dock::Split { columns, ratio, .. } = state.dock.at_mut(&path) else {
            return;
        };
        let wanted = if *columns { SEAT_MIN_W } else { SEAT_MIN_H };
        let floor = if wanted * 2.0 <= extent {
            wanted
        } else {
            SEAT_MIN_GRAB.min(extent / 2.0)
        } / extent;
        *ratio = (from + delta / extent).clamp(floor, 1.0 - floor);
    }));

    app.global::<TransitionControls>().on_apply(on_window!(|state, json:SharedString| {
        state.set_transition_controls(&json);
    }));
    app.global::<ParticleBulk>().on_select_effect(on_window!(|state, id:SharedString| {
        state.effect_select(id.as_str());
    }));
    app.global::<ParticleBulk>().on_change_param(on_window!(|state, id:SharedString, key:SharedString, value:f32| {
        state.effect_set_param(id.as_str(), key.as_str(), value);
    }));
    app.global::<ParticleBulk>().on_change_colour(on_window!(|state, id:SharedString, key:SharedString, value:slint::Color| {
        state.effect_set_colour(id.as_str(), key.as_str(), value);
    }));
    app.global::<ParticleBulk>().on_save_default(on_window!(|state, id:SharedString| {
        state.effect_save_default(id.as_str());
    }));
    app.global::<ParticleBulk>().on_reset_default(on_window!(|state, id:SharedString| {
        state.effect_reset_default(id.as_str());
    }));
    app.global::<ParticleBulk>().on_clear_preview(on_window!(|state| {
        state.effect_select("");
    }));
    app.global::<ParticleBulk>().on_apply(on_window!(|state, preset:i32, mode:i32, selected_only:bool, images:bool, videos:bool| {
        state.apply_particle_bulk(preset,mode,selected_only,images,videos);
    }));
    app.global::<ParticleBulk>().on_toggle_favourite(on_window!(|state, index: i32| {
        const PARTICLE_IDS: [&str; 12] = [
            "stars", "gold-sparkles", "embers", "fireflies", "bokeh",
            "dust-motes", "snow", "rain", "ash", "light-leaks",
            "shooting-stars", "confetti",
        ];
        if let Some(id) = usize::try_from(index)
            .ok().and_then(|index| PARTICLE_IDS.get(index)) {
            let key = format!("concat.particles-{id}");
            let starred = state.prefs.favourites.iter().any(|entry| entry == &key);
            state.library_favourite(&key, !starred);
        }
    }));
    app.global::<CanvasPresets>().on_choose(move |index| {
        Shell::with(|_, app| {
            if let Some(config) = concat_project::canvas_preset::Config::preset(index as u32) {
                canvas_publish(&app, &config);
            }
        });
    });
    app.global::<CanvasPresets>().on_edit_number(move |field, value| {
        Shell::with(|_, app| {
            canvas_modify(&app, |config| {
                let value = value as f64;
                match field.as_str() {
                    "foregroundX" => config.foreground.x = value,
                    "foregroundY" => config.foreground.y = value,
                    "foregroundWidth" => config.foreground.width = value,
                    "foregroundHeight" => config.foreground.height = value,
                    "borderWidth" => config.border_width = value,
                    "radius" => config.radius = value,
                    "blur" => config.blur = value,
                    "tintOpacity" => config.tint_opacity = value,
                    "shadowOpacity" => config.shadow_opacity = value,
                    "shadowOffset" => config.shadow_offset = value,
                    "matBottom" => config.mat_bottom = value,
                    "rotation" => config.rotation = value,
                    _ => {}
                }
            });
        });
    });
    app.global::<CanvasPresets>().on_edit_colour(move |field, value| {
        Shell::with(|_, app| {
            canvas_modify(&app, |config| {
                let hex = format!("#{:02x}{:02x}{:02x}", value.red(), value.green(), value.blue());
                match field.as_str() {
                    "borderColor" => config.border_color = hex,
                    "backgroundColor" => config.background_color = hex,
                    "backgroundTint" => config.background_tint = hex,
                    "shadowColor" => config.shadow_color = hex,
                    "matColor" => config.mat_color = hex,
                    _ => {}
                }
            });
        });
    });
    app.global::<CanvasPresets>().on_edit_mode(move |mode| {
        Shell::with(|_, app| {
            canvas_modify(&app, |config| {
                config.background = match mode {
                    1 => "blur", 2 => "color", 3 => "none", _ => "source",
                }.into();
            });
        });
    });
    app.global::<CanvasPresets>().on_sync_profile(move || {
        Shell::with(|_, app| {
            let panel = app.global::<CanvasPresets>();
            if let Ok(config) = concat_project::canvas_preset::Config::from_json(&panel.get_profile()) {
                canvas_publish(&app, &config);
            }
        });
    });
    app.global::<CanvasPresets>().on_random_favourites(on_window!(|state,images:bool,videos:bool,selected:bool| {
        state.apply_canvas_favourites_randomly(images,videos,selected);
    }));
    app.global::<CanvasPresets>().on_apply(on_window!(|state, json: SharedString, all: bool, images:bool, videos:bool| {
        state.apply_canvas_preset(&json, all, images, videos);
    }));
    app.global::<CanvasPresets>().on_toggle_favourite(move |preset| {
        Shell::with(|shell, app| {
            shell.studio.borrow_mut().toggle_canvas_preset_favourite(preset);
            let stars = (1..=8).map(|index|
                shell.studio.borrow().prefs.canvas_preset_favourites
                    .iter().any(|held| held == &format!("canvas:{index}"))
            ).collect::<Vec<_>>();
            app.global::<CanvasPresets>().set_favourites(ModelRc::new(VecModel::from(stars)));
        });
    });
    // Project-specific edits remain separate; only an explicitly saved Canvas
    // profile seeds the editor on a future launch/new project.
    app.global::<CanvasPresets>().on_save_default(move |profile| {
        Shell::with(|shell, app| {
            match concat_project::canvas_preset::Config::from_json(profile.as_str()) {
                Ok(config) => {
                    let mut studio = shell.studio.borrow_mut();
                    studio.prefs.canvas_default = serde_json::to_string(&config).ok();
                    studio.prefs.save(&studio.host.dirs);
                    app.global::<CanvasPresets>().set_default_status(
                        "Canvas default saved for future projects and restarts.".into());
                }
                Err(reason) => {
                    app.global::<CanvasPresets>().set_default_status(
                        format!("Canvas default not saved: {reason}").into());
                }
            }
        });
    });
    app.global::<CanvasPresets>().on_reset_default(move || {
        Shell::with(|shell, app| {
            let mut studio = shell.studio.borrow_mut();
            studio.prefs.canvas_default = None;
            studio.prefs.save(&studio.host.dirs);
            drop(studio);
            let sports = concat_project::canvas_preset::Config::preset(1)
                .expect("sports canvas default");
            canvas_publish(&app, &sports);
            app.global::<CanvasPresets>().set_default_status(
                "Canvas default reset to Sports Frame.".into());
        });
    });
    Shell::with(|shell, app| {
        let remembered = shell.studio.borrow().prefs.canvas_default.as_deref()
            .and_then(|json| concat_project::canvas_preset::Config::from_json(json).ok())
            .unwrap_or_else(|| concat_project::canvas_preset::Config::preset(1)
                .expect("sports canvas default"));
        let stars = (1..=8)
            .map(|index| shell.studio.borrow().prefs.canvas_preset_favourites
                .iter().any(|held| held == &format!("canvas:{index}")))
            .collect::<Vec<_>>();
        app.global::<CanvasPresets>().set_favourites(ModelRc::new(VecModel::from(stars)));
        canvas_publish(&app, &remembered);
    });
    app.on_start_c_builder(on_window!(|studio| {studio.builder_home();}));
    app.on_c_builder_return_home(on_window!(|studio,profile:SharedString| {studio.builder_return_home(&profile);}));
    app.on_c_builder_open_generated(on_window!(|studio| {studio.builder_open_generated();}));
    app.on_c_builder_cancel_generation(on_window!(|studio| {studio.builder_cancel_generation();}));
    app.on_c_builder_retry_used(on_window!(|studio| {studio.builder_retry_used();}));
    app.on_c_builder_choose_output(|| {
        #[cfg(not(any(target_os="android",target_os="ios")))]
        host::after_dialog(||rfd::FileDialog::new().set_title("C Builder output folder").pick_folder(),
            |studio,_,_,folder|{if !studio.c_builder.scanning {studio.cb_output_folder=folder.to_string_lossy().into_owned();}});
    });
    // ── Home C Builder (no separate EXE or editor project required) ──
    editor.on_cb_import_folder(|| {
        #[cfg(not(any(target_os = "android", target_os = "ios")))]
        host::after_dialog(
            || rfd::FileDialog::new().set_title("C Builder - Import Media Folder").pick_folder(),
            |studio, _, _, folder| c_builder_scan(studio, folder),
        );
    });
    editor.on_cb_refresh_folder(on_window!(|studio| {
        if !studio.c_builder.folder.is_empty() && !studio.c_builder.scanning {
            let folder = std::path::PathBuf::from(studio.c_builder.folder.clone());
            c_builder_scan(&mut studio, folder);
        }
    }));
    editor.on_cb_project_name_edited(on_window!(|studio,name:SharedString|{studio.c_builder.edit_project_name(&name);}));
    editor.on_cb_reset_project_name(on_window!(|studio|{studio.c_builder.reset_project_name();}));
    editor.on_cb_choose_narration(|| {
        host::pick_files_then("C Builder - Narration".into(),
            Some(("Audio".into(), &["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus"])),
            |studio, _, _, paths| {
                let Some(path) = paths.into_iter().next() else { return; };
                if studio.c_builder.scanning { return; }
                studio.c_builder.scanning = true;
                studio.c_builder.status = "Reading narration duration...".into();
                host::spawn(
                    move || {
                        let duration = c_builder::narration_seconds(&path)?;
                        Ok::<_, String>((path, duration))
                    },
                    |studio, _, _, outcome| {
                        studio.c_builder.scanning = false;
                        match outcome {
                            Ok((path, duration)) => {
                                studio.c_builder.choose_narration_name(&path);
                                studio.c_builder.narration = path.to_string_lossy().into_owned();
                                studio.c_builder.duration = Some(duration);
                                studio.c_builder.status = format!("Narration ready: {duration:.2}s");
                            }
                            Err(error) => {
                                studio.c_builder.status = format!("Narration error: {error}");
                                studio.notify(&error, true);
                            }
                        }
                    },
                );
            },
        );
    });
    editor.on_cb_keyword_changed(on_window!(|studio,json:SharedString,video:bool,keyword:SharedString,selected:bool,weight:i32| {
        studio.set_c_builder_keyword(&json,video,&keyword,selected,weight);
    }));
    editor.on_cb_equalize_keywords(on_window!(|studio,json:SharedString,video:bool| {
        studio.equalize_c_builder_keywords(&json,video);
    }));
    editor.on_cb_tune_profile(on_window!(
        |studio, json:SharedString, name:SharedString, opening:SharedString, closing:SharedString,
         image_min:SharedString, image_max:SharedString, video_min:SharedString, video_max:SharedString,
         max_videos:SharedString, max_images:SharedString, cooldown:SharedString, seed:SharedString| {
            if studio.c_builder.scanning { return; }
            let mut profile=match concat_project::planning::Profile::from_json(&json) {
                Ok(p)=>p,Err(e)=>{studio.notify(&e,true);return;}
            };
            let parse_float=|label:&str,value:&SharedString|->Result<f64,String> {
                value.trim().parse::<f64>().map_err(|_|format!("{label} must be a number"))
            };
            let parse_usize=|label:&str,value:&SharedString|->Result<usize,String> {
                value.trim().parse::<usize>().map_err(|_|format!("{label} must be a non-negative integer"))
            };
            let result=(||->Result<(),String>{
                let first=parse_float("Opening video %",&opening)?;
                let last=parse_float("Ending video %",&closing)?;
                if !(0.0..=100.0).contains(&first)||!(0.0..=100.0).contains(&last) {
                    return Err("Video mix percentages must be within 0–100".into());
                }
                profile.name=name.trim().to_owned();
                // Preserve any custom intermediate curve anchors.
                profile.curve.first_mut().expect("validated curve").video=first/100.;
                profile.curve.last_mut().expect("validated curve").video=last/100.;
                profile.image_seconds=[
                    parse_float("Image minimum seconds",&image_min)?,
                    parse_float("Image maximum seconds",&image_max)?,
                ];
                profile.video_seconds=[
                    parse_float("Video minimum seconds",&video_min)?,
                    parse_float("Video maximum seconds",&video_max)?,
                ];
                profile.max_videos=parse_usize("Max video run",&max_videos)?;
                profile.max_images=parse_usize("Max image run",&max_images)?;
                profile.cooldown=parse_usize("Cooldown",&cooldown)?;
                profile.seed=seed.trim().parse::<u64>().map_err(|_|"Seed must be a non-negative integer".to_owned())?;
                if let Some(selection)=profile.media_selection.as_mut(){selection.normalize();}
                profile.validate()
            })();
            if let Err(e)=result{studio.notify(&e,true);return;}
            studio.c_builder.profile_json=serde_json::to_string_pretty(&profile).unwrap_or_default();
            studio.c_builder.preview.clear();studio.c_builder.preview_shots.clear();
            studio.apply(concat_project::Command::SetPlanningProfile {
                profile:serde_json::to_value(&profile).unwrap_or_default(),
            });
        }
    ));
    editor.on_cb_videos_toggled(on_window!(|studio,json:SharedString,on:bool| {
        studio.set_c_builder_videos_enabled(&json,on);
    }));
    editor.on_cb_plan_profile(on_window!(|studio, json: SharedString, images: SharedString, videos: SharedString, action: SharedString| {
        let action = action.to_string();
        if action == "load" {
            host::pick_files_then("Import C Builder planning profile".into(), Some(("JSON".into(), &["json"])),
                |studio, _, _, paths| {
                    if let Some(path) = paths.first() {
                        match std::fs::read_to_string(path).map_err(|e| e.to_string())
                            .and_then(|s| concat_project::planning::Profile::from_json(&s)) {
                            Ok(mut p) => {
                                if let Some(s)=p.media_selection.as_mut(){s.normalize();}
                                studio.c_builder.profile_json = serde_json::to_string_pretty(&p).unwrap_or_default();
                                studio.apply(Command::SetPlanningProfile {profile:serde_json::to_value(p).unwrap_or_default()});
                            },
                            Err(e) => studio.notify(&e, true),
                        }
                    }
                });
            return;
        }
        let mut profile = match concat_project::planning::Profile::from_json(&json) {
            Ok(p) => p,
            Err(e) => { studio.notify(&e, true); return; }
        };
        if let Some(selection)=profile.media_selection.as_mut(){selection.normalize();}
        let folder=studio.host.dirs.config.join("c-builder-profiles");
        if let Some(index) = action.strip_prefix("select:").and_then(|v|v.parse::<usize>().ok()) {
            let selected = concat_project::planning::Profile::builtin(index)
                .or_else(|| index.checked_sub(4).and_then(|i|studio.c_builder.stored_profiles.get(i).cloned()));
            if let Some(mut selected) = selected {
                if selected.media_selection.is_none(){selected.media_selection=profile.media_selection.clone();}
                profile=selected;
                if let Some(selection)=profile.media_selection.as_mut(){selection.normalize();}
                studio.c_builder.active_profile_name=profile.name.clone();
            }
            else {studio.notify("Planning profile no longer exists",true);return;}
        }
        if action == "duplicate" {
            let original=profile.name.clone();let mut n=1;
            loop {let candidate=format!("{original} Copy {n}");if !studio.c_builder.stored_profiles.iter().any(|p|p.name==candidate){profile.name=candidate;break;}n+=1;}
        }
        if let Some(index) = action.strip_prefix("pin:").and_then(|v|v.trim().parse::<usize>().ok()) {
            let Some(shot) = index.checked_sub(1).and_then(|i|studio.c_builder.preview_shots.get(i)) else {studio.notify("Preview a plan and choose its shot number first",true);return;};
            profile.pins.retain(|p|p.start_us!=shot.start_us);
            profile.pins.push(concat_project::planning::Pin {source_id:shot.asset.path.clone(),start_us:shot.start_us,duration_us:shot.duration_us,trim_us:shot.trim_us});
            profile.pins.sort_by_key(|p|p.start_us);
        }
        if action == "clear-pins" {profile.pins.clear();}
        if matches!(action.as_str(),"store"|"duplicate"|"rename") {
            if let Err(e)=c_builder::store_profile(&folder,&serde_json::to_string(&profile).unwrap_or_default()){studio.notify(&e,true);return;}
            if action=="rename" && !studio.c_builder.active_profile_name.is_empty() && studio.c_builder.active_profile_name!=profile.name {
                let old=c_builder::profile_file(&folder,&studio.c_builder.active_profile_name);
                if old.is_file()&&!old.is_symlink(){if let Err(e)=std::fs::remove_file(old){studio.notify(&e.to_string(),true);return;}}
            }
            studio.c_builder.active_profile_name=profile.name.clone();
            studio.c_builder.stored_profiles=c_builder::load_profiles(&folder);
        }
        if action=="delete" {
            let name=&studio.c_builder.active_profile_name;
            if !studio.c_builder.stored_profiles.iter().any(|p|&p.name==name){studio.notify("Built-in profiles cannot be deleted; select a saved profile first",true);return;}
            let path=c_builder::profile_file(&folder,name);
            if path.is_symlink(){studio.notify("Managed profile cannot be a symbolic link",true);return;}
            if let Err(e)=std::fs::remove_file(path){studio.notify(&e.to_string(),true);return;}
            studio.c_builder.stored_profiles=c_builder::load_profiles(&folder);
            studio.c_builder.active_profile_name.clear();
            studio.c_builder.status="Saved profile deleted. Current project settings are preserved.".into();
            return;
        }
        if action == "reroll" { profile.seed = c_builder::random_seed(); }
        let encoded = serde_json::to_string_pretty(&profile).unwrap_or_default();
        studio.c_builder.profile_json = encoded.clone();
        studio.apply(concat_project::Command::SetPlanningProfile {
            profile: serde_json::to_value(&profile).unwrap_or_default(),
        });
        if action == "save" {
            // A declarative profile is saved independently, so duplicates and renamed
            // profiles can be shared without rebuilding the application.
            #[cfg(not(any(target_os = "android", target_os = "ios")))]
            host::after_dialog(
                || rfd::FileDialog::new().set_title("Export planning profile").add_filter("JSON", &["json"]).save_file(),
                move |studio, _, _, path| {
                    let result = c_builder::save_profile(&path, &encoded);
                    if let Err(e) = result { studio.notify(&format!("Profile save failed: {e}"), true); }
                });
            return;
        }
        if matches!(action.as_str(),"store"|"duplicate"|"rename")||action.starts_with("select:"){return;}
        let Some(duration) = studio.c_builder.duration else { studio.notify("Select narration first", true); return; };
        profile.media_selection.get_or_insert_with(Default::default).normalize();
        let assets = studio.c_builder.assets.clone();
        let iw = images.to_string(); let vw = videos.to_string();
        studio.c_builder.scanning = true;
        host::spawn(move || c_builder::plan_profile(&assets, duration, &iw, &vw, &profile),
            |studio, _, _, result| {
                studio.c_builder.scanning = false;
                match result {
                    Ok((shots, summary)) => {studio.c_builder.preview_shots=shots;studio.c_builder.preview=summary;},
                    Err(e) => { studio.c_builder.preview = format!("Plan rejected: {e}"); studio.notify(&e, true); }
                }
            });
    }));
    editor.on_cb_generate_project(on_window!(
        |studio, name: SharedString, images: SharedString, videos: SharedString, coverage: i32, profile_json: SharedString| {
            if studio.c_builder.scanning { return; }
            if studio.cb_pending_used.is_some(){studio.notify("Retry the blocked used-media step before generating another timeline",true);return;}
            let Some(duration) = studio.c_builder.duration else {
                studio.notify("Select narration first", true);
                return;
            };
            let output=std::path::PathBuf::from(studio.cb_output_folder.clone());
            if output.as_os_str().is_empty(){studio.notify("Choose an output folder",true);return;}
            let audio = std::path::PathBuf::from(studio.c_builder.narration.clone());
            let assets = studio.c_builder.assets.clone();
            if !name.trim().is_empty() && name.as_str()!=studio.c_builder.project_name {studio.c_builder.edit_project_name(&name);}
            let project_name = studio.c_builder.effective_name();
            let image_weights = images.to_string();
            let video_weights = videos.to_string();
            let _ = coverage; // retained callback parameter for older UI integrations
            let mut profile = match concat_project::planning::Profile::from_json(&profile_json) {
                Ok(p) => p, Err(e) => { studio.notify(&e, true); return; }
            };
            profile.media_selection.get_or_insert_with(Default::default).normalize();
            let cache_path = studio.host.dirs.data.join("c-builder-media-cache.json");
            let library=std::path::PathBuf::from(studio.c_builder.folder.clone());
            let cancel=std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));studio.cb_generation_cancel=Some(cancel.clone());
            studio.c_builder.scanning = true;
            studio.c_builder.status = "Planning and verifying native Concat timeline...".into();
            host::spawn(
                move || {
                    if cancel.load(std::sync::atomic::Ordering::Relaxed){return Err("Generation cancelled before saving any project".into());}
                    std::fs::create_dir_all(&output).map_err(|e|format!("Could not create output folder: {e}"))?;
                    let (shots, _) = c_builder::plan_profile(&assets, duration, &image_weights,
                        &video_weights, &profile)?;
                    let folder = c_builder::write_native_with_profile(&shots, &audio, duration, &output,
                        &project_name, Some(&profile))?;
                    std::fs::write(folder.join("planning-profile.json"),
                        serde_json::to_vec_pretty(&profile).map_err(|e| e.to_string())?)
                        .map_err(|e| format!("Project created but profile snapshot failed: {e}"))?;
                    let used: Vec<String> = shots.iter().map(|s| s.asset.path.clone())
                        .collect::<std::collections::HashSet<_>>().into_iter().collect();
                    let sources=used.into_iter().map(std::path::PathBuf::from).collect::<Vec<_>>();
                    let pending=crate::studio::c_builder_home::PendingUsed{library,project:folder,sources,cache:cache_path};
                    let finish=c_builder_used::finish(&pending.library,&pending.project,&pending.sources,&pending.cache,&cancel);
                    Ok::<_, String>((pending,finish))
                },
                |studio, _, _, result| {
                    match result {
                        Ok((pending,finish))=>studio.builder_archive_finished(pending,finish),
                        Err(error)=>{studio.c_builder.scanning=false;studio.cb_generation_cancel=None;studio.c_builder.status=format!("Generation failed: {error}");studio.notify(&error,true);}
                    }
                },
            );
        }
    ));

    // ── the bin ──
    editor.on_media_filter_changed(on_window!(|state, filter: MediaFilter| {
        state.handle(Msg::Media(MediaMsg::FilterChanged(filter)));
    }));
    editor.on_media_sort_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Media(MediaMsg::SortChanged(index)));
    }));
    editor.on_media_select(on_window!(|state, row: i32, additive: bool, range: bool| {
        state.handle(Msg::Media(MediaMsg::Select { row, additive, range }));
    }));
    editor.on_media_select_all(on_window!(|state| {
        state.handle(Msg::Media(MediaMsg::SelectAllVisible));
    }));
    editor.on_media_add_selected(on_window!(|state, at_playhead: bool| {
        state.handle(Msg::Media(MediaMsg::AddSelected { at_playhead }));
    }));
    editor.on_media_band_selected(on_window!(
        |state,
         columns: i32,
         from_col: i32,
         to_col: i32,
         from_row: i32,
         to_row: i32,
         additive: bool| {
            state.handle(Msg::Media(MediaMsg::Band {
                columns,
                from_col,
                to_col,
                from_row,
                to_row,
                additive,
            }));
        }
    ));
    editor.on_media_remove(on_window!(|state, row: i32| {
        state.handle(Msg::Media(MediaMsg::Remove(row)));
    }));
    editor.on_media_remove_selected(on_window!(|state| {
        state.handle(Msg::Media(MediaMsg::RemoveSelected));
    }));
    // Not through the handler macro: nothing here changes the state, and
    // the dialog is asked on a turn of its own with the studio unborrowed;
    // a phone's picker is another screen, and its files come back later.
    // See `host::pick_files_then`.
    editor.on_import_media(|| {
        Shell::with(|shell, _| {
            if shell.studio.borrow().session.is_none() {
                return;
            }
            host::pick_files_then(
                i18n::t("lib.importMedia"),
                Some((
                    i18n::t("common.media"),
                    &[
                        "mp4", "mov", "mkv", "webm", "avi", "m4v", "mp3", "wav", "aac", "m4a",
                        "flac", "ogg", "png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff",
                    ],
                )),
                |studio, _, _, paths| studio.handle(Msg::Media(MediaMsg::Import(paths))),
            );
        });
    });
    // Font files, the way media files come in: the system's picker, and
    // the answer later, on the window's thread.
    editor.on_text_style_default(on_window!(|state| {
        state.use_title_style_for_new();
    }));
    editor.on_import_font(|| {
        Shell::with(|shell, _| {
            if shell.studio.borrow().session.is_none() {
                return;
            }
            host::pick_files_then(
                i18n::t("lib.importFont"),
                Some((i18n::t("lib.fonts"), &["ttf", "otf", "ttc"])),
                |studio, _, _, paths| studio.import_fonts(paths),
            );
        });
    });
    editor.on_media_activate(on_window!(|state, id: i32| {
        state.place_at_playhead(&format!("media:{id}"));
    }));
    editor.on_library_add_text(on_window!(|state, preset: SharedString| {
        state.place_at_playhead(&format!("text:{preset}:Title"));
    }));
    editor.on_library_apply_caption_preset(on_window!(
        |state, preset: SharedString, all: bool| {
            state.apply_caption_preset(preset.as_str(), all);
        }
    ));
    // A figure from the Stickers page, named in the interface's language.
    editor.on_library_add_shape(on_window!(
        |state, shape: SharedString, label: SharedString| {
            state.place_at_playhead(&format!("shape:{shape}:{label}"));
        }
    ));
    // A filter is a layer over a span of the timeline; an effect goes on
    // the selected clip's chain, and audio on the sound's.
    editor.on_library_apply_filter(on_window!(
        |state, id: SharedString, label: SharedString| {
            state.place_filter_layer(id.as_str(), label.as_str());
        }
    ));
    // The Adjustments page: a layer that grades everything beneath it,
    // and a LUT onto the selected clip or, with none, as a layer.
    editor.on_library_add_adjustment(on_window!(|state, label: SharedString| {
        state.place_adjustment_layer(label.as_str());
    }));
    editor.on_library_apply_lut(on_window!(
        |state, id: SharedString, label: SharedString| {
            state.apply_lut(id.as_str(), label.as_str());
        }
    ));
    editor.on_library_audition_filter(on_window!(|state, id: SharedString| {
        state.audition_catalogue(id.as_str());
    }));
    editor.on_library_apply_effect(on_window!(|state, id: SharedString| {
        let (images,videos,_)=media_target_flags();
        state.apply_catalogue_targeted(id.as_str(),true,images,videos);
    }));
    editor.on_library_apply_audio(on_window!(|state, id: SharedString| {
        state.apply_catalogue(id.as_str(), false);
    }));
    editor.on_library_apply_transition(on_window!(|state, id: SharedString| {
        state.apply_transition(id.as_str());
    }));
    editor.on_library_apply_random_transitions(on_window!(|state, mode: i32| {
        state.apply_random_gpu_transitions(mode == 1, mode == 2);
    }));
    editor.on_library_apply_random_effects(on_window!(|state| {
        let (images,videos,selected)=media_target_flags();
        state.apply_random_gpu_effects_scoped(images,videos,selected);
    }));
    editor.on_library_save_template(on_window!(|state| {
        state.save_template();
    }));
    // Nothing borrowed: the dialog is asked on a turn of its own, and the
    // files it answers with are imported there; see `host::after_dialog`.
    editor.on_library_import_lut(Studio::import_lut);
    editor.on_library_reload_packages(on_window!(|state| {
        state.reload_packages(true);
    }));

    // ── the inspector's effect stacks ──
    editor.on_add_effect(on_window!(|state, _audio: bool| {
        state.notify(&i18n::t("lib.pickEffectFilterLibrary"), false);
    }));
    editor.on_remove_effect(on_window!(|state, id: i32| {
        state.remove_effect(id);
    }));

    // ── tabs ──
    editor.on_tab_selected(on_window!(|state, index: i32| {
        let Some(id) = state
            .project()
            .timelines
            .get(index.max(0) as usize)
            .map(|timeline| timeline.id.clone())
        else {
            return;
        };
        state.selection.clear();
        state.apply(concat_project::Command::SelectTimeline { timeline_id: id });
    }));
    editor.on_tab_renamed(on_window!(|state, index: i32, name: SharedString| {
        let trimmed = name.trim().to_string();
        let Some(id) = state
            .project()
            .timelines
            .get(index.max(0) as usize)
            .map(|timeline| timeline.id.clone())
        else {
            return;
        };
        if !trimmed.is_empty() {
            state.apply(concat_project::Command::RenameTimeline {
                timeline_id: id,
                name: trimmed,
            });
        }
    }));
    editor.on_tab_moved(on_window!(|state, from: i32, to: i32| {
        state.move_timeline(from, to);
    }));
    editor.on_tab_added(on_window!(|state| {
        if let Some(id) = state.apply(concat_project::Command::AddTimeline) {
            state.selection.clear();
            state.apply(concat_project::Command::SelectTimeline { timeline_id: id });
        }
    }));
    editor.on_tab_close_requested(on_window!(|state, index: i32| {
        let Some(id) = state
            .project()
            .timelines
            .get(index.max(0) as usize)
            .map(|timeline| timeline.id.clone())
        else {
            return;
        };
        state.selection.clear();
        state.apply(concat_project::Command::RemoveTimeline { timeline_id: id });
    }));

    // ── the tray ──
    editor.on_tool_changed(on_window!(|state, tool: TimelineTool| {
        state.handle(Msg::Timeline(TimelineMsg::ToolChanged(tool)));
    }));
    editor.on_pan_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::PanChanged(on)));
    }));
    editor.on_snap_changed(on_window!(|state, snap: bool| {
        state.handle(Msg::Timeline(TimelineMsg::SnapChanged(snap)));
    }));
    editor.on_magnetic_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::MagneticChanged(on)));
    }));
    editor.on_trim_follow_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::TrimFollowChanged(on)));
    }));
    editor.on_meters_shown_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::MetersShownChanged(on)));
    }));
    editor.on_preview_axis_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::PreviewAxisChanged(on)));
    }));
    editor.on_preview_axis_audio_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Timeline(TimelineMsg::PreviewAxisAudioChanged(on)));
    }));
    // The preview axis: on every move of the pointer across the lanes, so
    // it publishes the lanes and no more, the way a scrub does.
    editor.on_hovered(on_lanes!(|state, seconds: f32| {
        state.handle(Msg::Timeline(TimelineMsg::Hovered(seconds)));
    }));
    editor.on_hover_ended(on_lanes!(|state| {
        state.handle(Msg::Timeline(TimelineMsg::HoverEnded));
    }));
    editor.on_add_track(on_window!(|state, bottom: bool| {
        state.apply(concat_project::Command::AddTrack { bottom });
    }));
    editor.on_delete_selected(on_window!(|state| {
        state.delete_selected();
    }));
    // The tray's buttons; the menu row and ⌘Z reach the same two methods
    // through `app_menu_selected` below.
    editor.on_undo(on_window!(|state| {
        state.undo();
    }));
    editor.on_redo(on_window!(|state| {
        state.redo();
    }));
    editor.on_split(on_window!(|state| {
        let at = state.playhead;
        state.split_at(at, true);
    }));
    editor.on_merge(on_window!(|state| {
        state.merge();
    }));

    // ── the view ──
    editor.on_key_mark_moved(on_lanes!(|state, from: f32, to: f32| {
        state.move_keys_at(from, to);
    }));
    editor.on_scrubbed(on_lanes!(|state, seconds: f32| {
        state.scrub(seconds);
    }));
    editor.on_finger_held(on_lanes!(|state| {
        state.finger_held();
    }));
    editor.on_finger_lifted(on_lanes!(|state| {
        state.finger_lifted();
    }));
    editor.on_scrolled(on_lanes!(|state, seconds: f32| {
        state.handle(Msg::Timeline(TimelineMsg::Scrolled(seconds)));
    }));
    editor.on_zoom(on_lanes!(|state, factor: f32, anchor: f32| {
        state.handle(Msg::Timeline(TimelineMsg::Zoomed { factor, anchor }));
    }));
    editor.on_zoom_to_fit(on_lanes!(|state, width: f32| {
        state.handle(Msg::Timeline(TimelineMsg::ZoomToFit(width)));
    }));
    editor.on_lanes_resized(on_lanes!(|state, width: f32| {
        state.handle(Msg::Timeline(TimelineMsg::Resized(width)));
    }));

    // ── lanes ──
    editor.on_track_flag_changed(on_window!(
        |state, row: i32, visible: bool, muted: bool, locked: bool| {
            state.track_flags(row, visible, muted, locked);
        }
    ));
    editor.on_track_sized(on_window!(|state, row: i32, size: TrackSize| {
        state.set_lane_size(row, size);
    }));
    editor.on_track_removed(on_window!(|state, row: i32| {
        state.ask_remove_track(row);
    }));

    // ── the gestures ──
    editor.on_clip_pressed(move |id:SharedString, additive:bool, edge:i32| {
        Shell::with(|shell,app| {
            {let mut state=shell.studio.borrow_mut();state.drag_copy_held=app.global::<crate::ui::TimelineDrag>().get_copy_modifier();state.clip_pressed(&id,additive,edge);}
            shell.studio.borrow_mut().refresh_art();shell.studio.borrow().publish_lanes(&app,&shell.models);
        });
    });
    app.global::<crate::ui::TimelineDrag>().on_modes_changed(on_window!(|state,ripple:bool,duplicate:bool| {state.drag_modes_changed(ripple,duplicate);}));
    editor.on_clip_cancelled(on_window!(|state| {state.clip_cancelled();}));
    editor.on_clip_split(on_lanes!(|state, id: SharedString, seconds: f32| {
        state.split_clip_at(&id, seconds);
    }));
    editor.on_clip_volume(on_lanes!(|state, id: SharedString, gain: f32| {
        state.clip_volume(&id, gain);
    }));
    editor.on_save_frame(on_window!(|state| {
        state.save_frame_as();
    }));
    editor.on_clip_dragged(on_lanes!(|state, seconds: f32, pixels: f32| {
        state.clip_dragged(seconds, pixels);
    }));
    editor.on_clip_released(on_window!(|state| {
        state.clip_released();
    }));

    // ── drag and drop from the library ──
    editor.on_drag_hovered(on_lanes!(|state,
                                      payload: SharedString,
                                      seconds: f32,
                                      y: f32| {
        let row = state.row_at(y);
        state.drop = state.plan(payload.as_str(), seconds, row);
    }));
    editor.on_dropped(on_window!(|state,
                                  payload: SharedString,
                                  seconds: f32,
                                  y: f32| {
        state.drop = None;
        let row = state.row_at(y);
        if let Some(plan) = state.plan(payload.as_str(), seconds, row) {
            state.place(&plan);
        }
    }));
    editor.on_band_selected(on_lanes!(
        |state, from: f32, to: f32, from_y: f32, to_y: f32, additive: bool| {
            // As on a clip press and on the stage: what the inspector still
            // holds for the selection lands before the selection moves. A
            // press on the lanes' floor is the commonest way out of a
            // title's text box, and it ends here, with the band it drew -
            // usually an empty one that clears the selection.
            state.flush_commit();
            let (from_row, to_row) = (state.row_at(from_y), state.row_at(to_y));
            let caught: Vec<String> = state
                .timeline()
                .clips
                .iter()
                .filter(|clip| {
                    let row = state.row_of(&clip.track_id);
                    row >= from_row
                        && row <= to_row
                        && (clip.start + clip.duration) as f32 >= from
                        && clip.start as f32 <= to
                        && !state.locked(&clip.track_id)
                })
                .map(|clip| clip.id.clone())
                .collect();
            if additive {
                for id in caught {
                    if !state.selection.contains(&id) {
                        state.selection.push(id);
                    }
                }
            } else {
                state.selection = caught;
                state.transition_selected = None;
            }
        }
    ));

    // ── the inspector ──
    // The keyframe cluster. Its own global, so a row deep in a panel does
    // not have to be threaded a callback to reach here.
    {
        let keys = app.global::<Keyframes>();
        keys.on_remember_animation(on_lanes!(|state,index:i32,strength:f32,duration:f32,speed:f32,intensity:f32,canvas:CanvasMotionData| {
            state.remember_animation_settings(index,strength,duration,speed,intensity,canvas);
        }));
        keys.on_reset_animation(on_lanes!(|state,index:i32| {
            state.reset_animation_settings(index);
        }));
        keys.on_has_animation_default(move |index| {
            let mut exists=false;
            Shell::with(|shell,_| {
                exists=shell.studio.borrow().has_animation_default(index);
            });
            exists
        });
        keys.on_load_animation(move |index| {
            let mut result=AnimationInspectorData::default();
            Shell::with(|shell,_| {
                let settings=shell.studio.borrow().animation_settings(index);
                let c=settings.canvas;
                let (start_x,start_y)=c.start_anchor.map_or((0.0,0.0),|v|(v[0],v[1]));
                let (end_x,end_y)=c.end_anchor.map_or((0.0,0.0),|v|(v[0],v[1]));
                result=AnimationInspectorData {strength:settings.physics.strength as f32,duration:settings.physics.duration as f32,
                    speed:settings.physics.speed as f32,intensity:settings.physics.intensity as f32,
                    canvas:CanvasMotionData {start_scale:c.start_scale as f32,end_scale:c.end_scale as f32,duration:c.duration_seconds as f32,
                        delay:c.delay_seconds as f32,entry_seconds:c.entry_seconds as f32,hold_seconds:c.hold_seconds as f32,exit_seconds:c.exit_seconds as f32,
                        focus_middle_x:c.focus_middle[0] as f32,focus_middle_y:c.focus_middle[1] as f32,focus_hold:c.focus_hold_seconds as f32,
                        speed:c.speed as f32,strength:c.strength as f32,intensity:c.intensity as f32,blur:c.blur as f32,reverse:c.reverse,
                        custom_anchors:c.start_anchor.is_some() || c.end_anchor.is_some(),start_x:start_x as f32,start_y:start_y as f32,end_x:end_x as f32,end_y:end_y as f32,
                        easing:if c.easing==concat_project::canvas_motion::MotionEasing::Linear {0}else{1},seed:c.seed.min(i32::MAX as u64) as i32,corner_radius:settings.corners as f32},
                };
            });
            result
        });
        keys.invoke_restore_current();
        keys.on_clear_generated_motion(on_lanes!(|state,images:bool,videos:bool,selected_only:bool| {
            state.clear_generated_motion_bulk(images,videos,selected_only);
        }));
        keys.on_apply_animation_favourites(on_lanes!(|state,pool:ModelRc<bool>,images:bool,videos:bool,selected:bool| {
            let indexes=(0..pool.row_count()).filter(|i|pool.row_data(*i)==Some(true)).map(|i|i as i32).collect();
            state.apply_animation_favourites(indexes,images,videos,selected);
        }));
        keys.on_toggle(on_lanes!(|state, field: ClipField| {
            state.toggle_key(field);
        }));
        keys.on_step(on_lanes!(|state, field: ClipField, delta: i32| {
            state.step_key(field, delta);
        }));
        keys.on_clear(on_lanes!(|state, field: ClipField| {
            state.clear_keys_on(field);
        }));
        keys.on_apply_motion_preset(on_lanes!(|state, field: ClipField| {
            state.apply_motion_preset(field);
        }));
        keys.on_apply_random_motion(on_lanes!(|state| {
            state.apply_random_gpu_motion();
        }));
        keys.on_apply_physics_motion(on_lanes!(|state, index: i32, strength: f32, duration: f32, speed: f32, intensity: f32| {
            state.apply_physics_motion(index, strength, duration, speed, intensity);
        }));
        keys.on_apply_physics_bulk(on_lanes!(|state, preset: i32, mode: i32,
            images: bool, videos: bool, selected_only: bool,
            strength: f32, duration: f32, speed: f32, intensity: f32| {
            state.apply_physics_bulk(preset, mode, images, videos, selected_only,
                strength, duration, speed, intensity);
        }));
        keys.on_apply_canvas_motion(on_lanes!(|state, preset: i32, mode: i32, images: bool,
            videos: bool, selected_only: bool, settings: CanvasMotionData| {
            state.apply_canvas_motion_bulk(preset, mode, images, videos, selected_only, settings);
        }));
        keys.on_toggle_physics_favourite(on_lanes!(|state, index: i32| {
            state.toggle_physics_favourite(index);
        }));
        keys.on_apply_random_favourite_motion(on_lanes!(|state| {
            state.apply_random_favourite_gpu_motion();
        }));
        // Animations tab and inspector call the same owner-scoped backend.
        keys.on_apply_bulk_motion(on_lanes!(|state, mode: i32, images: bool,
            video_clips: bool, selected_only: bool| {
            state.apply_bulk_gpu_motion(mode, images, video_clips, selected_only);
        }));
        keys.on_apply_bulk_motion_preset(on_lanes!(|state, mode: i32, preset: i32,
            images: bool, video_clips: bool, selected_only: bool| {
            state.apply_bulk_gpu_motion_with_preset(
                mode, preset, images, video_clips, selected_only,
            );
        }));
        keys.on_layout_set(on_lanes!(|state, field:ClipField, value:f32| {
            state.layout_set(field,value);
        }));
        keys.on_layout_frame(on_lanes!(|state,fill:bool| {state.layout_frame(fill);}));
        keys.on_apply_adjustment(on_lanes!(|state, page:i32, images:bool, videos:bool, selected:bool,
            scale:f32,x:f32,y:f32,rotation:f32,opacity:f32,flip_h:bool,flip_v:bool| {
            state.apply_adjustment_bulk(page,images,videos,selected,scale,x,y,rotation,opacity,flip_h,flip_v);
        }));
        keys.on_apply_bulk_transform(on_lanes!(|state, mode: i32, images: bool,
            video_clips: bool, selected_only: bool, scale: f32, x: f32, y: f32, rotation: f32| {
            state.apply_bulk_gpu_transform(
                mode, images, video_clips, selected_only, scale, x, y, rotation,
            );
        }));
        // The same three verbs for the Adjust panel's knobs, which are
        // named rather than enumerated.
        keys.on_toggle_param(on_lanes!(|state, key: SharedString| {
            state.toggle_adjust_key(key.as_str());
        }));
        keys.on_step_param(on_lanes!(|state, key: SharedString, delta: i32| {
            state.step_adjust_key(key.as_str(), delta);
        }));
        keys.on_clear_param(on_lanes!(|state, key: SharedString| {
            state.clear_adjust_keys(key.as_str());
        }));
    }
    // The background behind the main picture, and Fit and Fill: a global
    // of their own, for the Details panel, the Video panel and the phone.
    {
        let backdrop = app.global::<Backdrop>();
        backdrop.on_set_mode(on_lanes!(|state, clip: bool, mode: i32| {
            state.backdrop_set_mode(clip, mode);
        }));
        backdrop.on_set_amount(on_lanes!(|state, clip: bool, amount: f32| {
            state.backdrop_set_amount(clip, amount);
        }));
        backdrop.on_set_colour(on_lanes!(|state, clip: bool, colour: slint::Color| {
            state.backdrop_set_colour(clip, colour);
        }));
        backdrop.on_set_still(on_lanes!(|state, clip: bool, row: i32| {
            state.backdrop_set_still(clip, row);
        }));
        backdrop.on_apply_bulk(on_lanes!(|state, mode:i32, amount:f32, colour:slint::Color, images:bool, videos:bool, selected_only:bool| {
            state.apply_bulk_canvas_background(mode,amount,colour,images,videos,selected_only);
        }));
        backdrop.on_apply_to_all(on_lanes!(|state| {
            state.backdrop_apply_to_all();
        }));
        backdrop.on_frame(on_lanes!(|state, fill: bool| {
            state.frame_selected(fill);
        }));
    }
    // The Keyframes panel's verbs; its rows go out with every publish.
    {
        let editor = app.global::<KeyEditor>();
        editor.on_jump(on_lanes!(|state, at: f32| {
            state.jump_to_key(at);
        }));
        editor.on_move_key(on_lanes!(|state,
                                      field: ClipField,
                                      param: SharedString,
                                      from: f32,
                                      to: f32| {
            state.move_key(field, param.as_str(), from, to);
        }));
        editor.on_set_ease(on_lanes!(|state,
                                      field: ClipField,
                                      param: SharedString,
                                      at: f32,
                                      x1: f32,
                                      y1: f32,
                                      x2: f32,
                                      y2: f32| {
            state.set_key_ease(field, param.as_str(), at, [x1, y1, x2, y2]);
        }));
        editor.on_drag_key(on_lanes!(
            |state, field: ClipField, param: SharedString, from: f32, to: f32, value: f32| {
                state.drag_key(field, param.as_str(), from, to, value);
            }
        ));
        editor.on_step_all(on_lanes!(|state, delta: i32| {
            state.step_any_key(delta);
        }));
        editor.on_clear_all(on_lanes!(|state| {
            state.clear_all_keys();
        }));
    }

    // The effect libraries' search, shelves and stars. Rust does the
    // filtering, so the panel only reports what was pressed.
    {
        let library = app.global::<Library>();
        library.on_query_changed(on_window!(|state, shelf: i32, text: SharedString| {
            state.library_query(shelf, &text);
        }));
        library.on_group_changed(on_window!(|state, shelf: i32, index: i32| {
            state.library_group(shelf, index);
        }));
        library.on_favourites_changed(on_window!(|state, shelf: i32, on: bool| {
            state.library_favourites(shelf, on);
        }));
        library.on_category_changed(on_window!(|state, shelf: i32, category: SharedString| {
            state.library_category(shelf, &category);
        }));
        library.on_favourite(on_window!(|state, id: SharedString, on: bool| {
            state.library_favourite(&id, on);
        }));
    }

    editor.on_clip_set(on_lanes!(|state, field: ClipField, value: f32| {
        state.clip_set(field, value);
    }));
    editor.on_clip_set_text(on_lanes!(
        |state, field: ClipTextField, value: SharedString| {
            state.clip_set_text(field, value.as_str());
        }
    ));
    editor.on_clip_set_colour(on_lanes!(
        |state, field: ClipTextField, value: slint::Color| {
            state.clip_set_colour(field, value);
        }
    ));
    // Lanes only: the commit is held and lands with a full publish of its
    // own once the control's moves pause; see `Studio::clip_commit`.
    editor.on_clip_commit(on_lanes!(|state| {
        state.clip_commit();
    }));

    // ── the chains ──
    editor.on_chain_add(on_window!(|state, audio: bool, id: SharedString| {
        let (images,videos,_)=media_target_flags();
        state.apply_catalogue_targeted(id.as_str(),!audio,images,videos);
    }));
    editor.on_chain_toggle(on_window!(|state, audio: bool, index: i32| {
        state.chain_toggle(audio, index);
    }));
    editor.on_chain_move_by(on_window!(|state, audio: bool, index: i32, delta: i32| {
        state.chain_move(audio, index, delta);
    }));
    editor.on_chain_remove(on_window!(|state, audio: bool, index: i32| {
        state.chain_remove(audio, index);
    }));
    editor.on_chain_set_param(on_lanes!(
        |state, audio: bool, index: i32, key: SharedString, value: f32| {
            state.chain_set_param(audio, index, key.as_str(), value);
        }
    ));

    editor.on_chain_set_colour(on_lanes!(
        |state, index: i32, key: SharedString, value: slint::Color| {
            state.chain_set_colour(index, key.as_str(), value);
        }
    ));

    editor.on_adjust_set(on_lanes!(|state, key: SharedString, value: f32| {
        state.adjust_set(key.as_str(), value);
    }));
    editor.on_curve_point(on_lanes!(|state,
                                     entry: i32,
                                     key: SharedString,
                                     point: i32,
                                     x: f32,
                                     y: f32| {
        if entry < 0 {
            state.adjust_curve_point(key.as_str(), point, x, y);
        } else {
            state.chain_curve_point(entry, key.as_str(), point, x, y);
        }
    }));
    editor.on_curve_remove(on_lanes!(
        |state, entry: i32, key: SharedString, point: i32| {
            if entry < 0 {
                state.adjust_curve_remove(key.as_str(), point);
            } else {
                state.chain_curve_remove(entry, key.as_str(), point);
            }
        }
    ));

    // ── the monitor ──
    editor.on_seek(on_lanes!(|state, seconds: f32| {
        state.stop_glide();
        state.pause();
        state.seek(seconds);
    }));
    editor.on_step_frames(on_lanes!(|state, frames: f32| {
        state.stop_glide();
        state.pause();
        let fps = state.frame_rate().round().max(1.0);
        let at = (state.playhead * fps).round() + frames;
        state.seek(at / fps);
    }));
    editor.on_ratio_changed(on_window!(|state, index: i32| {
        state.set_output((index.max(0) as usize).min(OUTPUTS.len() - 1));
    }));
    editor.on_quality_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Monitor(MonitorMsg::QualityChanged(index)));
    }));
    editor.on_play_toggled(on_window!(|state| {
        state.play_toggle();
    }));
    app.global::<FontSearch>()
        .on_query_changed(on_window!(|state, text: SharedString| {
            state.font_query = text.to_string();
        }));
    app.global::<Meters>().on_clip_cleared({
        let weak = app.as_weak();
        move || {
            if let Some(app) = weak.upgrade() {
                crate::host::Shell::with(|shell, _| {
                    shell.studio.borrow_mut().meters.clear_clip(&app);
                });
            }
        }
    });
    app.global::<SourcePreview>().on_closed(on_window!(|state| {
        state.close_source();
    }));

    // ── the library's Audio page ──
    {
        let sounds = app.global::<Sounds>();
        // The picker answers on a turn of its own, the way the bin's does;
        // see `host::pick_files_then`.
        sounds.on_import(|| {
            host::pick_files_then(
                i18n::t("sounds.importSounds"),
                Some((i18n::t("common.audio"), &concat_host::sounds::EXTENSIONS)),
                |studio, _, _, paths| studio.handle(Msg::Sounds(SoundsMsg::Import(paths))),
            );
        });
        sounds.on_preview(on_window!(|state, id: SharedString| {
            state.handle(Msg::Sounds(SoundsMsg::Preview(id.to_string())));
        }));
        sounds.on_add(on_window!(|state, id: SharedString| {
            state.handle(Msg::Sounds(SoundsMsg::Add(id.to_string())));
        }));
        sounds.on_remove(on_window!(|state, id: SharedString| {
            state.handle(Msg::Sounds(SoundsMsg::Remove(id.to_string())));
        }));
        sounds.on_shown(on_window!(|state, on: bool| {
            state.handle(Msg::Sounds(SoundsMsg::Shown(on)));
        }));
        // Voices: Text to speech on the voice or style clicked.
        // Concat's own sounds: the list asked for by a click, never at
        // startup.
        sounds.on_load_concat(on_window!(|state| {
            state.handle(Msg::Sounds(SoundsMsg::LoadConcat));
        }));
        sounds.on_concat_category_changed(on_window!(|state, index: i32| {
            state.handle(Msg::Sounds(SoundsMsg::ConcatCategory(index)));
        }));
        sounds.on_voice_open(on_window!(|state, id: SharedString| {
            state.open_voice(id.as_str());
        }));
        sounds.on_voice_remove(on_window!(|state, id: SharedString| {
            state.forget_voice(id.as_str());
        }));
    }
    // Text to speech keeps the sample it reads in as a voice of the
    // user's own, with a name and a picture.
    app.on_speech_save_name_edited(on_window!(|state, name: SharedString| {
        state.handle(Msg::Speech(SpeechMsg::SaveNameEdited(name.to_string())));
    }));
    app.on_speech_pick_picture(|| {
        host::pick_files_then(
            i18n::t("speech.picture"),
            Some((
                i18n::t("speech.picture"),
                &concat_host::voices::PICTURE_EXTENSIONS,
            )),
            |studio, _, _, paths| {
                if let Some(path) = paths.into_iter().next() {
                    studio.handle(Msg::Speech(SpeechMsg::PicturePicked(path)));
                }
            },
        );
    });
    app.on_speech_save_voice(on_window!(|state| {
        state.handle(Msg::Speech(SpeechMsg::SaveVoice));
    }));
    app.on_speech_clear_style(on_window!(|state| {
        state.handle(Msg::Speech(SpeechMsg::ClearStyle));
    }));
    {}
    app.global::<SourcePreview>()
        .on_seek(on_window!(|state, seconds: f32| {
            state.source_seek(f64::from(seconds));
        }));

    // ── the stage ──
    editor.on_stage_pressed(on_window!(|state, x: f32, y: f32, additive: bool| {
        state.stage_pressed(x, y, additive);
    }));
    editor.on_stage_grip_pressed(on_window!(
        |state, id: SharedString, grip: i32, x: f32, y: f32| {
            state.stage_grip_pressed(id.as_str(), grip, x, y);
        }
    ));
    editor.on_stage_dragged(on_lanes!(|state, x: f32, y: f32, snap: bool| {
        state.stage_dragged(x, y, snap);
    }));
    editor.on_stage_released(on_window!(|state| {
        state.stage_released();
    }));

    // ── the cutout ──
    editor.on_cutout_mode(on_window!(|state, mode: i32| {
        state.cutout_mode(mode);
    }));
    editor.on_cutout_tool(on_window!(|state, index: i32| {
        state.cutout_tool(index);
    }));
    editor.on_cutout_size(on_lanes!(|state, size: f32| {
        state.cutout_size(size);
    }));
    editor.on_cutout_painting(on_window!(|state, on: bool| {
        state.cutout_painting(on);
    }));
    editor.on_cutout_subject(on_window!(|state, index: i32| {
        state.cutout_subject(index);
    }));
    editor.on_cutout_clear(on_window!(|state| {
        state.cutout_clear();
    }));
    editor.on_clip_enhance_set(on_window!(|state, on: bool| {
        state.clip_enhance_set(on);
    }));
    editor.on_clip_enhance_size(on_window!(|state, index: i32| {
        state.clip_enhance_size(index);
    }));
    editor.on_clip_enhance_strength(on_window!(|state, strength: f32| {
        state.clip_enhance_strength(strength);
    }));
    editor.on_clip_enhance_preview(on_window!(|state| {
        state.clip_enhance_preview();
    }));
    editor.on_clip_enhance_run(on_window!(|state| {
        state.clip_enhance_run();
    }));
    editor.on_transition_remove(on_window!(|state| {
        state.remove_transition();
    }));
    editor.on_transition_duration_set(on_window!(|state, seconds: f32| {
        state.set_transition_duration(seconds as f64);
    }));
    editor.on_transition_duration_all(on_window!(|state, seconds: f32| {
        state.apply_transition_duration_to_all(seconds as f64);
    }));
    editor.on_transition_intensity_set(on_window!(|state, amount: f32| {
        state.set_transition_intensity(amount as f64);
    }));
    editor.on_transition_draft_duration_set(on_window!(|state, seconds: f32| {
        state.set_transition_draft_duration(seconds as f64);
    }));
    editor.on_transition_draft_intensity_set(on_window!(|state, amount: f32| {
        state.set_transition_draft_intensity(amount as f64);
    }));
    editor.on_transition_default_save(on_window!(|state, seconds: f32, amount: f32| {
        state.save_transition_default(seconds as f64, amount as f64);
    }));
    editor.on_transition_default_reset(on_window!(|state| {
        state.reset_transition_default();
    }));

    // ── the context menu ──
    editor.on_clip_context(on_window!(|state, id: SharedString| {
        state.menu_token += 1;
        state.menu_media = None;
        if state.clip(id.as_str()).is_none() {
            state.menu_target = None;
            return;
        }
        if !state.selection.iter().any(|held| held == id.as_str()) {
            state.selection = vec![id.to_string()];
        }
        state.menu_target = Some(id.to_string());
    }));
    // The bin's cards share the menu's rows and token with the clips; which
    // one was asked for is what the two targets below remember.
    editor.on_media_context(on_window!(|state, row: i32| {
        state.menu_token += 1;
        state.menu_target = None;
        state.menu_media = state
            .media
            .by_row(state.project(), row)
            .map(|item| item.id.clone());
    }));
    editor.on_menu_selected(on_window!(|state, action: SharedString| {
        if let Some(id) = state.menu_media.clone() {
            state.media_action(&id, action.as_str());
            return;
        }
        // The clip the menu was opened on; failing that, the one clip that
        // is selected, which is what the menu was showing anyway.
        let target = state.menu_target.clone().or_else(|| state.sole_selection());
        if let Some(id) = target {
            // The captions sheet is a pane of its own, so its row goes
            // through the window's messages rather than the clip's verbs:
            // the sheet opens on this clip's voice, "Use voice from
            // timeline" on, and the clip is selected so it shows which.
            if action.as_str() == "captions" {
                state.flush_commit();
                state.transition_selected = None;
                state.selection = vec![id.clone()];
                state.handle(Msg::Captions(CaptionsMsg::OpenOnClip(id)));
                return;
            }
            state.clip_action(&id, action.as_str());
        }
    }));

    // ── the keyboard ──
    //
    // A press on any pane's floor takes focus back from the field that had
    // it; see Editor.blur. The chords that are also menu rows go through the
    // menu's handler, so the key and the row cannot come apart.
    editor.on_blur(|| Shell::with(|_, app| app.invoke_blur()));
    // A field that is done being typed into - Enter, Escape - releases the
    // focus the same way, rather than clearing it: a cleared focus is a
    // window where no key reaches anything. See Focus in util.slint.
    app.global::<Focus>()
        .on_release(|| Shell::with(|_, app| app.invoke_blur()));
    editor.on_shortcut(move |action: SharedString| match action.as_str() {
        "import" | "export" | "settings" | "zoom-in" | "zoom-out" | "start" | "end" | "snap"
        | "magnetic" | "pan" | "preview-axis" => {
            Shell::with(|_, app| app.invoke_app_menu_selected(action.clone()));
        }
        _ => Shell::with(|shell, app| {
            shell.studio.borrow_mut().shortcut(action.as_str());
            shell.studio.borrow_mut().refresh_art();
            shell.studio.borrow().publish(&app, &shell.models);
        }),
    });
    // Every key the window did not give to a field or a binding, raw:
    // the chord, what it resolves to and where that dispatches are one
    // table now (keymap.rs), and the capture field in Settings ›
    // Shortcuts listens on this same door — binding instead of
    // dispatching while a row holds the next key. True means the key
    // was used and the scope keeps it; false lets it go on up, to a
    // popup's Escape or to nothing at all.
    editor.on_key_chord(move |text, shift, alt, control, meta, on_start| {
        let chord = keymap::chord(text.as_str(), shift, alt, control, meta);
        let mut handled = false;
        Shell::with(|shell, app| {
            // ── the capture field ──
            if shell.studio.borrow().settings.capturing.is_some() {
                // Every key while a row listens goes to the row. A
                // modifier on its own is not a chord: it waits for the
                // key it is holding out for, which is swallowed here.
                handled = true;
                let Some(chord) = chord else {
                    return;
                };
                // Escape declines; the row goes back to its chord.
                if chord == "escape" {
                    let mut state = shell.studio.borrow_mut();
                    state.settings.capturing = None;
                    state.publish(&app, &shell.models);
                    return;
                }
                // The menu bar's and the window's own chords stay
                // bindings in app.slint and would fire before this
                // callback ever ran in the editor; a row that claimed
                // one would be a row telling a lie.
                if keymap::reserved(&chord) {
                    let mut state = shell.studio.borrow_mut();
                    state.notify(&i18n::t("shortcuts.reserved"), true);
                    state.publish(&app, &shell.models);
                    return;
                }
                let mut state = shell.studio.borrow_mut();
                let Some(id) = state.settings.capturing.clone() else {
                    return;
                };
                state.settings.capturing = None;
                // The chord is one action's: whoever held it loses it,
                // and the toast says which row is now unbound.
                let lost = keymap::bind(&mut state.prefs, &id, &chord);
                state.prefs.save(&state.host.dirs);
                if let Some(lost) = lost {
                    state.notify(
                        &i18n::tf("shortcuts.unboundNow", &[&keymap::name(lost)]),
                        false,
                    );
                }
                state.publish(&app, &shell.models);
                return;
            }

            // ── dispatch ──
            let Some(chord) = chord else {
                return;
            };
            if chord == "escape" && matches!(shell.studio.borrow().gesture, crate::studio::Gesture::Move {..} | crate::studio::Gesture::Trim {..} | crate::studio::Gesture::TransitionResize {..}) {
                shell.studio.borrow_mut().clip_cancelled();shell.studio.borrow().publish(&app,&shell.models);handled=true;return;
            }
            if on_start {
                // The launch screen has no project to act on. Capture
                // is handled above, so its keys work there too.
                return;
            }
            let action = {
                let state = shell.studio.borrow();
                keymap::resolve(&state.prefs, &chord)
            };
            let Some(action) = action else {
                return;
            };
            handled = true;
            match action {
                // The playhead, straight: these were never menu rows and
                // their handlers are a seek apiece.
                "play" => {
                    shell.studio.borrow_mut().play_toggle();
                    shell.studio.borrow_mut().refresh_art();
                    shell.studio.borrow().publish(&app, &shell.models);
                }
                step @ ("step-back" | "step-back-10" | "step-forward" | "step-forward-10") => {
                    let frames: f32 = match step {
                        "step-back" => -1.0,
                        "step-back-10" => -10.0,
                        "step-forward" => 1.0,
                        _ => 10.0,
                    };
                    {
                        let mut state = shell.studio.borrow_mut();
                        state.pause();
                        let fps = state.frame_rate().round().max(1.0);
                        let at = (state.playhead * fps).round() + frames;
                        state.seek(at / fps);
                    }
                    shell.studio.borrow_mut().refresh_art();
                    shell.studio.borrow().publish_lanes(&app, &shell.models);
                }
                // The chords that are also menu rows go through the
                // menu's own handler, so the key and the row cannot come
                // apart — the same door on_shortcut keeps open.
                "save" | "undo" | "redo" | "import" | "export" | "settings" | "zoom-in"
                | "zoom-out" | "start" | "end" | "snap" | "pan" | "preview-axis" | "delete" => {
                    app.invoke_app_menu_selected(SharedString::from(action));
                }
                // The rest: the editor's own table, the way the phone's
                // buttons and the tray reach it.
                _ => {
                    shell.studio.borrow_mut().shortcut(action);
                    shell.studio.borrow_mut().refresh_art();
                    shell.studio.borrow().publish(&app, &shell.models);
                }
            }
        });
        handled
    });

    // ── the project sheet ──
    editor.on_modify_project(on_window!(|state| {
        state.handle(Msg::Project(ProjectMsg::Open));
    }));
    app.on_project_closed(on_window!(|state| {
        state.handle(Msg::Project(ProjectMsg::Close));
    }));
    app.on_project_name_edited(on_window!(|state, name: SharedString| {
        state.handle(Msg::Project(ProjectMsg::NameEdited(name.to_string())));
    }));
    app.on_project_size_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Project(ProjectMsg::SizeChanged(index)));
    }));
    app.on_project_rate_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Project(ProjectMsg::RateChanged(index)));
    }));
    app.on_project_color_space_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Project(ProjectMsg::ColorSpaceChanged(index)));
    }));
    app.on_project_custom_width_edited(on_window!(|state, width: f32| {
        state.handle(Msg::Project(ProjectMsg::CustomWidth(width)));
    }));
    app.on_project_custom_height_edited(on_window!(|state, height: f32| {
        state.handle(Msg::Project(ProjectMsg::CustomHeight(height)));
    }));
    app.on_project_custom_fps_edited(on_window!(|state, fps: f32| {
        state.handle(Msg::Project(ProjectMsg::CustomFps(fps)));
    }));
    app.on_project_apply(on_window!(|state| {
        state.handle(Msg::Project(ProjectMsg::Apply));
    }));

    app.on_confirm_accepted(on_window!(|state| {
        state.confirm_accepted();
    }));
    app.on_confirm_dismissed(on_window!(|state| {
        state.confirm = None;
    }));

    app.on_relink_all(on_window!(|state| {
        state.handle(Msg::Relink(RelinkMsg::RelinkAll));
    }));
    app.on_relink_dismiss(on_window!(|state| {
        state.handle(Msg::Relink(RelinkMsg::Dismiss));
    }));

    #[cfg(windows)]
    let _gpu_queue_timer = {
        app.global::<GpuCompatibility>().set_enabled(true);
        cloud_gpu::wire(&app)
    };

    // ── the dialogs ──
    app.on_export_clicked(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Open));
    }));
    app.on_open_settings(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::Open));
    }));
    // The theme is one bool on the Theme global, and every colour in the
    // tree is a binding away from it; it is also remembered.
    app.on_settings_theme_changed({
        move |dark| {
            log::info!("settings: theme {}", if dark { "dark" } else { "light" });
            Shell::with(|shell, app| {
                app.global::<Theme>().set_dark(dark);
                let mut studio = shell.studio.borrow_mut();
                studio.prefs.dark = Some(dark);
                studio.prefs.save(&studio.host.dirs);
            });
        }
    });
    // The accent is the same shape of thing: one int on the Theme global,
    // remembered under the name the global lists it by.
    app.on_settings_accent_changed({
        move |index| {
            log::info!("settings: accent {index}");
            Shell::with(|shell, app| {
                let theme = app.global::<Theme>();
                let names = theme.get_accent_names();
                let Some(name) = usize::try_from(index)
                    .ok()
                    .and_then(|row| slint::Model::row_data(&names, row))
                else {
                    return;
                };
                theme.set_accent_choice(index);
                let mut studio = shell.studio.borrow_mut();
                studio.prefs.accent = Some(prefs::Preferences::accent_id(&name));
                studio.prefs.save(&studio.host.dirs);
            });
        }
    });
    // A picked colour is remembered as its hex, which is what tells it from
    // a name when the file is read back. Every drag on the picker's square
    // lands here, so the file is written often for a moment; it is small.
    app.on_settings_accent_custom_changed({
        move |colour| {
            Shell::with(|shell, app| {
                let theme = app.global::<Theme>();
                let count = slint::Model::row_count(&theme.get_accent_names());
                theme.set_accent_custom(colour);
                theme.set_accent_choice(count as i32);
                let mut studio = shell.studio.borrow_mut();
                studio.prefs.accent = Some(format::hex_of(colour));
                studio.prefs.save(&studio.host.dirs);
            });
        }
    });
    // The playhead's colour is the same shape of thing as the custom accent:
    // a colour on the Theme global, remembered as its hex, behind a flag
    // that says it is on. A reset clears the flag and forgets the hex, so
    // the palette's own comes back and the file no longer names one.
    app.on_settings_playhead_changed({
        move |colour| {
            Shell::with(|shell, app| {
                let theme = app.global::<Theme>();
                theme.set_playhead_custom(colour);
                theme.set_playhead_is_custom(true);
                let mut studio = shell.studio.borrow_mut();
                studio.prefs.playhead = Some(format::hex_of(colour));
                studio.prefs.save(&studio.host.dirs);
            });
        }
    });
    app.on_settings_playhead_reset({
        move || {
            Shell::with(|shell, app| {
                app.global::<Theme>().set_playhead_is_custom(false);
                let mut studio = shell.studio.borrow_mut();
                studio.prefs.playhead = None;
                studio.prefs.save(&studio.host.dirs);
            });
        }
    });
    // The interface scale is the one appearance choice a property cannot
    // express: it is the window's scale factor. The window that is up is
    // rescaled in place to the display's own scale times the choice - the
    // same physical extent laid out again in logical pixels, so the
    // interface grows within the window rather than being clipped by it.
    // Nothing is rebuilt; the window keeps its size on screen. The row
    // reads its value off the Theme global, so that is set here too.
    // https://github.com/jub0t/Concat/issues/320
    app.on_settings_scale_changed({
        move |index| {
            log::info!("settings: ui scale {index}");
            Shell::with(|shell, app| {
                let percent = prefs::Preferences::scale_at(usize::try_from(index).unwrap_or(0));
                {
                    let mut studio = shell.studio.borrow_mut();
                    studio.prefs.ui_scale = percent;
                    studio.prefs.save(&studio.host.dirs);
                }
                app.global::<Theme>().set_ui_scale(index);
                platform::set_ui_scale(percent);
                platform::apply_ui_scale(app.window());
            });
        }
    });
    app.on_export_closed(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Close));
    }));
    app.on_settings_closed(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::Close));
    }));
    app.on_export_name_edited(on_window!(|state, name: SharedString| {
        state.handle(Msg::Export(ExportMsg::NameEdited(name.to_string())));
    }));
    app.on_export_rate_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Export(ExportMsg::RateChanged(index)));
    }));
    app.on_export_codec_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Export(ExportMsg::CodecChanged(index)));
    }));
    app.on_export_hdr_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Export(ExportMsg::HdrChanged(on)));
    }));
    app.on_export_ten_bit_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Export(ExportMsg::TenBitChanged(on)));
    }));
    app.on_export_bitrate_changed(on_window!(|state, mbps: f32| {
        state.handle(Msg::Export(ExportMsg::BitrateChanged(mbps)));
    }));
    app.on_export_again(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Again));
    }));
    app.on_export_browse(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Browse));
    }));
    app.on_export_reveal(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Reveal));
    }));
    app.on_export_cancel(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Cancel));
    }));
    app.on_export_start(on_window!(|state| {
        state.handle(Msg::Export(ExportMsg::Start));
    }));

    // ── settings ──
    app.on_settings_page_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Settings(SettingsMsg::PageChanged(index)));
    }));
    // The Shortcuts page: a row asked for the next keys, and the two
    // ways back — one row as it ships, or all of them.
    app.on_settings_shortcut_capture(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ShortcutCapture(id.to_string())));
    }));
    app.on_settings_shortcut_reset(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ShortcutReset(id.to_string())));
    }));
    app.on_settings_shortcut_reset_all(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::ShortcutResetAll));
    }));
    app.on_settings_show_log(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::ShowLog));
    }));
    app.on_settings_language_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Settings(SettingsMsg::LanguageChanged(index)));
    }));
    app.on_settings_log_level_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Settings(SettingsMsg::LogLevelChanged(index)));
    }));
    app.on_settings_playhead_stops_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Settings(SettingsMsg::PlayheadStopsChanged(on)));
    }));
    app.on_settings_proxies_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Settings(SettingsMsg::ProxiesChanged(on)));
    }));
    app.on_settings_gpu_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Settings(SettingsMsg::GpuChanged(on)));
    }));
    app.on_settings_magnetic_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Settings(SettingsMsg::MagneticChanged(on)));
    }));
    app.on_settings_download_source_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Settings(SettingsMsg::DownloadSourceChanged(index)));
    }));
    app.on_settings_download_base_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::DownloadBaseEdited(
            text.to_string(),
        )));
    }));
    app.on_settings_server_enabled_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Settings(SettingsMsg::ServerEnabledChanged(on)));
    }));
    app.on_settings_server_host_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ServerHostEdited(
            text.to_string(),
        )));
    }));
    app.on_settings_server_port_edited(on_window!(|state, port: i32| {
        state.handle(Msg::Settings(SettingsMsg::ServerPortEdited(port)));
    }));
    app.on_settings_server_token_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ServerTokenEdited(
            text.to_string(),
        )));
    }));
    app.on_settings_server_token_generated(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::ServerTokenGenerated));
    }));
    app.on_settings_check_updates(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::CheckUpdates));
    }));
    app.on_settings_version_chosen(on_window!(|state, index: i32| {
        state.handle(Msg::Settings(SettingsMsg::VersionChosen(index)));
    }));
    app.on_settings_install_version(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::InstallVersion));
    }));
    app.on_settings_install_cancel(on_window!(|state| {
        state.handle(Msg::Settings(SettingsMsg::InstallCancel));
    }));
    app.on_model_activated(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ModelActivated(id.to_string())));
    }));
    app.on_model_download(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ModelDownload(id.to_string())));
    }));
    app.on_model_cancel(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ModelCancel(id.to_string())));
    }));
    app.on_model_remove(on_window!(|state, id: SharedString| {
        state.handle(Msg::Settings(SettingsMsg::ModelRemove(id.to_string())));
    }));

    // ── the tray's sound and word tools ──
    // The tray's panel buttons are toggles: a press opens the panel, or
    // puts it away when it is up.
    editor.on_captions(on_window!(|state| {
        let msg = if state.captions.open {
            CaptionsMsg::Close
        } else {
            CaptionsMsg::Open
        };
        state.handle(Msg::Captions(msg));
    }));
    editor.on_record_voiceover(on_window!(|state| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Toggle));
    }));
    app.on_voiceover_closed(on_window!(|state| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Close));
    }));
    app.on_voiceover_action(on_window!(|state, verb: SharedString| {
        let msg = match verb.as_str() {
            "record" => VoiceoverMsg::Record,
            "pause" => VoiceoverMsg::Pause,
            "resume" => VoiceoverMsg::Resume,
            "stop" => VoiceoverMsg::Stop,
            "retake" => VoiceoverMsg::Retake,
            "retry" => VoiceoverMsg::Retry,
            other => {
                log::warn!("voiceover: no such action {other:?}");
                return;
            }
        };
        state.handle(Msg::Voiceover(msg));
    }));
    app.on_voiceover_device_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Device(index)));
    }));
    app.on_voiceover_countdown_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Countdown(on)));
    }));
    app.on_voiceover_mute_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Mute(on)));
    }));
    app.on_voiceover_gain_changed(on_window!(|state, db: f32| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Gain(db)));
    }));
    app.on_voiceover_prompt_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Voiceover(VoiceoverMsg::Prompt(text.to_string())));
    }));
    app.on_voiceover_prompt_speed_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Voiceover(VoiceoverMsg::PromptSpeed(index)));
    }));
    editor.on_speak(on_window!(|state| {
        let msg = if state.speech.open {
            SpeechMsg::Close
        } else {
            SpeechMsg::Open
        };
        state.handle(Msg::Speech(msg));
    }));

    app.on_captions_closed(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::Close));
    }));
    app.on_captions_voice_toggled(on_window!(|state, on: bool| {
        state.handle(Msg::Captions(CaptionsMsg::VoiceToggled(on)));
    }));
    app.on_captions_source_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Captions(CaptionsMsg::SourceChanged(index)));
    }));
    app.on_captions_text_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Captions(CaptionsMsg::TextEdited(text.to_string())));
    }));
    app.on_captions_model_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Captions(CaptionsMsg::ModelChanged(index)));
    }));
    app.on_captions_gpu_render_toggled(on_window!(|state, on: bool| {
        state.handle(Msg::Captions(CaptionsMsg::GpuRenderToggled(on)));
    }));
    app.on_captions_gpu_toggled(on_window!(|state, on: bool| {
        state.handle(Msg::Captions(CaptionsMsg::GpuToggled(on)));
    }));
    app.on_captions_preset_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Captions(CaptionsMsg::PresetChanged(index)));
    }));
    app.on_captions_import_gpu_json(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::ImportGpuJson));
    }));
    app.on_captions_karaoke_toggled(on_window!(|state, on: bool| {
        state.handle(Msg::Captions(CaptionsMsg::KaraokeToggled(on)));
    }));
    app.on_captions_placement_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Captions(CaptionsMsg::PlacementChanged(index)));
    }));
    app.on_captions_size_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Captions(CaptionsMsg::SizeChanged(index)));
    }));
    app.on_captions_begin(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::Begin));
    }));
    app.on_captions_cancel(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::Cancel));
    }));
    app.on_captions_import_srt(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::ImportSrt));
    }));
    app.on_captions_export_srt(on_window!(|state| {
        state.handle(Msg::Captions(CaptionsMsg::ExportSrt));
    }));
    app.on_speech_closed(on_window!(|state| {
        state.handle(Msg::Speech(SpeechMsg::Close));
    }));
    app.on_speech_text_edited(on_window!(|state, text: SharedString| {
        state.handle(Msg::Speech(SpeechMsg::TextEdited(text.to_string())));
    }));
    app.on_speech_voice_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Speech(SpeechMsg::VoiceChanged(index)));
    }));
    app.on_speech_model_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Speech(SpeechMsg::ModelChanged(index)));
    }));
    app.on_speech_speed_changed(on_window!(|state, value: f32| {
        state.handle(Msg::Speech(SpeechMsg::SpeedChanged(value)));
    }));
    app.on_speech_quality_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Speech(SpeechMsg::QualityChanged(index)));
    }));
    app.on_speech_pauses_changed(on_window!(|state, value: f32| {
        state.handle(Msg::Speech(SpeechMsg::PausesChanged(value)));
    }));
    app.on_speech_reference_start_changed(on_window!(|state, seconds: f32| {
        state.handle(Msg::Speech(SpeechMsg::ReferenceStartChanged(seconds)));
    }));
    app.on_speech_use_sample_changed(on_window!(|state, on: bool| {
        state.handle(Msg::Speech(SpeechMsg::UseSampleChanged(on)));
    }));
    app.on_speech_sample_changed(on_window!(|state, index: i32| {
        state.handle(Msg::Speech(SpeechMsg::SampleChanged(index)));
    }));
    // A string cut, and nothing of the window's: the sheet keeps the text,
    // Rust only knows where a character starts.
    app.on_speech_insert_tag(|text: SharedString, tag: SharedString, at: i32| {
        panes::speech::insert_tag(text.as_str(), tag.as_str(), at.max(0) as usize).into()
    });
    app.on_speech_begin(on_window!(|state| {
        state.handle(Msg::Speech(SpeechMsg::Begin));
    }));
    app.on_speech_cancel(on_window!(|state| {
        state.handle(Msg::Speech(SpeechMsg::Cancel));
    }));

    // ── the title-bar menus ──
    app.on_menu_opened(on_window!(|state, index: i32| {
        state.open_menu = index;
        state.menu_bar_token += 1;
    }));
    app.on_app_menu_selected({
        move |action| {
            log::info!("menu: {action}");
            Shell::with(|shell, app| {
                {
                    let mut state = shell.studio.borrow_mut();
                    state.open_menu = -1;
                    match action.as_str() {
                        "add-selected" => state.handle(Msg::Media(MediaMsg::AddSelected { at_playhead: false })),
                        // The launch screen's own verb, from the menu bar:
                        // the picker, the check and the notice are all one
                        // implementation in panes/start.rs, so the two
                        // cannot drift into disagreeing about what a project
                        // folder is.
                        "open" => state.handle(Msg::Start(StartMsg::Open)),
                        // Asked with the studio unborrowed - it is
                        // borrowed for this whole match - and on a turn of
                        // its own; see `host::after_dialog`.
                        "import" => host::pick_files_then(
                            i18n::t("lib.importMedia"),
                            None,
                            |studio, _, _, paths| {
                                studio.handle(Msg::Media(MediaMsg::Import(paths)))
                            },
                        ),
                        "export" => state.handle(Msg::Export(ExportMsg::Open)),
                        #[cfg(windows)]
                        "send-gpu" => {
                            if cloud_gpu::can_submit(&app) {
                                if let Some(path) = state.save_for_gpu() {
                                    cloud_gpu::submit(&app, path);
                                    state.notify("Project queued for GPU upload", false);
                                }
                            } else {
                                state.notify("GPU Queue is unavailable", true);
                            }
                        }
                        #[cfg(windows)]
                        "prepare-zip-only" => {
                            if cloud_gpu::offline_zip_busy(&app) {
                                app.global::<CloudGpu>().set_zip_open(true);
                            } else if let Some(project) = state.save_for_gpu() {
                                // Reuse precisely the Send to GPU saved-project snapshot.
                                // The picker runs later with Studio unborrowed.
                                let label = concat_project::naming::basename(&state.project_name);
                                host::after_dialog(
                                    move || rfd::FileDialog::new()
                                        .set_title("Prepare ZIP Only - Offline")
                                        .add_filter("GPU-ready ZIP", &["zip"])
                                        .set_file_name(format!("{label}.zip"))
                                        .save_file(),
                                    move |studio, app, _, selected| {
                                        let destination = if selected.extension()
                                            .is_some_and(|extension| extension.eq_ignore_ascii_case("zip"))
                                        {
                                            selected
                                        } else {
                                            selected.with_extension("zip")
                                        };
                                        // Never overwrite an existing archive.
                                        if destination.exists() {
                                            studio.notify("ZIP already exists. Choose a new name.", true);
                                            return;
                                        }
                                        cloud_gpu::prepare_zip_only(app, project, destination);
                                        studio.notify("Preparing local GPU-ready ZIP (no upload)", false);
                                    },
                                );
                            }
                        }
                        #[cfg(windows)]
                        "gpu-config" => {
                            state.handle(Msg::Settings(SettingsMsg::Open));
                            state.handle(Msg::Settings(SettingsMsg::PageChanged(cloud_gpu::SETTINGS_PAGE)));
                        }
                        "template" => state.save_template(),
                        "speech" => state.handle(Msg::Speech(SpeechMsg::Open)),
                        "clear-cache" => state.clear_project_cache(),
                        "settings" => state.handle(Msg::Settings(SettingsMsg::Open)),
                        "close-project" => {
                            // Back to the launch screen, not out of the
                            // app: a failed save keeps the project open and
                            // the notice below lands it on screen.
                            let _ = state.close_project();
                        }
                        "undo" => state.undo(),
                        "redo" => state.redo(),
                        "snap" => state.handle(Msg::Timeline(TimelineMsg::SnapToggled)),
                        "pan" => {
                            let on = !state.lanes.pan_mode;
                            state.handle(Msg::Timeline(TimelineMsg::PanChanged(on)))
                        }
                        "preview-axis" => {
                            let on = !state.prefs.preview_axis;
                            state.handle(Msg::Timeline(TimelineMsg::PreviewAxisChanged(on)))
                        }
                        "magnetic" => state.handle(Msg::Timeline(TimelineMsg::MagneticToggled)),
                        "sort-added" => state.handle(Msg::Media(MediaMsg::SortChanged(0))),
                        "sort-name" => state.handle(Msg::Media(MediaMsg::SortChanged(1))),
                        "sort-kind" => state.handle(Msg::Media(MediaMsg::SortChanged(2))),
                        "zoom-in" => state.handle(Msg::Timeline(TimelineMsg::ZoomIn)),
                        "zoom-out" => state.handle(Msg::Timeline(TimelineMsg::ZoomOut)),
                        "start" => {
                            state.pause();
                            state.seek(0.0);
                        }
                        "end" => {
                            state.pause();
                            let end = state.duration();
                            state.seek(end);
                        }
                        "delete" => state.delete_selected(),
                        // The selected clips, as the tray's button splits, or
                        // everything under the playhead when none is.
                        "split" => {
                            let at = state.playhead;
                            state.split_at(at, true);
                        }
                        "save" => state.save(true),
                        _ => {}
                    }
                }
                if action == "close-window" {
                    log::info!("close: File > Close Window");
                    let outcome = close_window(
                        || shell.studio.borrow_mut().close_project(),
                        || {
                            app.window().hide().ok();
                            slint::quit_event_loop().ok();
                        },
                    );
                    if let Err(error) = outcome {
                        // Unsaved work is worth more than the window: the
                        // project stays open and the reason is shown.
                        log::warn!("close: not closing, project could not be saved: {error}");
                        shell.studio.borrow_mut().offer_quit_without_saving(error);
                        shell.studio.borrow_mut().refresh_art();
                        shell.studio.borrow().publish(&app, &shell.models);
                        return;
                    }
                    log::info!("close: quit_event_loop called (menu)");
                    return;
                }
                shell.studio.borrow_mut().refresh_art();
                shell.studio.borrow().publish(&app, &shell.models);
            });
        }
    });

    // --- the pieces Slint cannot express ---------------------------------
    app.global::<Curves>().on_ease(format::bezier_y_at_x);
    app.global::<Curves>()
        .on_parse(|text, fallback| format::parse_bezier(text.as_str(), fallback));
    app.global::<Fmt>()
        .on_parse_timecode(|text| format::parse_timecode(text.as_str()));
    app.global::<Fmt>().on_tick_interval(format::tick_interval);
    app.global::<Fmt>().on_hex_of(|value, with_alpha| {
        if with_alpha {
            format::hex_rgba(value).into()
        } else {
            format::hex_of(value).into()
        }
    });
    app.global::<Fmt>()
        .on_hex_digits(|value| format::hex_of(value)[1..].into());
    app.global::<Fmt>()
        .on_color_of(|text, fallback| format::parse_colour(text.as_str()).unwrap_or(fallback));
    app.global::<Fmt>()
        .on_parse_frames(|text, rate| format::parse_frames(text.as_str(), rate));

    // The drag payloads: plain text, because a drag that says "media:12" is
    // one that can be read in a log.
    app.global::<Payload>().on_of(DataTransfer::from);
    app.global::<Payload>()
        .on_text(|payload| payload.plain_text().unwrap_or_default());
    app.global::<Payload>().on_pane_seat(|text| {
        text.strip_prefix("pane:")
            .and_then(|rest| rest.split(':').next())
            .and_then(|seat| seat.parse().ok())
            .unwrap_or(-1)
    });
    app.global::<Payload>().on_tab_index(|text| {
        text.strip_prefix("tab:")
            .and_then(|rest| rest.split(':').next())
            .and_then(|index| index.parse().ok())
            .unwrap_or(-1)
    });

    // The picture the cursor carries, resolved through the same `incoming`
    // the drop uses, memoised by theme and payload.
    app.global::<Payload>().on_preview({
        let chips: RefCell<HashMap<String, slint::Image>> = RefCell::new(HashMap::new());
        move |payload| {
            let mut result = slint::Image::default();
            Shell::with(|shell, app| {
                let theme = app.global::<Theme>();
                let key = format!("{}{payload}", if theme.get_dark() { 'd' } else { 'l' });
                if let Some(chip) = chips.borrow().get(&key) {
                    result = chip.clone();
                    return;
                }
                if let Some(rest) = payload.strip_prefix("pane:") {
                    let mut fields = rest.splitn(3, ':').skip(1);
                    let label = fields.next().unwrap_or_default();
                    let slug = fields.next().unwrap_or_default();
                    let chip = slint::Image::load_from_svg_data(
                        chips::drag_chip_svg(
                            chips::pane_glyph(slug),
                            label,
                            "",
                            theme.get_accent(),
                            theme.get_field(),
                            theme.get_raised(),
                            theme.get_fg(),
                        )
                        .as_bytes(),
                    )
                    .unwrap_or_default();
                    chips.borrow_mut().insert(key, chip.clone());
                    result = chip;
                    return;
                }
                // A timeline tab in flight wears the timeline pane's mark.
                if let Some(rest) = payload.strip_prefix("tab:") {
                    let name = rest.split_once(':').map_or("", |(_, name)| name);
                    let chip = slint::Image::load_from_svg_data(
                        chips::drag_chip_svg(
                            chips::pane_glyph("timeline"),
                            name,
                            "",
                            theme.get_accent(),
                            theme.get_field(),
                            theme.get_raised(),
                            theme.get_fg(),
                        )
                        .as_bytes(),
                    )
                    .unwrap_or_default();
                    chips.borrow_mut().insert(key, chip.clone());
                    result = chip;
                    return;
                }
                let studio = shell.studio.borrow();
                let Some(plan) = studio.incoming(payload.as_str()) else {
                    return;
                };
                let (mark, well) = match plan.kind {
                    ClipKind::Video => (theme.get_kind_video(), theme.get_kind_video_well()),
                    ClipKind::Audio => (theme.get_kind_audio(), theme.get_kind_audio_well()),
                    ClipKind::Image => (theme.get_kind_image(), theme.get_kind_image_well()),
                    ClipKind::Text => (theme.get_kind_text(), theme.get_kind_text_well()),
                    ClipKind::Filter => (theme.get_kind_filter(), theme.get_kind_filter_well()),
                    ClipKind::Shape => (theme.get_kind_shape(), theme.get_kind_shape_well()),
                };
                let wave = studio
                    .peaks
                    .get(&plan.media)
                    .filter(|_| plan.kind == ClipKind::Audio)
                    .map(|peaks| format::wave_path(peaks, 0.0, plan.duration, 32, 0.75))
                    .unwrap_or_default();
                let document = chips::drag_chip_svg(
                    chips::chip_glyph(plan.kind),
                    &plan.label,
                    &wave,
                    mark,
                    well,
                    theme.get_raised(),
                    theme.get_fg(),
                );
                let chip =
                    slint::Image::load_from_svg_data(document.as_bytes()).unwrap_or_default();
                chips.borrow_mut().insert(key, chip.clone());
                result = chip;
            });
            result
        }
    });

    // The programme level meter has no feed yet: playback's mix does not
    // report levels. It stays parked at silence.
    app.on_meter_watched_changed({
        let weak = app.as_weak();
        move |_watched| {
            if let Some(app) = weak.upgrade() {
                app.global::<Editor>().set_level(0.0);
                app.global::<Editor>().set_peak(-1.0);
            }
        }
    });

    if let Some(path) = project {
        // Use the same loader as Open project, after all callbacks and models
        // are installed. Make the path absolute so recents never depend on the
        // launcher's working directory on a later run. Unlike canonicalize,
        // this does not introduce Windows verbatim paths into media tooling.
        let opened = std::path::absolute(&path)
            .map_err(|error| format!("{}: {error}", path.display()))
            .and_then(|path| {
                let path = path
                    .to_str()
                    .ok_or_else(|| "project directory is not valid Unicode".to_string())?;
                concat_host::projects::open(path)
            })
            .and_then(|info| shell.studio.borrow_mut().open_project(info));
        if let Err(error) = opened {
            log::error!("could not open launch project: {error}");
            shell.studio.borrow_mut().notify(&error, true);
        }
    }

    {
        shell.studio.borrow_mut().refresh_art();
        shell.studio.borrow().publish(&app, &shell.models);
    }

    // As far as a smoke run goes: everything the first frame needs exists
    // and has been handed to the window. The exit is the one the event
    // loop's end takes below, for the same reason - the engine's threads
    // are not waited on.
    if smoke {
        // whisper's CPU kernels, which the x86_64 Windows and Linux bundles
        // carry as modules beside the program, one per CPU generation, for
        // ggml to choose from (#388). A bundle without them starts and
        // fails at its first transcription, so the smoke run loads them.
        // Not elsewhere: there they are linked in, and asking ggml for its
        // devices would only bring Metal up on a Mac runner for nothing.
        #[cfg(all(
            target_arch = "x86_64",
            any(target_os = "windows", target_os = "linux")
        ))]
        match concat_speech::transcribe::cpu_backend() {
            Ok(cpu) => log::info!("smoke: whisper runs on {cpu}"),
            Err(error) => {
                log::error!("smoke: {error}");
                leave(1);
            }
        }
        log::info!("smoke: ok");
        leave(0);
    }

    // Decoders nobody has asked a frame of for ten seconds are closed,
    // looked at every five: each holds buffers of its own, and the playhead
    // leaves files behind. One reopens on its next frame. Held here, for
    // the life of the loop.
    let reader_sweep = slint::Timer::default();
    reader_sweep.start(
        slint::TimerMode::Repeated,
        std::time::Duration::from_secs(5),
        || {
            let closed = concat_host::scheduler()
                .pool()
                .close_idle(std::time::Duration::from_secs(10));
            if closed > 0 {
                log::debug!("memory: closed {closed} idle decoder(s)");
            }
        },
    );

    // A launch that has run this long was not killed by its start; the
    // mark goes, so the next one is not started safe for nothing.
    let started = startup_mark.as_ref().map(|mark| mark.path().to_owned());
    slint::Timer::single_shot(SAFE_AFTER, move || {
        if let Some(path) = started {
            let _ = std::fs::remove_file(path);
        }
    });

    // The chosen interface scale, as soon as the window is up.
    slint::Timer::single_shot(std::time::Duration::ZERO, || {
        Shell::with(|_, app| platform::apply_ui_scale(app.window()));
    });

    let result = app.run();
    drop(reader_sweep);
    log::info!("close: event loop exited (ok={})", result.is_ok());
    // The loop failing is the window never having run at all - on a phone
    // whose Vulkan driver wgpu will not take, it is the whole launch, and
    // the person sees the app vanish. Its reason goes in the log before
    // anything else can end the process, and the exit says it failed.
    if let Err(error) = &result {
        log::error!("close: the window could not run: {error}");
        // The mark stays: the next launch starts safe.
        std::mem::forget(startup_mark);
        leave(1);
    }
    drop(startup_mark);
    leave(0);
}

/// How long a launch runs before it counts as having started.
const SAFE_AFTER: std::time::Duration = std::time::Duration::from_secs(15);

/// Safe start: whether this launch draws on the software rasteriser
/// because the last one died while starting, and the mark that says this
/// one is starting.
///
/// Every recent release met a Windows driver that closed the app in its
/// first seconds - a new one each time (#339, #378, #389, #242) - and a
/// launch check on a CI machine meets none of them. A launch that dies
/// before [`SAFE_AFTER`] leaves its mark; the next one finds it, draws on
/// WARP or lavapipe instead of the GPU, and keeps doing so - a driver that
/// killed one launch kills the next, and starting on the GPU every other
/// time would crash every other time. Slower, and it opens. Settings >
/// General's GPU switch goes back. Only on Windows and Linux, which have a
/// software rasteriser to draw on; a Mac has none, and a phone's backend
/// picks its own way to draw.
fn safe_start() -> (Option<concat_host::jobs::Tripwire>, bool) {
    if !cfg!(any(windows, target_os = "linux")) {
        return (None, false);
    }
    let Ok(dirs) = concat_host::AppDirs::locate() else {
        return (None, false);
    };
    let mark = dirs.data.join("startup");
    let mut preferences = prefs::Preferences::load(&dirs);
    let safe = concat_host::jobs::Tripwire::tripped(&mark) && !preferences.software_gpu;
    if safe {
        log::warn!("start: the last launch closed while starting; drawing in software from now on");
        preferences.software_gpu = true;
        preferences.save(&dirs);
    }
    if preferences.software_gpu && std::env::var_os("CONCAT_GPU").is_none() {
        // SAFETY: the process is single-threaded here, as for the scale
        // above: nothing reads the environment concurrently.
        unsafe { std::env::set_var("CONCAT_GPU", "software") };
    }
    (Some(concat_host::jobs::Tripwire::arm(&mark)), safe)
}

/// Ends the process with `code`, the log flushed first, and the threads
/// still running - the GPU's, the engine's, the audio output's - not
/// waited on.
///
/// On Linux it is `_exit`, not `exit`: `exit` runs the C libraries' exit
/// handlers under those threads, and they are not written for it - most
/// likely Mesa's llvmpipe, tearing its LLVM down while its rasteriser
/// threads are still drawing. On an arm64 machine with no GPU,
/// v0.2.7-rc.2's `concat --smoke` logged "smoke: ok" and then died of a
/// segmentation fault on its way out; a person closing the window there
/// would have had the same crash. Nothing those handlers do is anything
/// a closing app needs: the log is flushed here, and the files a project
/// writes are written before the window closes.
fn leave(code: i32) -> ! {
    log::logger().flush();
    #[cfg(target_os = "linux")]
    {
        use std::io::Write;
        let _ = std::io::stdout().flush();
        let _ = std::io::stderr().flush();
        // SAFETY: `_exit` ends the process there and then; nothing of
        // this one's is touched after it.
        unsafe { libc::_exit(code) }
    }
    #[cfg(not(target_os = "linux"))]
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::close_window;
    use std::cell::{Cell, RefCell};
    use std::path::Path;

    use concat_host::{Session, projects};

    /// A scratch folder that removes itself when the test ends, pass, fail or
    /// panic. Its name carries the process id and the thread's so two tests
    /// running at once never share one, and it sits under `temp_dir`, which
    /// the isolated run points into the approved directory.
    struct Scratch(std::path::PathBuf);

    impl Scratch {
        fn new(tag: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "concat-close-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).expect("a scratch folder");
            Self(path)
        }

        fn location(&self) -> String {
            self.0.to_string_lossy().into_owned()
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            // Best effort: a leftover scratch must not fail a run that its
            // test already decided.
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// Saves the session the slot holds and, only when the write lands, lets
    /// it go - the contract `Studio::close_project` keeps for every way out.
    /// The write is `concat_host`'s own, so nothing here fabricates a
    /// failure or a success.
    fn save_then_release(slot: &RefCell<Option<Session>>) -> Result<(), String> {
        let mut slot = slot.borrow_mut();
        let result = match slot.as_mut() {
            Some(session) => session.save(None),
            None => Ok(()),
        };
        if result.is_ok() {
            *slot = None;
        }
        result
    }

    fn tracks(session: &Session) -> usize {
        session.project().active().tracks.len()
    }

    /// The regression this gate exists for, over a real project on disk: a
    /// close used to hide the window and quit whatever the save did. The
    /// write here is `concat_host`'s own - a real [`Session`] through
    /// `projects::save` - and it is made to fail by a directory standing
    /// where the manifest must go, a block at the destination the save
    /// renames onto, so the failure holds whatever the save calls its
    /// temporary file (the old `.saving` or the current per-call name). A
    /// failed write must leave the session open, keep the edit it holds and
    /// never leave; the retry, unblocked, must land the edit and then leave.
    ///
    /// The gate is what is under test, not a Slint handler: there is no
    /// window, no event loop and no `Studio` here, only the real session and
    /// the real filesystem.
    #[test]
    fn a_failed_save_keeps_the_real_session_and_its_edit() {
        let scratch = Scratch::new("session");
        let info = projects::create(&scratch.location(), "Closing", 1920, 1080, 30, 1)
            .expect("creates the project");
        let manifest = projects::manifest_path(Path::new(&info.path));

        // A real session, saved once; then an edit that lives in memory
        // only, so the disk holds the project without it.
        let mut session = Session::open_info(&info).expect("opens the session");
        let unedited = tracks(&session);
        session.save(None).expect("the baseline write lands");
        let baseline = std::fs::read(&manifest).expect("reads the baseline manifest");
        session
            .apply(concat_project::Command::AddTrack { bottom: false })
            .expect("adds a track");
        let edited = tracks(&session);
        assert_eq!(edited, unedited + 1, "the edit really changed the project");

        // Block the destination: a directory where the manifest must go. The
        // save writes its temporary, flushes it, and cannot rename it over a
        // directory, so it fails for real and leaves no partial manifest.
        std::fs::remove_file(&manifest).expect("removes the manifest");
        std::fs::create_dir(&manifest).expect("blocks the manifest path");

        let slot = RefCell::new(Some(session));
        let left = Cell::new(false);
        let outcome = close_window(|| save_then_release(&slot), || left.set(true));

        let failed_at = manifest.display().to_string();
        assert!(outcome.is_err(), "a blocked write is a failure");
        assert!(
            outcome.as_ref().unwrap_err().contains(failed_at.as_str()),
            "the failure is the real one from the blocked destination: {outcome:?}"
        );
        assert!(manifest.is_dir(), "the block is untouched, nothing landed");
        {
            let held = slot.borrow();
            let session = held.as_ref().expect("the session is kept");
            assert_eq!(
                tracks(session),
                edited,
                "the kept session still holds the edit"
            );
        }
        assert!(!left.get(), "nothing leaves on a failed save");

        // Restore the baseline that the test removed to install its block.
        // This checks recovery; it does not claim that a manifest deleted
        // by the test itself was preserved by the failed write.
        std::fs::remove_dir(&manifest).expect("clears the block");
        std::fs::write(&manifest, &baseline).expect("restores the manifest");
        let reopened = Session::open_info(&info).expect("reopens the old document");
        assert_eq!(
            tracks(&reopened),
            unedited,
            "the failed write saved nothing over the old one"
        );
        drop(reopened);

        // The retry, unblocked: the same gate, now landing. Only a landed
        // save lets the session go and leaves, and the edit is on disk for
        // the next open.
        let outcome = close_window(|| save_then_release(&slot), || left.set(true));
        assert!(outcome.is_ok(), "the retry lands: {outcome:?}");
        assert!(slot.borrow().is_none(), "a landed save lets the session go");
        assert!(left.get(), "and only then leaves");
        let reopened = Session::open_info(&info).expect("reopens the saved project");
        assert_eq!(
            tracks(&reopened),
            edited,
            "the edit is on disk after the retry"
        );
    }

    /// Exercise the capture-before-dispatch contract with real session
    /// snapshots: a queued autosave must not replace the newer close save,
    /// even if its worker only starts writing after close has succeeded.
    #[test]
    fn a_delayed_autosave_cannot_replace_the_close_snapshot() {
        let scratch = Scratch::new("ordered-close");
        let info = projects::create(&scratch.location(), "Ordered", 1920, 1080, 30, 1)
            .expect("creates the project");
        let mut session = Session::open_info(&info).expect("opens the session");
        let (old_path, old_document) = session.prepare_save(None);
        let old_ticket = projects::ticket();
        session
            .apply(concat_project::Command::AddTrack { bottom: false })
            .expect("adds the unsaved track");
        let edited = tracks(&session);
        let (new_path, new_document) = session.prepare_save(None);
        let new_ticket = projects::ticket();

        let (release, delayed) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            delayed.recv().expect("releases the delayed autosave");
            projects::save_ordered(&old_path, &old_document, old_ticket)
        });
        let left = Cell::new(false);
        let outcome = close_window(
            || projects::save_ordered(&new_path, &new_document, new_ticket).map(|_| ()),
            || left.set(true),
        );
        // Always release and join the worker before asserting, including
        // when the close write unexpectedly fails.
        release.send(()).expect("releases the worker");
        let late = worker.join().expect("the worker completes");
        assert!(outcome.is_ok(), "the close save succeeds: {outcome:?}");
        assert!(left.get());
        assert_eq!(late, Ok(projects::Saved::Superseded));
        let reopened = Session::open_info(&info).expect("reopens the latest snapshot");
        assert_eq!(tracks(&reopened), edited, "the last edit survives close");
    }

    /// The version switch schedules its relaunch inside the leave, so a
    /// failed save starts no replacement build over the running one.
    #[test]
    fn a_failed_save_does_not_schedule_the_relaunch() {
        let relaunched = Cell::new(false);
        let outcome = close_window(
            || Err("could not write the project".to_owned()),
            || relaunched.set(true),
        );
        assert!(outcome.is_err());
        assert!(!relaunched.get(), "nothing is relaunched on a failed save");
    }
}


/// Import or refresh a folder using Concat's own probe. Work stays off the UI thread;
/// saved metadata is reused for every unchanged file, and completed scans are durable.
fn c_builder_scan(studio: &mut Studio, folder: std::path::PathBuf) {
    if studio.c_builder.scanning { return; }
    let folder_label = folder.to_string_lossy().into_owned();
    studio.c_builder.folder = folder_label.clone();
    studio.c_builder.scanning = true;
    studio.c_builder.scan_done = 0;
    studio.c_builder.scan_total = 0;
    studio.c_builder.status = "Scanning media...".into();
    let cache_path = studio.host.dirs.data.join("c-builder-media-cache.json");
    host::spawn(
        move || {
            c_builder::scan(&folder, &cache_path, |done, total| {
                if done == 0 || done == total || done % 8 == 0 {
                    let expected = folder_label.clone();
                    host::on_ui(move |studio, _, _| {
                        if studio.c_builder.scanning && studio.c_builder.folder == expected {
                            studio.c_builder.scan_done = done;
                            studio.c_builder.scan_total = total;
                        }
                    });
                }
            })
        },
        |studio, _, _, result| {
            studio.c_builder.scanning = false;
            match result {
                Ok(report) => {
                    studio.c_builder.folder = report.root.to_string_lossy().into_owned();
                    studio.c_builder.keyword_summary = report.keywords();
                    studio.c_builder.assets = report.assets;
                    studio.c_builder.probed = report.probed;
                    studio.c_builder.reused = report.reused;
                    studio.c_builder.removed = report.removed;
                    studio.c_builder.scan_done = studio.c_builder.assets.len() + report.failures.len();
                    studio.c_builder.scan_total = studio.c_builder.scan_done;
                    studio.c_builder.status = format!(
                        "Library ready: {} images, {} videos; {} probed, {} cached, {} removed, {} errors",
                        studio.c_builder.images(), studio.c_builder.videos(), report.probed,
                        report.reused, report.removed, report.failures.len(),
                    );
                    if let Some(error) = report.failures.first() {
                        studio.notify(&format!("Media import: {error}"), true);
                    }
                }
                Err(error) => {
                    studio.c_builder.status = format!("Scan failed: {error}");
                    studio.notify(&error, true);
                }
            }
        },
    );
}
