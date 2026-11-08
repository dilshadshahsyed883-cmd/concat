// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors

//! Desktop commands for reversible structural compounds.
//! Clips remain real clips on their original tracks; no proxy with duplicate
//! audio or unimplemented parent effects is ever sent to Preview/CUDA.

use super::{Command, Confirm, Studio};
use concat_project::model::{Compound, CompoundMember};
use std::collections::{HashMap, HashSet};

/// Resolve a clip to its outermost visible compound. A clip can be directly
/// owned by an inner compound, which is itself a member of another compound.
/// Context-menu operations must target the same outermost container the user
/// sees, not accidentally edit/delete only its hidden inner membership.
/// Damaged or cyclic graphs fail closed without touching source clips.
fn compound_ancestry(groups: &[Compound], clip_id: &str) -> Option<Vec<String>> {
    let mut current = groups.iter().find(|g| g.members.iter().any(|m| {
        matches!(m, CompoundMember::Clip(id) if id == clip_id)
    }))?.id.clone();
    let mut visited = HashSet::new();
    let mut ancestry = Vec::new();
    while visited.insert(current.clone()) {
        ancestry.push(current.clone());
        match groups.iter().find(|g| g.members.iter().any(|m| {
            matches!(m, CompoundMember::Compound(id) if id == &current)
        })) {
            Some(parent) => current = parent.id.clone(),
            None => return Some(ancestry),
        }
    }
    None
}
fn root_compound_for_clip(groups: &[Compound], clip_id: &str) -> Option<String> {
    compound_ancestry(groups, clip_id)?.last().cloned()
}

/// Recursively collect actual members for the editor-only compound view.
/// Preview and CUDA continue rendering the original timeline clips exactly once.
fn compound_view_clips(groups: &[Compound], root: &str) -> Option<HashSet<String>> {
    let by_id: HashMap<&str, &Compound> =
        groups.iter().map(|g| (g.id.as_str(), g)).collect();
    if !by_id.contains_key(root) { return None; }
    let mut pending = vec![root];
    let mut seen = HashSet::new();
    let mut clips = HashSet::new();
    while let Some(group_id) = pending.pop() {
        if !seen.insert(group_id) { continue; }
        let group = by_id.get(group_id)?;
        for m in &group.members {
            match m {
                CompoundMember::Clip(id) => { clips.insert(id.clone()); },
                CompoundMember::Compound(id) => pending.push(id),
            }
        }
    }
    Some(clips)
}

impl Studio {
    pub(super) fn open_compound_from(&mut self, clicked: &str) {
        // Undo, deletion or a reopened document can invalidate a member view.
        // Recover to the full timeline rather than trapping navigation.
        if self.compound_edit.as_ref().is_some_and(|id|
            !self.timeline().compounds.iter().any(|g| &g.id == id)) {
            self.compound_edit = None;
            self.compound_edit_parents.clear();
        }
        let Some(ancestry) = compound_ancestry(&self.timeline().compounds, clicked) else {
            return;
        };
        let next = match self.compound_edit.as_deref() {
            None => ancestry.last(),
            Some(scope) => ancestry.iter().position(|id| id == scope)
                .and_then(|index| index.checked_sub(1))
                .and_then(|index| ancestry.get(index)),
        };
        let Some(next) = next.cloned() else { return; };
        self.flush_commit();
        if let Some(previous) = self.compound_edit.replace(next) {
            self.compound_edit_parents.push(previous);
        }
        self.selection.clear();
        self.transition_selected = None;
    }

    pub(super) fn leave_compound_edit(&mut self) {
        self.flush_commit();
        // A parent may have been ungrouped while its child was open.
        self.compound_edit = None;
        while let Some(parent) = self.compound_edit_parents.pop() {
            if self.timeline().compounds.iter().any(|g| g.id == parent) {
                self.compound_edit = Some(parent);
                break;
            }
        }
        self.selection.clear();
        self.transition_selected = None;
    }

    pub(super) fn editing_compound_label(&self) -> String {
        self.compound_edit.as_ref()
            .and_then(|id| self.timeline().compounds.iter().find(|g| &g.id == id))
            .map(|g| g.name.clone())
            .unwrap_or_default()
    }

    pub(super) fn visible_compound_clips(&self) -> Option<HashSet<String>> {
        compound_view_clips(&self.timeline().compounds, self.compound_edit.as_deref()?)
    }

