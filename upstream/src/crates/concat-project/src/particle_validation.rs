// SPDX-License-Identifier: AGPL-3.0-or-later
//! Existing particle worker constraints for editor-authored parameters.
use crate::model::AppliedFilter;

/// Bounds shared with the particle worker's existing parameter contract.
pub fn bounds(key: &str) -> Option<(f64, f64)> {
    Some(match key {
        "density" | "size_variance" => (0.0, 100.0),
        "size" => (0.001, 0.04),
        "speed" | "glow" => (0.0, 2.0),
        "direction" => (-180.0, 180.0),
        "seed" => (0.0, 65535.0),
        "lifetime" => (0.2, 60.0),
        "fade" | "twinkle" => (0.0, 10.0),
        "opacity" | "red" | "green" | "blue" => (0.0, 1.0),
        "points" => (4.0, 6.0),
        "start" | "end" => (0.0, 86400.0),
        _ => return None,
    })
}

fn value_valid(key: &str, value: f64) -> bool {
    bounds(key).is_some_and(|(lo, hi)| value.is_finite() && (lo..=hi).contains(&value))
        && (key != "seed" || value.fract() == 0.0)
        && (key != "points" || value == 4.0 || value == 6.0)
}

/// Whether the edited particle entry satisfies static and keyframe constraints.
/// Other effect families keep their existing validation policy.
pub fn valid(entry: &AppliedFilter) -> bool {
    if !entry.id.starts_with("concat.particles-") { return true; }
    if entry.params.iter().any(|(key, value)| !value_valid(key, *value)) { return false; }
    let start = entry.params.get("start").copied().unwrap_or(0.0);
    let end = entry.params.get("end").copied().unwrap_or(86400.0);
    if end <= start { return false; }
    entry.keys.iter().all(|(name, keys)| {
        bounds(name).is_some() && !matches!(name.as_str(), "seed" | "points" | "start" | "end")
            && keys.len() <= 2048
            && keys.iter().enumerate().all(|(index, key)| {
                key.at.is_finite() && (0.0..=1.0).contains(&key.at)
                    && value_valid(name, key.value) && key.ease.0.iter().all(|v| v.is_finite())
                    && !keys[..index].iter().any(|previous| previous.at == key.at)
            })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ParamKey, KeyEase};
    #[test]
    fn rejects_worker_invalid_discrete_ranges_and_time_windows() {
        let mut entry = AppliedFilter::new("concat.particles-stars");
        assert!(valid(&entry));
        for (key, value) in [("seed", 1.5), ("points", 5.0), ("opacity", 1.01), ("unknown", 1.0)] {
            entry.params.insert(key.into(), value);
            assert!(!valid(&entry), "{key}");
            entry.params.clear();
        }
        entry.params.insert("start".into(), 10.0);
        entry.params.insert("end".into(), 5.0);
        assert!(!valid(&entry));
        entry.params.insert("end".into(), 10.0);
        assert!(!valid(&entry));
        entry.params.insert("end".into(), 11.0);
        entry.params.insert("seed".into(), 65535.0);
        entry.params.insert("points".into(), 6.0);
        assert!(valid(&entry));
        entry.keys.insert("seed".into(), vec![ParamKey { at: 0.0, value: 1.0, ease: KeyEase::LINEAR }]);
        assert!(!valid(&entry));
        entry.keys.clear();
        entry.keys.insert("opacity".into(), vec![ParamKey { at: 0.0, value: 0.5, ease: KeyEase::IN_OUT }]);
        assert!(valid(&entry));
    }
}
