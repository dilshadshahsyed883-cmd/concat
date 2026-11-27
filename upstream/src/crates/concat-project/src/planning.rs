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
    /// Optional overall video-duration target (0–100 percent).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub overall_video_percent: Option<u32>,
    /// Guaranteed video-only prefix; overall ratio applies to the remainder.
    #[serde(default)]
    pub video_intro_seconds: u32,
    /// Independent video keyword budget for a dedicated intro library.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intro_selection: Option<MediaSelection>,
    pub seed: u64,
    #[serde(default)]
    pub pins: Vec<Pin>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub media_selection: Option<MediaSelection>,
}

/// Shared integer-percentage budget for one kind of C Builder keyword.
/// Disabled entries have zero share. BTreeMap ordering breaks ties.
fn allocate_keyword_budget(
    entries: &mut std::collections::BTreeMap<String, KeywordSelection>,
    keys: &[String],
    budget: u32,
) {
    if keys.is_empty() {
        return;
    }
    let sum: u64 = keys.iter().map(|key| entries[key].weight as u64).sum();
    let equal = sum == 0;
    let denominator = if equal { keys.len() as u64 } else { sum };
    let mut shares: Vec<(String, u32, u64)> = keys
        .iter()
        .map(|key| {
            let weight = if equal { 1 } else { entries[key].weight as u64 };
            let numerator = weight * budget as u64;
            (
                key.clone(),
                (numerator / denominator) as u32,
                numerator % denominator,
            )
        })
        .collect();
    let allocated: u32 = shares.iter().map(|entry| entry.1).sum();
    let mut ranks: Vec<usize> = (0..shares.len()).collect();
    // Stable ties by keyword, independently of arrival order or UI recycling.
    ranks.sort_by(|a, b| {
        shares[*b]
            .2
            .cmp(&shares[*a].2)
            .then_with(|| shares[*a].0.cmp(&shares[*b].0))
    });
    for index in ranks.into_iter().take((budget - allocated) as usize) {
        shares[index].1 += 1;
    }
    for (key, value, _) in shares {
        entries.get_mut(&key).expect("known keyword").weight = value;
    }
}

impl MediaSelection {
    /// Canonicalize legacy shares; disabled rows are always zero.
    pub fn normalize(&mut self) {
        for entries in [&mut self.images, &mut self.videos] {
            for row in entries.values_mut() {if !row.selected {row.weight=0;}}
            let keys: Vec<String> = entries
                .iter()
                .filter(|(_, row)| row.selected)
                .map(|(key, _)| key.clone())
                .collect();
            // All-zero groups are intentionally excluded, not silently enabled.
            if keys.iter().any(|key| entries[key].weight > 0) {
                allocate_keyword_budget(entries, &keys, 100);
            }
        }
    }

    /// Apply exactly one slider/checkbox transaction, then publish one coherent
    /// normalized model. Independent media kinds are never cross-modified.
    pub fn change_keyword(&mut self, video: bool, keyword: &str, selected: bool, requested: u32) {
        self.normalize();
        let entries = if video {
            &mut self.videos
        } else {
            &mut self.images
        };
        let old = entries.get(keyword).cloned().unwrap_or_default();
        if !selected {
            // Deselected rows immediately release their entire share.
            entries.insert(
                keyword.into(),
                KeywordSelection {
                    selected: false,
                    weight: 0,
                },
            );
            let peers: Vec<String> = entries
                .iter()
                .filter(|(_, v)| v.selected)
                .map(|(k, _)| k.clone())
                .collect();
            allocate_keyword_budget(entries, &peers, 100);
            return;
        }
        let peers: Vec<String> = entries
            .iter()
            .filter(|(k, v)| v.selected && k.as_str() != keyword)
            .map(|(k, _)| k.clone())
            .collect();
        let desired = if peers.is_empty() {
            100
        } else if !old.selected && (requested == 0 || requested == old.weight) {
            // A newly enabled row receives an equal initial share.
            100 / (peers.len() as u32 + 1)
        } else {
            requested.min(100)
        };
        entries.insert(
            keyword.into(),
            KeywordSelection {
                selected: true,
                weight: desired,
            },
        );
        allocate_keyword_budget(entries, &peers, 100 - desired);
    }

