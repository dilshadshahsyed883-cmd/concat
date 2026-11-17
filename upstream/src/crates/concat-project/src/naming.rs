// SPDX-License-Identifier: AGPL-3.0-or-later
//! Portable display/output names. Internal job IDs and snapshot paths never use these names.
use std::path::Path;

/// Leaves room for extensions, collision suffixes and normal Windows path components.
pub const MAX_BASENAME_UTF16: usize = 120;
fn limited(name: &str, limit: usize) -> String {
    let mut used = 0;
    name.chars()
        .take_while(|c| {
            used += c.len_utf16();
            used <= limit
        })
        .collect()
}
fn reserved(name: &str) -> bool {
    let head = name.split('.').next().unwrap_or("").to_uppercase();
    matches!(
        head.as_str(),
        "CON" | "PRN" | "AUX" | "NUL" | "CLOCK$" | "CONIN$" | "CONOUT$"
    ) || ["COM", "LPT"].iter().any(|prefix| {
        head.strip_prefix(prefix).is_some_and(|tail| {
            matches!(
                tail,
                "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
            )
        })
    })
}
/// Sanitize a logical name once, keeping Unicode and internal dots intact.
/// Invalid Windows characters become underscores, device names get a prefix,
/// and truncation always ends at a complete Unicode scalar/UTF-16 pair.
pub fn basename(name: &str) -> String {
    let clean: String = name
        .trim()
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    let clean = clean.trim_end_matches(['.', ' ']);
    let mut out = limited(clean, MAX_BASENAME_UTF16)
        .trim_end_matches(['.', ' '])
        .to_owned();
    if out.is_empty() || out == "." || out == ".." {
        out = "Untitled".into();
    }
    if reserved(&out) {
        out = format!("_{}", limited(&out, MAX_BASENAME_UTF16 - 1));
    }
    out
}
/// The basename of a narration source, removing only its final extension.
/// Windows paths are understood even in headless Linux tests.
pub fn audio_stem(path: &Path) -> String {
    let path = path.to_string_lossy();
    let name = path.rsplit(['/', '\\']).next().unwrap_or("");
    let stem = name
        .rfind('.')
        .filter(|i| *i > 0)
        .map(|i| &name[..i])
        .unwrap_or(name);
    basename(stem)
}
fn filename(name: &str, extension: &str) -> String {
    let trimmed = name.trim();
    let stem = if trimmed.len() >= extension.len()
        && trimmed
            .get(trimmed.len() - extension.len()..)
            .is_some_and(|tail| tail.eq_ignore_ascii_case(extension))
    {
        &trimmed[..trimmed.len() - extension.len()]
    } else {
        trimmed
    };
    format!("{}{extension}", basename(stem))
}
/// Friendly ZIP filename; does not alter the unique internal snapshot path.
pub fn zip_name(name: &str) -> String {
    filename(name, ".zip")
}
/// Friendly MP4 filename; does not alter the unique internal worker output path.
pub fn mp4_name(name: &str) -> String {
    filename(name, ".mp4")
}
/// Collision suffix. The first candidate is unnumbered; subsequent candidates
/// stay within the same UTF-16 bound without dropping or splitting the suffix.
pub fn numbered(base: &str, n: u32) -> String {
    let safe = basename(base);
    if n <= 1 {
        return safe;
    }
    let suffix = format!(" ({n})");
    let limit = MAX_BASENAME_UTF16 - suffix.encode_utf16().count();
    format!(
        "{}{suffix}",
        limited(&safe, limit).trim_end_matches(['.', ' '])
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn narration_stems_keep_unicode_spaces_and_internal_dots() {
        assert_eq!(
            audio_stem(Path::new(r"C:\Voices\Alex Eala.final.v2.mp3")),
            "Alex Eala.final.v2"
        );
        assert_eq!(audio_stem(Path::new("/voices/آواز test.wav")), "آواز test");
        assert_eq!(audio_stem(Path::new(".voice")), ".voice");
    }
    #[test]
    fn windows_invalid_reserved_and_empty_names_are_safe() {
        assert_eq!(basename(" a<b>:c\"d/e\\f|g?h*i\n "), "a_b__c_d_e_f_g_h_i");
        for name in [
            "CON",
            "con.txt",
            "LPT9",
            "COM¹.txt",
            "NUL",
            "AUX",
            "CONOUT$",
        ] {
            assert!(basename(name).starts_with('_'));
        }
        for name in ["", " ", ".", "..", "... "] {
            assert_eq!(basename(name), "Untitled");
        }
        assert_eq!(basename("COM10"), "COM10");
        assert_eq!(basename("song... "), "song");
    }
    #[test]
    fn utf16_limits_never_split_emoji_or_collision_suffixes() {
        let long = "🎾".repeat(100);
        let safe = basename(&long);
        assert_eq!(safe.encode_utf16().count(), 120);
        let unique = numbered(&long, u32::MAX);
        assert!(unique.encode_utf16().count() <= 120);
        assert!(unique.ends_with(" (4294967295)"));
        assert_eq!(numbered("My Song", 1), "My Song");
        assert_eq!(numbered("My Song", 2), "My Song (2)");
    }
    #[test]
    fn filenames_preserve_basename_and_avoid_repeated_extensions() {
        assert_eq!(zip_name("voice.final"), "voice.final.zip");
        assert_eq!(mp4_name("voice.final"), "voice.final.mp4");
        assert_eq!(mp4_name("My Video.MP4"), "My Video.mp4");
        assert_eq!(zip_name("My Video.zip"), "My Video.zip");
        assert_eq!(mp4_name("CON"), "_CON.mp4");
        assert_eq!(mp4_name("🎾a"), "🎾a.mp4");
    }
}
