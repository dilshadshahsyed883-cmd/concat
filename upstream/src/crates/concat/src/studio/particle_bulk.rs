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
        selected_only: bool,
        images: bool,
        videos: bool,
    ) {
        let Some(id) = IDS.get(preset as usize) else {
            self.notify("Choose a valid particle preset and target type", true);
            return;
        };
        if !images && !videos {
            self.notify("Choose a valid particle preset and target type", true);
            return;
        }
        let id = format!("concat.particles-{id}");
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
            let mut effects = clip.video_effects.clone();
            // The shared upsert enforces one particle preset per clip and preserves unrelated effects.
            let mut entry = self.effect_application(&id);
            // Preserve deterministic per-clip random seeds unless this preset
            // has an explicitly edited/saved seed override.
            if !entry.params.contains_key("seed") {
                entry.params.insert("seed".into(), (draw % 65536) as f64);
            }
            model::upsert_effect(&mut effects, entry);
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