    /// One-click equal weighting for enabled groups of the requested type.
    pub fn equalize(&mut self, video: bool) {
        let entries = if video {
            &mut self.videos
        } else {
            &mut self.images
        };
        for row in entries.values_mut(){if !row.selected{row.weight=0;}}
        let peers: Vec<String> = entries
            .iter()
            .filter(|(_, v)| v.selected)
            .map(|(k, _)| k.clone())
            .collect();
        for key in &peers {
            entries.get_mut(key).expect("known keyword").weight = 1;
        }
        allocate_keyword_budget(entries, &peers, 100);
    }
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
            overall_video_percent: None,
            video_intro_seconds: 0,
            intro_selection: None,
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
        if self.overall_video_percent.is_some_and(|v| v > 100) {return Err("Overall video ratio must be 0–100".into());}
        if self.cooldown > 10000 || self.max_videos > 10000 || self.max_images > 10000 {
            return Err("Sequence controls exceed safety limits".into());
        }
        for selection in self.media_selection.iter().chain(self.intro_selection.iter()) {
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
    generate_with_intro_pool(sources,total_us,profile,None)
}
/// With a dedicated library, indexes at or after the split are intro-only.
pub fn generate_with_intro_pool(sources: &[Source], total_us: u64, profile: &Profile, intro_split:Option<usize>) -> Result<Plan,String> {
    profile.validate()?;
    if intro_split.is_some_and(|i|i>sources.len()){return Err("Invalid intro source pool".into());}
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
    let intro_us=u64::from(profile.video_intro_seconds)*1_000_000;
    if intro_us>total_us/3 {return Err("Video-only intro cannot exceed one third of narration duration".into());}
    if intro_us>0 && !profile.pins.is_empty(){return Err("Video-only intro cannot be combined with pinned shots".into());}
    let body_us=total_us-intro_us;
    let target=profile.overall_video_percent.map(|v|body_us*v as u64/100);
    let curve_mean: f64=profile.curve.windows(2).map(|p|(p[1].at-p[0].at)*(p[0].video+p[1].video)/2.0).sum();
    let adjusted=|v:f64,t:f64| if t<=curve_mean {if curve_mean>0.0 {v*t/curve_mean}else{0.0}} else if curve_mean<1.0 {v+(1.0-v)*(t-curve_mean)/(1.0-curve_mean)}else{1.0};
    // Integrate the rescaled distribution to retain opening/ending shape.
    let cumulative=|x:f64,t:f64|->f64{
        profile.curve.windows(2).map(|p|{
            let end=x.min(p[1].at);let span=(end-p[0].at).max(0.0);
            let a=adjusted(p[0].video,t);let b=adjusted(p[1].video,t);
            span*(a+(b-a)*span/(p[1].at-p[0].at)/2.0)
        }).sum()
    };
    let mut video_us=0_u64;
    let body_single_kind=target.filter(|v|*v==0||*v==body_us).map(|v|v==body_us);
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
        let in_intro=at<intro_us;
        let in_pool=|i:usize|intro_split.is_none_or(|split|(i>=split)==in_intro);
        let single_kind=if in_intro{Some(true)}else{body_single_kind};
        let target=if in_intro{None}else{target};
        let local_at=at.saturating_sub(intro_us);
        if plan.shots.len() >= 1_000_000 {
            return Err("Plan exceeds one million shots".into());
        }
        if let Some(pin) = profile.pins.iter().find(|p| p.start_us == at) {
            let index = sources
                .iter()
                .position(|s| s.id == pin.source_id)
                .ok_or("Pinned source missing")?;
            let video = sources[index].video;
            if single_kind.is_some_and(|kind|kind!=video) || (!profile.allow_repeats && uses[index] > 0)
                || (single_kind.is_none() && video && profile.max_videos > 0 && video_run >= profile.max_videos)
                || (single_kind.is_none() && !video && profile.max_images > 0 && image_run >= profile.max_images)
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
                video_us += pin.duration_us;
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
                    in_pool(*i) && s.video == video
                        && single_kind.is_none_or(|kind|kind==video)
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
            !images.is_empty() && (single_kind.is_some() || profile.max_images == 0 || image_run < profile.max_images);
        let can_video =
            !videos.is_empty() && (single_kind.is_some() || profile.max_videos == 0 || video_run < profile.max_videos);
        if !can_image && !can_video {
            if in_intro{return Err("Video-only intro requires eligible selected video sources for its entire duration".into());}
            return Err(format!(
                "Hard sequence/repeat constraints exhausted at {:.3}s; adjust profile",
                at as f64 / 1e6
            ));
        }
        let mut weight = profile.video_weight(local_at as f64 / body_us as f64);
        if profile.ratio_by_seconds {
            let image_mean = (profile.image_seconds[0] + profile.image_seconds[1]) / 2.0;
            let video_mean = (profile.video_seconds[0] + profile.video_seconds[1]) / 2.0;
            weight = (weight / video_mean) / (weight / video_mean + (1.0 - weight) / image_mean);
        }
        let want_video = if in_intro {true} else if let Some(target)=target {
            let t=target as f64/body_us as f64;
            let mean=((profile.image_seconds[0]+profile.image_seconds[1]+profile.video_seconds[0]+profile.video_seconds[1])/4.0*1e6).max(1.0);
            let expected=cumulative(((local_at as f64+mean/2.0)/body_us as f64).min(1.0),t)*body_us as f64;
            let deficit=expected-video_us as f64;
            let local=adjusted(profile.video_weight(local_at as f64/body_us as f64),t);
            let probability=(local+deficit/mean).clamp(0.0,1.0);
            if video_us>=target {false} else if local_at.saturating_sub(video_us)>=body_us-target {true} else {rng.unit()<probability}
        }else{rng.unit()<weight};
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
        for (_,source) in sources
            .iter().enumerate()
            .filter(|(i,s)| in_pool(*i) && s.video == video && available_groups.contains(s.group.as_str()))
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
        let remaining = (if in_intro{next_pin.min(intro_us)}else{next_pin}) - at;
        // Avoid a tiny final sliver by splitting the last two shots evenly.
        let min_us = (range[0] * 1e6) as u64;
        if remaining > duration && remaining - duration < min_us {
            duration = remaining / 2;
        }
        duration = duration.min(remaining).max(1);
        if let Some(target)=target {
            let budget=if video {target.saturating_sub(video_us)} else {(body_us-target).saturating_sub(local_at-video_us)};
            if budget>=min_us && duration>budget {duration=budget;}
        }
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
            if !in_intro {video_us += duration;}
            video_run += 1;
            image_run = 0;
        } else {
            image_run += 1;
            video_run = 0;
        }
        if in_intro && at==intro_us {video_run=0;image_run=0;}
    }
    if let Some(target)=target {
        if video_us.abs_diff(target)>body_us/200 {
            plan.warnings.push(format!("Overall video target {:.1}% could not be met within shot/source/run constraints; actual {:.1}%",target as f64/body_us as f64*100.0,video_us as f64/body_us as f64*100.0));
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
    fn dedicated_intro_pool_isolated_from_body_even_with_same_keyword() {
        let s=sources();
        let mut p=Profile::default();
        p.video_intro_seconds=20;p.overall_video_percent=Some(50);
        let a=generate_with_intro_pool(&s,90_000_000,&p,Some(50)).unwrap();
        assert_eq!(a.shots,generate_with_intro_pool(&s,90_000_000,&p,Some(50)).unwrap().shots);
        for shot in a.shots {
            assert_eq!(shot.source>=50,shot.start_us<20_000_000);
            if shot.source>=50{assert!(s[shot.source].video);assert!(shot.start_us+shot.duration_us<=20_000_000);}
        }
        assert!(generate_with_intro_pool(&s,90_000_000,&p,Some(s.len())).is_err());
        p.video_intro_seconds=0;
        assert!(generate_with_intro_pool(&s,90_000_000,&p,Some(50)).unwrap().shots.iter().all(|s|s.source<50));
    }
    #[test]
    fn video_only_intro_exact_boundary_and_remaining_ratio() {
        let s=sources();
        let mut p=Profile::default();
        p.video_intro_seconds=30;
        p.overall_video_percent=Some(0);
        p.max_videos=1;
        let a=generate(&s,120_000_000,&p).unwrap();
        assert_eq!(a.shots,generate(&s,120_000_000,&p).unwrap().shots);
        let mut end=0;
        for shot in &a.shots {
            assert_eq!(shot.start_us,end);
            end+=shot.duration_us;
            if shot.start_us<30_000_000 {
                assert!(s[shot.source].video);
                assert!(end<=30_000_000);
            }else{assert!(!s[shot.source].video);}
        }
        assert_eq!(end,120_000_000);
        assert_eq!(a.stats(&s).video_us,30_000_000);
        p.video_intro_seconds=41;
        assert!(generate(&s,120_000_000,&p).is_err());
        p.video_intro_seconds=30;
        let images:Vec<_>=s.into_iter().filter(|s|!s.video).collect();
        assert!(matches!(generate(&images,120_000_000,&p),Err(e) if e.contains("Video-only intro")));
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
    fn overall_duration_target_rescales_curve_and_honors_extremes() {
        let pool=sources();
        for ratio in [0,30,65,100] {
            let mut p=Profile::default();p.overall_video_percent=Some(ratio);p.max_videos=0;p.max_images=0;
            let plan=generate(&pool,600_000_000,&p).unwrap();let actual=plan.stats(&pool).video_us as f64/600_000_000.0;
            assert!((actual-ratio as f64/100.0).abs()<0.02,"ratio {ratio}: {actual}");
            if ratio==0||ratio==100 {assert_eq!(actual,ratio as f64/100.0);}
        }
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

#[cfg(test)]
mod keyword_budget_tests {
    use super::*;
    fn row(selected: bool, weight: u32) -> KeywordSelection {
        KeywordSelection { selected, weight }
    }
    fn shares(selection: &MediaSelection, video: bool) -> Vec<u32> {
        let entries = if video {
            &selection.videos
        } else {
            &selection.images
        };
        entries
            .values()
            .filter(|entry| entry.selected)
            .map(|entry| entry.weight)
            .collect()
    }
    #[test]
    fn coupled_drag_and_independent_budgets() {
        let mut s = MediaSelection::default();
        for key in ["a", "b", "c", "d"] {
            s.images.insert(key.into(), row(true, 100));
            s.videos.insert(key.into(), row(true, 100));
        }
        s.normalize();
        assert_eq!(shares(&s, false), vec![25, 25, 25, 25]);
        assert_eq!(shares(&s, true), vec![25, 25, 25, 25]);
        s.change_keyword(false, "a", true, 40);
        assert_eq!(shares(&s, false), vec![40, 20, 20, 20]);
        assert_eq!(shares(&s, true), vec![25, 25, 25, 25]);
        s.change_keyword(false, "a", true, 10);
        assert_eq!(shares(&s, false), vec![10, 30, 30, 30]);
        assert_eq!(shares(&s, true), vec![25, 25, 25, 25]);
    }
    #[test]
    fn rounding_and_unequal_shares_are_stable() {
        let mut s = MediaSelection::default();
        for (key, weight) in [("a", 10), ("b", 30), ("c", 60)] {
            s.images.insert(key.into(), row(true, weight));
        }
        s.change_keyword(false, "a", true, 39);
        assert_eq!(shares(&s, false), vec![39, 20, 41]);
        assert_eq!(shares(&s, false).iter().sum::<u32>(), 100);
        let once = s.clone();
        for _ in 0..10 {
            s.normalize();
        }
        assert_eq!(s, once);
    }
    #[test]
    fn disable_reenable_zero_hundred_and_equalize() {
        let mut s = MediaSelection::default();
        for key in ["a", "b", "c", "d"] {
            s.images.insert(key.into(), row(true, 25));
        }
        s.change_keyword(false, "a", false, 25);
        assert_eq!(shares(&s, false), vec![34, 33, 33]);
        assert_eq!(s.images["a"].weight, 0);
        s.change_keyword(false, "a", true, 25);
        assert_eq!(shares(&s, false), vec![25, 25, 25, 25]);
        s.change_keyword(false, "a", true, 100);
        assert_eq!(shares(&s, false), vec![100, 0, 0, 0]);
        s.change_keyword(false, "a", true, 0);
        assert_eq!(shares(&s, false), vec![0, 34, 33, 33]);
        s.equalize(false);
        assert_eq!(shares(&s, false), vec![25, 25, 25, 25]);
        for key in ["a", "b", "c", "d"] {
            s.change_keyword(false, key, false, 0);
        }
        assert!(shares(&s, false).is_empty());
        s.change_keyword(false, "a", true, 0);
        assert_eq!(shares(&s, false), vec![100]);
        s.change_keyword(false, "b", true, 0);
        // A re-enabled group receives an equal initial share.
        assert_eq!(shares(&s, false), vec![50, 50]);
    }
    #[test]
    fn all_zero_selection_stays_excluded_until_explicit_drag_or_equalize() {
        let mut s = MediaSelection::default();
        for key in ["a", "b"] {
            s.images.insert(key.into(), row(true, 0));
        }
        s.normalize();
        assert_eq!(shares(&s, false), vec![0, 0]);
        s.change_keyword(false, "a", true, 40);
        assert_eq!(shares(&s, false), vec![40, 60]);
    }
    #[test]
    fn legacy_profiles_round_trip_deterministically() {
        let mut p = Profile::default();
        let mut s = MediaSelection::default();
        for key in ["a", "b", "c", "d"] {
            s.images.insert(key.into(), row(true, 100));
        }
        s.videos.insert("unused".into(), row(false, 87));
        p.media_selection = Some(s);
        let original = serde_json::to_string(&p).unwrap();
        let mut loaded = Profile::from_json(&original).unwrap();
        assert_eq!(
            loaded.media_selection.as_ref().unwrap().images["a"].weight,
            100
        );
        loaded.media_selection.as_mut().unwrap().normalize();
        let normalized = serde_json::to_string(&loaded).unwrap();
        assert_eq!(
            shares(loaded.media_selection.as_ref().unwrap(), false),
            vec![25, 25, 25, 25]
        );
        assert_eq!(
            normalized,
            serde_json::to_string(&Profile::from_json(&normalized).unwrap()).unwrap()
        );
        assert_eq!(
            loaded.media_selection.as_ref().unwrap().videos["unused"].weight,
            0
        );
    }
}
