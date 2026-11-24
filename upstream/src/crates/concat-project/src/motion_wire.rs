// SPDX-License-Identifier: AGPL-3.0-or-later
//! Versioned, lossless storage of generated motion. In-memory editing continues
//! to use native keys. The worker builds the very same generator, never a
//! second implementation of the animation formulas.
use serde_json::{Value, json};
use crate::canvas_motion::{CanvasMotionControls, CanvasMotionPreset};
use crate::model::{Clip, ClipKey};
use crate::motion_preset::{MotionPreset, OWNERSHIP_KEY};
use crate::physics_motion::{PhysicsControls, PhysicsPreset};

const MARKER: &str = "parameterOnly";
const MAX_EXPANDED_KEYS: usize = 2_000_000;
const BASIC: [MotionPreset; 11] = [
    MotionPreset::PushIn, MotionPreset::PanRight, MotionPreset::FadeIn,
    MotionPreset::PushOut, MotionPreset::PanLeft, MotionPreset::PanUp,
    MotionPreset::PanDown, MotionPreset::FadeOut, MotionPreset::RotateLeft,
    MotionPreset::RotateRight, MotionPreset::OpacityPulse,
];

/// Capture the actual inputs used to generate the editable native keys.
pub fn recipe(frame: f64, controls: impl serde::Serialize) -> Value {
    json!({"version": 1, "frameSeconds": frame, "controls": controls})
}

fn regenerate(value: &Value, preset: &str, recipe: &Value) -> Option<Vec<ClipKey>> {
    if recipe.get("version")?.as_u64()? != 1 { return None; }
    let frame = recipe.get("frameSeconds")?.as_f64()?;
    if !frame.is_finite() || frame <= 0.0 { return None; }
    let controls = recipe.get("controls")?.clone();
    let mut clip: Clip = serde_json::from_value(value.clone()).ok()?;
    clip.keys.clear();
    let keys = if let Some(p) = BASIC.into_iter().find(|p| p.id() == preset) {
        let c: PhysicsControls = serde_json::from_value(controls).ok()?;
        if !c.valid() { return None; }
        p.keys_with(&clip, frame, c.strength, c.duration, c.speed).ok()?.0
    } else if let Some(p) = PhysicsPreset::ALL.into_iter().find(|p| p.id() == preset) {
        let c: PhysicsControls = serde_json::from_value(controls).ok()?;
        if !c.valid() { return None; }
        p.keys_with(&clip, frame, c).ok()?.0
    } else {
        let p = CanvasMotionPreset::ALL.into_iter().find(|p| p.id() == preset)?;
        let c: CanvasMotionControls = serde_json::from_value(controls).ok()?;
        p.keys(&clip, frame, c).ok()?
    };
    // This is precisely the canonicalization used by replace_generated.
    clip.keys = keys;
    let keys = clip.tidy().keys;
    (!keys.is_empty() && keys.len() <= 2048).then_some(keys)
}

fn visit_clips(
    document: &mut Value,
    mut f: impl FnMut(&mut Value) -> Result<(), String>,
) -> Result<(), String> {
    if let Some(clips) = document.get_mut("clips").and_then(Value::as_array_mut) {
        for clip in clips { f(clip)?; }
    }
    if let Some(timelines) = document.get_mut("timelines").and_then(Value::as_array_mut) {
        for timeline in timelines {
            if let Some(clips) = timeline.get_mut("clips").and_then(Value::as_array_mut) {
                for clip in clips { f(clip)?; }
            }
        }
    }
    Ok(())
}

fn compact_clip(clip: &mut Value) -> bool {
    let Some(owner) = clip.get(OWNERSHIP_KEY) else { return false; };
    if owner.get("version").and_then(Value::as_u64) != Some(1) { return false; }
    let Some(keys) = clip.get("keys") else { return false; };
    // Manual edits, rewindowed clips, and legacy presets without original
    // controls retain their exact native keys. Never approximate them.
    if owner.get("keys") != Some(keys) { return false; }
    let Some(preset) = owner.get("preset").and_then(Value::as_str) else { return false; };
    let Some(recipe) = owner.get("recipe") else { return false; };
    let Some(generated) = regenerate(clip, preset, recipe) else { return false; };
    if serde_json::to_value(&generated).ok().as_ref() != Some(keys) { return false; }
    let map = clip.as_object_mut().expect("clip object");
    map.remove("keys");
    let owner = map.get_mut(OWNERSHIP_KEY).and_then(Value::as_object_mut).expect("owner object");
    owner.remove("keys");
    owner.insert(MARKER.into(), Value::Bool(true));
    true
}

/// Write v2 only if at least one generated motion was proven reproducible.
/// Otherwise retain the original v1 wire format, including manual keys.
pub fn compact_document(document: &mut Value) {
    let mut changed = false;
    let _ = visit_clips(document, |clip| {
        changed |= compact_clip(clip);
        Ok(())
    });
    if changed { document["version"] = json!(2); }
}

