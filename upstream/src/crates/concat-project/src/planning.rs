// SPDX-License-Identifier: AGPL-3.0-or-later
//! Declarative, deterministic shot planning. No probing, file writes or model inference.
#![allow(missing_docs)]
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Anchor {
    pub at: f64,
    pub video: f64,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Pin {
    pub source_id: String,
    pub start_us: u64,
    pub duration_us: u64,
    #[serde(default)]
    pub trim_us: u64,
}
#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KeywordSelection {
    pub selected: bool,
    pub weight: u32,
}
#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct MediaSelection {
    #[serde(default)]
    pub images: std::collections::BTreeMap<String, KeywordSelection>,
    #[serde(default)]
    pub videos: std::collections::BTreeMap<String, KeywordSelection>,
    #[serde(default)]
    pub videos_enabled: bool,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Profile {
    pub version: u32,
    pub name: String,
    pub curve: Vec<Anchor>,
    pub image_seconds: [f64; 2],
    pub video_seconds: [f64; 2],
    /// Zero means unlimited.
    pub max_videos: usize,
    pub max_images: usize,
    pub cooldown: usize,
    pub allow_repeats: bool,
    /// Correct probabilities for differing shot durations when targeting seconds.
    pub ratio_by_seconds: bool,
    pub seed: u64,
    #[serde(default)]
    pub pins: Vec<Pin>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub media_selection: Option<MediaSelection>,
}
impl Default for Profile {
    fn default() -> Self {
        Self {
            version: 1,
            name: "Video-Heavy Opening".into(),
            curve: vec![
                Anchor {
                    at: 0.0,
                    video: 0.65,
                },
                Anchor {
                    at: 0.25,
                    video: 0.45,
                },
                Anchor {
                    at: 0.75,
                    video: 0.20,
                },
                Anchor {
                    at: 1.0,
                    video: 0.05,
                },
            ],
            image_seconds: [3.0, 5.0],
            video_seconds: [2.5, 4.0],
            max_videos: 2,
            max_images: 0,
            cooldown: 3,
            allow_repeats: true,
            ratio_by_seconds: true,
            seed: 1,
            pins: Vec::new(),
            media_selection: None,
        }
    }
}
impl Profile {
    pub fn builtin(index: usize) -> Option<Self> {
        let mut profile = Self::default();
        match index {
            0 => {
                profile.name = "Balanced".into();
                profile.curve = vec![
                    Anchor {
                        at: 0.0,
                        video: 0.2,
                    },
                    Anchor {
                        at: 1.0,
                        video: 0.2,
                    },
                ];
            }
            1 => {}
            2 => {
                profile.name = "Image-Heavy Outro".into();
                profile.curve = vec![
                    Anchor {
                        at: 0.0,
                        video: 0.4,
                    },
                    Anchor {
                        at: 0.7,
                        video: 0.2,
                    },
                    Anchor {
                        at: 1.0,
                        video: 0.0,
                    },
                ];
            }
            3 => {
                profile.name = "Fully Custom".into();
            }
            _ => return None,
        }
        Some(profile)
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 {
            return Err("Unsupported planning profile version".into());
        }
        if self.name.trim().is_empty() || self.name.len() > 128 {
            return Err("Invalid profile name".into());
        }
        if !(2..=128).contains(&self.curve.len())
            || self.curve[0].at != 0.0
            || self.curve.last().map(|a| a.at) != Some(1.0)
        {
            return Err("Curve needs 2–128 anchors covering 0 through 1".into());
        }
        let mut previous = -1.0;
        for anchor in &self.curve {
            if !anchor.at.is_finite()
                || !(0.0..=1.0).contains(&anchor.at)
                || anchor.at <= previous
                || !anchor.video.is_finite()
                || !(0.0..=1.0).contains(&anchor.video)
            {
                return Err("Invalid curve anchor".into());
            }
            previous = anchor.at;
        }
        for range in [self.image_seconds, self.video_seconds] {
            if range
                .iter()
                .any(|v| !v.is_finite() || !(0.1..=3600.0).contains(v))
                || range[0] > range[1]
            {
                return Err("Invalid shot duration range (0.1–3600s)".into());
            }
        }
        if self.cooldown > 10000 || self.max_videos > 10000 || self.max_images > 10000 {
            return Err("Sequence controls exceed safety limits".into());
        }
        if let Some(selection) = &self.media_selection {
            if selection.images.len() + selection.videos.len() > 20000
                || selection
                    .images
                    .iter()
                    .chain(selection.videos.iter())
                    .any(|(key, value)| {
                        key.trim().is_empty() || key.len() > 256 || value.weight > 100
                    })
            {
                return Err("Keyword selections require names and weights from 0 to 100".into());
            }
        }
        if self.pins.len() > 10000 {
            return Err("Too many pinned shots".into());
        }
        let mut end = 0;
        for pin in &self.pins {
            if pin.source_id.is_empty() || pin.duration_us == 0 || pin.start_us < end {
                return Err(
                    "Pinned shots must be ordered, non-overlapping and have positive durations"
                        .into(),
                );
            }
            end = pin
                .start_us
                .checked_add(pin.duration_us)
                .ok_or("Pinned interval overflow")?;
        }
        Ok(())
    }
    pub fn from_json(source: &str) -> Result<Self, String> {
        if source.len() > 65536 {
            return Err("Profile exceeds 64 KiB".into());
        }
        let profile: Self = serde_json::from_str(source).map_err(|e| e.to_string())?;
        profile.validate()?;
        Ok(profile)
    }
    pub fn video_weight(&self, at: f64) -> f64 {
        let pair = self
            .curve
            .windows(2)
            .find(|p| at <= p[1].at)
            .unwrap_or(&self.curve[self.curve.len() - 2..]);
        let u = ((at - pair[0].at) / (pair[1].at - pair[0].at)).clamp(0.0, 1.0);
        pair[0].video + (pair[1].video - pair[0].video) * u
    }
}
#[derive(Clone, Debug)]
pub struct Source {
    pub id: String,
    pub group: String,
    pub video: bool,
    pub duration_us: u64,
    pub weight: f64,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Placement {
    pub source: usize,
    pub start_us: u64,
    pub duration_us: u64,
    pub trim_us: u64,
}
#[derive(Clone, Debug)]
pub struct Plan {
    pub shots: Vec<Placement>,
    pub warnings: Vec<String>,
}
#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub images: usize,
    pub videos: usize,
    pub image_us: u64,
    pub video_us: u64,
}
impl Plan {
    pub fn stats(&self, sources: &[Source]) -> Stats {
        let mut stats = Stats::default();
        for shot in &self.shots {
            if sources[shot.source].video {
                stats.videos += 1;
                stats.video_us += shot.duration_us;
            } else {
                stats.images += 1;
                stats.image_us += shot.duration_us;
            }
        }
        stats
    }
}
struct Random(u64);
impl Random {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / ((1_u64 << 53) as f64)
    }
}
/// Exhausted hard constraints return an error; availability/cooldown relaxations are reported.
pub fn generate(sources: &[Source], total_us: u64, profile: &Profile) -> Result<Plan, String> {
    profile.validate()?;
    if total_us == 0 || total_us > 86_400_000_000 {
        return Err("Duration must be 0–24 hours".into());
    }
    if sources.is_empty()
        || sources
            .iter()
            .any(|s| !s.weight.is_finite() || s.weight < 0.0)
    {
        return Err("Missing sources or invalid relevance weight".into());
    }
    let ids: std::collections::HashSet<_> = sources.iter().map(|s| &s.id).collect();
    if ids.len() != sources.len() {
        return Err("Duplicate source identities".into());
    }
    for pin in &profile.pins {
        let source = sources
            .iter()
            .find(|s| s.id == pin.source_id)
            .ok_or("Pinned source is unavailable")?;
        if pin.start_us + pin.duration_us > total_us
            || (source.video && pin.trim_us.saturating_add(pin.duration_us) > source.duration_us)
            || (!source.video && pin.trim_us != 0)
        {
            return Err("Pinned shot exceeds timeline or source bounds".into());
        }
    }
    let mut rng = Random(if profile.seed == 0 {
        0x9e3779b97f4a7c15
    } else {
        profile.seed
    });
    let mut plan = Plan {
        shots: Vec::new(),
        warnings: Vec::new(),
    };
    let mut uses = vec![0_usize; sources.len()];
    let mut at = 0;
    let mut video_run = 0;
    let mut image_run = 0;
    while at < total_us {
        if plan.shots.len() >= 1_000_000 {
            return Err("Plan exceeds one million shots".into());
        }
        if let Some(pin) = profile.pins.iter().find(|p| p.start_us == at) {
            let index = sources
                .iter()
                .position(|s| s.id == pin.source_id)
                .ok_or("Pinned source missing")?;
            let video = sources[index].video;
            if (!profile.allow_repeats && uses[index] > 0)
                || (video && profile.max_videos > 0 && video_run >= profile.max_videos)
                || (!video && profile.max_images > 0 && image_run >= profile.max_images)
            {
                return Err(
                    "Pinned shot violates hard repetition/run limits; adjust profile".into(),
                );
            }
            plan.shots.push(Placement {
                source: index,
                start_us: at,
                duration_us: pin.duration_us,
                trim_us: pin.trim_us,
            });
            at += pin.duration_us;
            uses[index] += 1;
            if video {
                video_run += 1;
                image_run = 0;
            } else {
                image_run += 1;
                video_run = 0;
            }
            continue;
        }
        let available = |video: bool| -> Vec<usize> {
            sources
                .iter()
                .enumerate()
                .filter(|(i, s)| {
                    s.video == video
                        && s.weight > 0.0
                        && (!video || s.duration_us >= (profile.video_seconds[0] * 1e6) as u64)
                        && (profile.allow_repeats || uses[*i] == 0)
                })
                .map(|(i, _)| i)
                .collect()
        };
        let images = available(false);
        let videos = available(true);
        let can_image =
            !images.is_empty() && (profile.max_images == 0 || image_run < profile.max_images);
        let can_video =
            !videos.is_empty() && (profile.max_videos == 0 || video_run < profile.max_videos);
        if !can_image && !can_video {
            return Err(format!(
                "Hard sequence/repeat constraints exhausted at {:.3}s; adjust profile",
                at as f64 / 1e6
            ));
        }
        let mut weight = profile.video_weight(at as f64 / total_us as f64);
        if profile.ratio_by_seconds {
            let image_mean = (profile.image_seconds[0] + profile.image_seconds[1]) / 2.0;
            let video_mean = (profile.video_seconds[0] + profile.video_seconds[1]) / 2.0;
            weight = (weight / video_mean) / (weight / video_mean + (1.0 - weight) / image_mean);
        }
        let want_video = rng.unit() < weight;
        let video = if want_video { can_video } else { !can_image };
        if video != want_video {
            let warning = "Requested distribution constrained by source availability or run limits"
                .to_owned();
            if !plan.warnings.contains(&warning) {
                plan.warnings.push(warning);
            }
        }
        let mut candidates = if video { videos } else { images };
        let recent = &plan.shots[plan.shots.len().saturating_sub(profile.cooldown)..];
        let fresh: Vec<_> = candidates
            .iter()
            .copied()
            .filter(|i| {
                !recent
                    .iter()
                    .any(|s| sources[s.source].id == sources[*i].id)
            })
            .collect();
        if !fresh.is_empty() {
            candidates = fresh;
        } else if profile.cooldown > 0 && !recent.is_empty() {
            let warning =
                "Source cooldown relaxed because no unused candidate is available".to_owned();
            if !plan.warnings.contains(&warning) {
                plan.warnings.push(warning);
            }
        }
        // Pick keyword groups before assets. Library size must not silently
        // multiply a group's selected percentage, nor should anti-repeat usage.
        let available_groups: std::collections::HashSet<_> = candidates
            .iter()
            .map(|i| sources[*i].group.as_str())
            .collect();
        let mut groups = std::collections::BTreeMap::<&str, f64>::new();
        for source in sources
            .iter()
            .filter(|s| s.video == video && available_groups.contains(s.group.as_str()))
        {
            *groups.entry(&source.group).or_default() += source.weight;
        }
        let mut group_ticket = rng.unit() * groups.values().sum::<f64>();
        let mut selected_group = *groups.keys().last().ok_or("No selected keyword group")?;
        for (group, weight) in groups {
            group_ticket -= weight;
            if group_ticket < 0. {
                selected_group = group;
                break;
            }
        }
        candidates.retain(|i| sources[*i].group == selected_group);
        let sum: f64 = candidates
            .iter()
            .map(|i| sources[*i].weight / (1.0 + uses[*i] as f64))
            .sum();
        let mut ticket = rng.unit() * sum;
        let mut picked = *candidates.last().ok_or("No source available")?;
        for index in candidates {
            ticket -= sources[index].weight / (1.0 + uses[index] as f64);
            if ticket < 0.0 {
                picked = index;
                break;
            }
        }
        let range = if video {
            profile.video_seconds
        } else {
            profile.image_seconds
        };
        let mut duration = ((range[0] + rng.unit() * (range[1] - range[0])) * 1e6).round() as u64;
        if video {
            duration = duration.min(sources[picked].duration_us);
        }
        let next_pin = profile
            .pins
            .iter()
            .find(|p| p.start_us > at)
            .map(|p| p.start_us)
            .unwrap_or(total_us);
        let remaining = next_pin - at;
        // Avoid a tiny final sliver by splitting the last two shots evenly.
        let min_us = (range[0] * 1e6) as u64;
        if remaining > duration && remaining - duration < min_us {
            duration = remaining / 2;
        }
        duration = duration.min(remaining).max(1);
        let trim = if video {
            (rng.unit() * (sources[picked].duration_us - duration) as f64) as u64
        } else {
            0
        };
        plan.shots.push(Placement {
            source: picked,
            start_us: at,
            duration_us: duration,
            trim_us: trim,
        });
        uses[picked] += 1;
        at += duration;
        if video {
            video_run += 1;
            image_run = 0;
        } else {
            image_run += 1;
            video_run = 0;
        }
    }
    Ok(plan)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sources() -> Vec<Source> {
        (0..100)
            .map(|i| Source {
                id: i.to_string(),
                group: "all".into(),
                video: i % 2 == 0,
                duration_us: 15_000_000,
                weight: 1.0,
            })
            .collect()
    }
    #[test]
    fn deterministic_exact_fit_and_opening_bias() {
        let p = Profile::default();
        let s = sources();
        let a = generate(&s, 1_200_000_000, &p).unwrap();
        assert_eq!(a.shots, generate(&s, 1_200_000_000, &p).unwrap().shots);
        let mut end = 0;
        let mut run = 0;
        for shot in &a.shots {
            assert_eq!(shot.start_us, end);
            end += shot.duration_us;
            if s[shot.source].video {
                run += 1;
                assert!(run <= 2);
                assert!(shot.trim_us + shot.duration_us <= 15_000_000);
            } else {
                run = 0;
            }
        }
        assert_eq!(end, 1_200_000_000);
        let rate = |start, end| {
            let shots: Vec<_> = a
                .shots
                .iter()
                .filter(|x| x.start_us >= start && x.start_us < end)
                .collect();
            shots.iter().filter(|x| s[x.source].video).count() as f64 / shots.len() as f64
        };
        assert!(rate(0, 300_000_000) > rate(900_000_000, 1_200_000_000));
        assert!(
            a.shots
                .windows(2)
                .any(|p| s[p[0].source].video && s[p[1].source].video)
        );
    }
    #[test]
    fn extreme_ratios_and_single_media_kinds() {
        for ratio in [0.0, 0.2, 0.5, 1.0] {
            let mut p = Profile::default();
            p.max_videos = 0;
            p.curve = vec![
                Anchor {
                    at: 0.0,
                    video: ratio,
                },
                Anchor {
                    at: 1.0,
                    video: ratio,
                },
            ];
            let s = sources();
            let a = generate(&s, 600_000_000, &p).unwrap();
            let stats = a.stats(&s);
            if ratio == 0.0 {
                assert_eq!(stats.videos, 0);
            }
            if ratio == 1.0 {
                assert_eq!(stats.images, 0);
            }
            let actual = stats.video_us as f64 / 600_000_000.0;
            assert!((actual - ratio).abs() < 0.12);
            for video in [true, false] {
                let mut q = p.clone();
                q.max_videos = 0;
                let subset: Vec<_> = s.iter().filter(|x| x.video == video).cloned().collect();
                assert!(!generate(&subset, 17_370_000, &q).unwrap().shots.is_empty());
            }
        }
    }
    #[test]
    fn invalid_and_exhausted_profiles_are_errors() {
        let mut p = Profile::default();
        p.curve[1].at = 0.0;
        assert!(p.validate().is_err());
        p = Profile::default();
        p.allow_repeats = false;
        assert!(generate(&sources()[..2], 600_000_000, &p).is_err());
        assert!(Profile::from_json("{\"version\":99}").is_err());
    }
    #[test]
    fn versioned_round_trip_and_seed_changes() {
        let p = Profile::default();
        assert_eq!(
            p,
            Profile::from_json(&serde_json::to_string(&p).unwrap()).unwrap()
        );
        let mut q = p.clone();
        q.seed += 1;
        assert_ne!(
            generate(&sources(), 600_000_000, &p).unwrap().shots,
            generate(&sources(), 600_000_000, &q).unwrap().shots
        );
    }
}

