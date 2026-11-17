// SPDX-License-Identifier: AGPL-3.0-or-later
use super::*;
const IDS: [&str; 12] = [
    "stars",
    "gold-sparkles",
    "embers",
    "fireflies",
    "bokeh",
    "dust-motes",
    "snow",
    "rain",
    "ash",
    "light-leaks",
    "shooting-stars",
    "confetti",
];
impl Studio {
    pub fn apply_particle_bulk(
        &mut self,
        preset: i32,
        mode: i32,
        selected_only: bool,
        images: bool,
        videos: bool,
    ) {
        if !matches!(mode, 0..=2) || (!images && !videos) {
            self.notify("Choose a valid particle preset and target type", true);
            return;
        }
        let options: Vec<String> = IDS
            .iter()
            .enumerate()
            .filter(|(i, _)| mode != 0 || *i == preset as usize)
            .map(|(_, id)| format!("concat.particles-{id}"))
            .filter(|id| mode != 2 || self.prefs.favourites.contains(id))
            .collect();
        if options.is_empty() {
            self.notify("Choose or favorite a particle effect first", true);
            return;
        }
        if selected_only && self.selection.is_empty() {
            self.notify("Select timeline media first", true);
            return;
        }
        let seed = crate::c_builder::random_seed();
        let mut commands = Vec::new();
        for clip in &self.timeline().clips {
            if !((images && clip.kind == model::ClipKind::Image)
                || (videos && clip.kind == model::ClipKind::Video))
                || self.locked(&clip.track_id)
                || self
                    .timeline()
                    .track(&clip.track_id)
                    .is_none_or(|t| !t.visible)
                || (selected_only && !self.selection.contains(&clip.id))
            {
                continue;
            }
            let draw = clip
                .id
                .bytes()
                .fold(seed, |s, b| s.wrapping_mul(0x100000001b3) ^ u64::from(b));
            let id = &options[draw as usize % options.len()];
            let mut effects = clip.video_effects.clone();
            // Explicit application changes only the requested preset, preserving other overlays and authored effects.
            let mut entry = AppliedFilter::new(id);
            entry.params.insert("seed".into(), (draw % 65536) as f64);
            if let Some(index) = effects.iter().position(|e| &e.id == id) {
                effects[index] = entry;
            } else {
                effects.push(entry);
            }
            commands.push(Command::UpdateClip {
                clip_id: clip.id.clone(),
                patch: ClipPatch {
                    video_effects: Some(effects),
                    ..Default::default()
                },
            });
        }
        if commands.is_empty() {
            self.notify("No unlocked matching images or videos", true);
            return;
        }
        self.apply(Command::Batch { commands });
    }
}
