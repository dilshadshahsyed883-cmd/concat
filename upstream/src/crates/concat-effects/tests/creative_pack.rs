// SPDX-License-Identifier: AGPL-3.0-or-later
//! Parse the new packages and validate their full WGSL interfaces without a GPU.
use concat_effects::Package;

#[test]
fn creative_packages_load_with_valid_shader_contracts() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("packages");
    for slug in [
        "diagonal-wipe",
        "door-reveal",
        "paper-rip-vertical",
        "paper-rip-diagonal",
        "double-paper-tear",
        "paper-unfold",
        "brush-stroke",
        "page-curl",
        "clock-wipe",
    ] {
        let path = root.join(format!("concat.{slug}"));
        let manifest = std::fs::read_to_string(path.join("effect.toml")).unwrap();
        let shader = std::fs::read_to_string(path.join("effect.wgsl")).unwrap();
        let package = Package::from_sources(&manifest, None, Some(&shader))
            .unwrap_or_else(|e| panic!("{slug}: {e}"));
        assert!(
            package.transition().is_some(),
            "{slug} must have a native preview shader"
        );
        assert!(package.check_fixtures().is_empty());
    }
}
