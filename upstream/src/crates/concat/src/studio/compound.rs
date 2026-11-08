// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors

//! Desktop commands for reversible structural compounds.
//! Clips remain real clips on their original tracks; no proxy with duplicate
//! audio or unimplemented parent effects is ever sent to Preview/CUDA.

use super::{Command, Studio};
use concat_project::model::CompoundMember;
use std::collections::HashSet;

impl Studio {
    pub(super) fn compound_for_clip(&self, clip_id: &str) -> Option<String> {
        self.timeline().compounds.iter().find(|g| g.members.iter().any(|m| {
            matches!(m, CompoundMember::Clip(id) if id == clip_id)
        })).map(|g| g.id.clone())
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

    pub(super) fn break_compound_from(&mut self, clicked: &str) {
        if let Some(compound_id) = self.compound_for_clip(clicked) {
            self.apply(Command::UngroupCompound { compound_id });
        }
    }
}