/// Rehydrate compact keys before the editor or CUDA planner reads them.
/// GPU callers do not need the duplicated editor ownership key vector.
pub fn expand_document(document: &mut Value, include_ownership: bool) -> Result<(), String> {
    let version = crate::doc::document_version(document);
    if version == 1 { return Ok(()); }
    if version != 2 || !document.is_object() {
        return Err("Unsupported parameterized motion document version".into());
    }
    let mut total = 0usize;
    visit_clips(document, |clip| {
        let Some(owner) = clip.get(OWNERSHIP_KEY) else { return Ok(()); };
        let Some(marker) = owner.get(MARKER) else { return Ok(()); };
        if marker != &Value::Bool(true)
            || owner.get("version").and_then(Value::as_u64) != Some(1)
            || owner.get("keys").is_some() || clip.get("keys").is_some()
        {
            return Err("Invalid compact animation ownership record".into());
        }
        let id = clip.get("id").and_then(Value::as_str).unwrap_or("unknown");
        let preset = owner.get("preset").and_then(Value::as_str).ok_or("Missing animation ID")?;
        let recipe = owner.get("recipe").ok_or("Missing animation parameters")?;
        let keys = regenerate(clip, preset, recipe)
            .ok_or_else(|| format!("Cannot regenerate animation {preset} on clip {id}"))?;
        total += keys.len();
        if total > MAX_EXPANDED_KEYS { return Err("Too many expanded animation keys".into()); }
        let keys = serde_json::to_value(keys).map_err(|e| e.to_string())?;
        let map = clip.as_object_mut().ok_or("Invalid animation clip")?;
        map.insert("keys".into(), keys.clone());
        let owner = map.get_mut(OWNERSHIP_KEY).and_then(Value::as_object_mut)
            .ok_or("Invalid animation ownership")?;
        owner.remove(MARKER);
        if include_ownership { owner.insert("keys".into(), keys); }
        Ok(())
    })?;
    document["version"] = json!(1);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ClipKind;
    use crate::motion_preset::{generated_ownership, replace_generated_with_recipe};

    fn check(preset: &str, controls: impl serde::Serialize) {
        let mut clip = Clip::blank("c1", "T1", ClipKind::Image, "image", 0.0, 8.0);
        let recipe = recipe(1.0 / 30.0, controls);
        let keys = regenerate(&serde_json::to_value(&clip).unwrap(), preset, &recipe).unwrap();
        replace_generated_with_recipe(&mut clip, &[], keys, preset.into(), None, Some(recipe)).unwrap();
        let original = serde_json::to_value(&clip).unwrap();
        let mut doc = json!({"version":1,"clips":[original.clone()],"timelines":[{"clips":[original.clone()]}]});
        compact_document(&mut doc);
        assert_eq!(doc["version"], 2, "{preset}");
        assert!(doc["clips"][0].get("keys").is_none(), "{preset}");
        assert!(doc["timelines"][0]["clips"][0][OWNERSHIP_KEY].get("keys").is_none(), "{preset}");
        let mut worker = doc.clone();
        expand_document(&mut worker, false).unwrap();
        assert_eq!(worker["clips"][0]["keys"], original["keys"]);
        expand_document(&mut doc, true).unwrap();
        assert_eq!(doc["clips"][0], original);
        assert!(generated_ownership(&serde_json::from_value(doc["clips"][0].clone()).unwrap()).is_some());
        // A later authored key edit must never be compacted away.
        doc["clips"][0]["keys"][0]["value"] = json!(0.12345);
        let edited = doc["clips"][0].clone();
        compact_document(&mut doc);
        assert_eq!(doc["clips"][0], edited);
    }

    #[test]
    fn every_animation_family_compacts_and_regenerates_exact_native_keys() {
        let physics = PhysicsControls { strength: 1.3, duration: 0.75, speed: 1.2, intensity: 1.1 };
        for preset in BASIC { check(preset.id(), physics); }
        for preset in PhysicsPreset::ALL { check(preset.id(), physics); }
        let canvas = CanvasMotionControls {
            start_scale: 0.82, middle_scale: 0.96, end_scale: 0.72,
            seed: u64::MAX - 17, ..CanvasMotionControls::default()
        };
        for preset in CanvasMotionPreset::ALL { check(preset.id(), canvas); }
    }

    #[test]
    fn legacy_and_manual_keys_stay_native_and_bad_parameters_fail_closed() {
        let mut legacy = json!({"version":1,"clips":[{"keys":[{"property":"scale","at":0,"value":1.0}]}]});
        let original = legacy.clone();
        compact_document(&mut legacy);
        assert_eq!(legacy, original);
        let mut invalid = json!({"version":2,"clips":[{"generatedMotionV1":{
            "version":1,"parameterOnly":true,"preset":"unknown","recipe":{}
        }}]});
        assert!(expand_document(&mut invalid, true).is_err());
    }
}