    pub(super) fn compound_for_clip(&self, clip_id: &str) -> Option<String> {
        let ancestry = compound_ancestry(&self.timeline().compounds, clip_id)?;
        match self.compound_edit.as_deref() {
            Some(scope) => {
                let index = ancestry.iter().position(|id| id == scope)?;
                Some(ancestry[if index == 0 { 0 } else { index - 1 }].clone())
            }
            None => ancestry.last().cloned(),
        }
    }

    pub(super) fn compound_selected_from(&mut self, clicked: &str) {
        let ids: Vec<String> = if self.selection.iter().any(|id| id == clicked) {
            self.selection.clone()
        } else {
            vec![clicked.to_owned()]
        };
        let ids: Vec<String> = ids.into_iter().filter(|id| self.clip(id).is_some_and(
            |clip| !self.locked(&clip.track_id))).collect();
        if ids.is_empty() { return; }
        let name = format!("Compound {}", self.timeline().compounds.len() + 1);
        self.apply(Command::CreateCompound {
            clip_ids: ids, compound_ids: Vec::new(), name,
        });
    }

    /// Wrap one existing root compound and any free selected clips into a
    /// new parent. The command rejects cycles and multiple ownership.
    pub(super) fn nest_compound_from(&mut self, clicked: &str) {
        let Some(compound_id) = self.compound_for_clip(clicked) else { return; };
        let free_clips = self.selection.iter()
            .filter(|id| self.compound_for_clip(id.as_str()).is_none())
            .cloned()
            .collect();
        let name = format!("Compound {}", self.timeline().compounds.len() + 1);
        self.apply(Command::CreateCompound {
            clip_ids: free_clips, compound_ids: vec![compound_id], name,
        });
    }

    pub(super) fn compound_all_layers(&mut self) {
        let name = format!("Compound {}", self.timeline().compounds.len() + 1);
        self.apply(Command::CreateCompoundAll { name });
    }

    /// Range from the earliest to latest selected item. A boundary crossing
    /// any clip is rejected by the command rather than trimming source data.
    pub(super) fn compound_selection_span(&mut self, clicked: &str) {
        let selected = if self.selection.iter().any(|id| id == clicked) {
            self.selection.clone()
        } else {
            vec![clicked.to_owned()]
        };
        let mut first = f64::INFINITY;
        let mut last = 0.0_f64;
        for id in selected {
            if let Some(clip) = self.clip(&id) {
                first = first.min(clip.start);
                last = last.max(clip.start + clip.duration);
            }
        }
        if first.is_infinite() || last <= first { return; }
        let name = format!("Compound {}", self.timeline().compounds.len() + 1);
        self.apply(Command::CreateCompoundRange {
            start: first, end: last, name,
        });
    }

    /// Edits the *actual* clips in place. No hidden proxy or baked media is
    /// substituted; selection exposes nested clips as one editable set.
    pub(super) fn select_compound_members(&mut self, clicked: &str) {
        let Some(id) = self.compound_for_clip(clicked) else { return; };
        fn collect(studio: &Studio, group_id: &str, seen: &mut HashSet<String>,
                   clips: &mut Vec<String>) {
            if !seen.insert(group_id.to_owned()) { return; }
            if let Some(group) = studio.timeline().compounds.iter().find(|g| g.id == group_id) {
                for member in &group.members {
                    match member {
                        CompoundMember::Clip(id) => {
                            if !clips.contains(id) { clips.push(id.clone()); }
                        }
                        CompoundMember::Compound(id) => collect(studio, id, seen, clips),
                    }
                }
            }
        }
        let mut ids = Vec::new();
        collect(self, &id, &mut HashSet::new(), &mut ids);
        if ids.is_empty() { return; }
        self.flush_commit();
        self.transition_selected = None;
        self.selection = ids;
    }

    pub fn prompt_compound_rename(&mut self, clicked: &str) {
        let Some(id) = self.compound_for_clip(clicked) else { return; };
        let Some(group) = self.timeline().compounds.iter().find(|g| g.id == id) else {
            return;
        };
        self.compound_rename = Some((id, group.name.clone()));
    }

    pub fn compound_rename_edit(&mut self, text: String) {
        if let Some((_, name)) = self.compound_rename.as_mut() {
            *name = text;
        }
    }