#[cfg(test)]
mod pin_tests {
    use super::*;
    #[test]
    fn regeneration_preserves_pinned_source_timing_and_trims() {
        let sources = vec![
            Source {
                id: "photo".into(),
                group: "all".into(),
                video: false,
                duration_us: 0,
                weight: 1.0,
            },
            Source {
                id: "video".into(),
                group: "all".into(),
                video: true,
                duration_us: 20_000_000,
                weight: 1.0,
            },
        ];
        let mut p = Profile::default();
        p.max_videos = 0;
        p.pins = vec![Pin {
            source_id: "video".into(),
            start_us: 10_000_000,
            duration_us: 3_000_000,
            trim_us: 7_000_000,
        }];
        for seed in 1..4 {
            p.seed = seed;
            let plan = generate(&sources, 60_000_000, &p).unwrap();
            let pin = plan
                .shots
                .iter()
                .find(|s| s.start_us == 10_000_000)
                .unwrap();
            assert_eq!(
                (pin.source, pin.duration_us, pin.trim_us),
                (1, 3_000_000, 7_000_000)
            );
            assert_eq!(
                plan.shots.last().map(|s| s.start_us + s.duration_us),
                Some(60_000_000)
            );
        }
        p.pins[0].trim_us = 19_000_000;
        assert!(generate(&sources, 60_000_000, &p).is_err());
    }
}