    pub fn compound_rename_apply(&mut self) {
        let Some((id, name)) = self.compound_rename.clone() else { return; };
        let name = name.trim();
        if name.is_empty() {
            self.notify("Compound Clip name cannot be blank.", true);
            return;
        }
        self.compound_rename = None;
        self.apply(Command::RenameCompound {
            compound_id: id, name: name.to_owned(),
        });
    }

    pub fn compound_rename_dismiss(&mut self) {
        self.compound_rename = None;
    }

    pub fn ask_delete_compound(&mut self, clicked: &str) {
        let Some(id) = self.compound_for_clip(clicked) else { return; };
        let Some(group) = self.timeline().compounds.iter().find(|g| g.id == id) else {
            return;
        };
        self.confirm = Some(Confirm::RemoveCompound {
            compound_id: id, name: group.name.clone(),
        });
    }

    pub(super) fn break_compound_from(&mut self, clicked: &str) {
        if let Some(compound_id) = self.compound_for_clip(clicked) {
            self.apply(Command::UngroupCompound { compound_id });
        }
    }
}

#[cfg(test)]
mod nested_lookup_tests {
    use super::*;

    fn group(id: &str, members: Vec<CompoundMember>) -> Compound {
        Compound {
            id: id.into(),
            name: id.into(),
            members,
            extra: Default::default(),
        }
    }

    #[test]
    fn nested_clip_actions_target_outermost_compound() {
        let groups = vec![
            group("inner", vec![CompoundMember::Clip("video".into())]),
            group("middle", vec![CompoundMember::Compound("inner".into())]),
            group("outer", vec![
                CompoundMember::Compound("middle".into()),
                CompoundMember::Clip("voice".into()),
            ]),
        ];
        assert_eq!(root_compound_for_clip(&groups, "video").as_deref(), Some("outer"));
        assert_eq!(root_compound_for_clip(&groups, "voice").as_deref(), Some("outer"));
        assert_eq!(root_compound_for_clip(&groups, "missing"), None);
    }

    #[test]
    fn member_view_includes_nested_clips_without_unrelated_media() {
        let groups = vec![
            group("inner", vec![CompoundMember::Clip("a".into())]),
            group("outer", vec![CompoundMember::Compound("inner".into()),
                                CompoundMember::Clip("b".into())]),
            group("unrelated", vec![CompoundMember::Clip("c".into())]),
        ];
        let ids = compound_view_clips(&groups, "outer").unwrap();
        assert_eq!(ids.len(), 2);
        assert!(ids.contains("a") && ids.contains("b"));
        assert!(!ids.contains("c"));
        assert_eq!(compound_view_clips(&groups, "absent"), None);
    }

    #[test]
    fn open_nested_groups_descends_one_level_per_step() {
        let groups = vec![
            group("inner", vec![CompoundMember::Clip("a".into())]),
            group("middle", vec![CompoundMember::Compound("inner".into())]),
            group("outer", vec![CompoundMember::Compound("middle".into())]),
        ];
        let path = compound_ancestry(&groups, "a").unwrap();
        assert_eq!(path, vec!["inner", "middle", "outer"]);
        let index = path.iter().position(|id| id == "outer").unwrap();
        assert_eq!(path[index - 1], "middle");
        let index = path.iter().position(|id| id == "middle").unwrap();
        assert_eq!(path[index - 1], "inner");
    }

    #[test]
    fn independent_compounds_remain_independent() {
        let groups = vec![
            group("one", vec![CompoundMember::Clip("a".into())]),
            group("two", vec![CompoundMember::Clip("b".into())]),
        ];
        assert_eq!(root_compound_for_clip(&groups, "a").as_deref(), Some("one"));
        assert_eq!(root_compound_for_clip(&groups, "b").as_deref(), Some("two"));
    }

    #[test]
    fn cyclic_hand_edited_graph_never_loops_or_mutates_media() {
        let groups = vec![
            group("one", vec![
                CompoundMember::Clip("a".into()),
                CompoundMember::Compound("two".into()),
            ]),
            group("two", vec![CompoundMember::Compound("one".into())]),
        ];
        assert_eq!(root_compound_for_clip(&groups, "a"), None);
        assert_eq!(groups[0].members.len(), 2);
    }
}
