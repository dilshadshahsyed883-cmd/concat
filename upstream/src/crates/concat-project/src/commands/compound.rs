// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors

//! Reversible compound membership. Clips are not baked, copied, hidden or
//! retimed; the existing renderers therefore see each original visual and
//! audio source exactly once. These commands intentionally do not offer
//! parent-level effects until both Desktop and CUDA implement true nested
//! compositing: silently applying an effect to each child changes the image.

use std::collections::{HashMap, HashSet};
use super::*;
use crate::model::{Compound, CompoundMember};

fn invalid(reason: &str) -> CommandError {
    CommandError::InvalidCompound { reason: reason.to_owned() }
}

fn owned_members(timeline: &Timeline) -> (HashSet<String>, HashSet<String>) {
    let mut clips = HashSet::new();
    let mut groups = HashSet::new();
    for group in &timeline.compounds {
        for member in &group.members {
            match member {
                CompoundMember::Clip(id) => { clips.insert(id.clone()); }
                CompoundMember::Compound(id) => { groups.insert(id.clone()); }
            }
        }
    }
    (clips, groups)
}

fn bounds(
    timeline: &Timeline,
    group_id: &str,
    stack: &mut HashSet<String>,
) -> Result<(f64, f64), CommandError> {
    if !stack.insert(group_id.to_owned()) {
        return Err(invalid("Compound nesting contains a cycle."));
    }
    let group = timeline.compounds.iter().find(|g| g.id == group_id)
        .ok_or_else(|| invalid("Compound reference not found."))?;
    let mut first = f64::INFINITY;
    let mut last = 0.0_f64;
    for member in &group.members {
        let (a,b) = match member {
            CompoundMember::Clip(id) => {
                let c = timeline.clips.iter().find(|c| &c.id == id)
                    .ok_or_else(|| invalid("Compound references a deleted clip."))?;
                (c.start, c.start + c.duration)
            },
            CompoundMember::Compound(id) => bounds(timeline, id, stack)?,
        };
        first = first.min(a);
        last = last.max(b);
    }
    stack.remove(group_id);
    if first.is_infinite() { return Err(invalid("Cannot group an empty compound.")); }
    Ok((first, last))
}

fn create(
    project: &mut Project, mint: &mut IdMint,
    clips: Vec<String>, groups: Vec<String>, name: String,
) -> Result<Outcome, CommandError> {
    let timeline = project.active_mut();
    let name = name.trim();
    if name.is_empty() { return Err(invalid("Give the compound a non-empty name.")); }
    let (owned_clips, owned_groups) = owned_members(timeline);
    let (mut unique_clips, mut unique_groups) = (HashSet::new(), HashSet::new());
    let valid_clips: HashSet<&str> = timeline.clips.iter().map(|c| c.id.as_str()).collect();
    let valid_groups: HashSet<&str> = timeline.compounds.iter().map(|g| g.id.as_str()).collect();
    for id in &clips {
        if !unique_clips.insert(id.as_str()) || !valid_clips.contains(id.as_str()) {
            return Err(invalid("Compound has a duplicate or unknown clip."));
        }
        if owned_clips.contains(id) { return Err(invalid("Clip already belongs to a compound. Select its parent compound instead.")); }
    }
    for id in &groups {
        if !unique_groups.insert(id.as_str()) || !valid_groups.contains(id.as_str()) {
            return Err(invalid("Compound has a duplicate or unknown nested compound."));
        }
        if owned_groups.contains(id) { return Err(invalid("Nested compound already has a parent.")); }
        bounds(timeline, id, &mut HashSet::new())?;
    }
    if clips.is_empty() && groups.is_empty() {
        return Err(invalid("Choose at least one clip or compound."));
    }
    let members = clips.into_iter().map(CompoundMember::Clip)
        .chain(groups.into_iter().map(CompoundMember::Compound))
        .collect();
    let id = mint.next("cmp");
    timeline.compounds.push(Compound {
        id: id.clone(), name: name.to_owned(), members, extra: Default::default(),
    });
    Ok(Outcome { created_id: Some(id), applied: true })
}

/// Clean dangling references after destructive clip or track edits and when
/// reading a damaged document. A malformed cycle is dropped rather than
/// allowing unbounded recursion. Existing picture/sound clips are untouched.
pub(crate) fn prune_compounds(timeline: &mut Timeline) {
    let clip_ids: HashSet<String> = timeline.clips.iter().map(|c| c.id.clone()).collect();
    let mut ids = HashSet::new();
    timeline.compounds.retain(|g| !g.id.is_empty() && ids.insert(g.id.clone()));
    // Iterate to a fixed point: deleting a leaf may empty its parent.
    loop {
        let valid: HashSet<String> = timeline.compounds.iter().map(|g| g.id.clone()).collect();
        let mut owned_clip = HashSet::new();
        let mut owned_group = HashSet::new();
        let before = timeline.compounds.clone();
        for group in &mut timeline.compounds {
            group.members.retain(|member| match member {
                CompoundMember::Clip(id) => clip_ids.contains(id) && owned_clip.insert(id.clone()),
                CompoundMember::Compound(id) => id != &group.id && valid.contains(id)
                    && owned_group.insert(id.clone()),
            });
        }
        timeline.compounds.retain(|group| !group.members.is_empty());
        if timeline.compounds == before { break; }
    }
    // Reject cycles in hand-edited documents without dropping any media.
    let snapshot = timeline.compounds.clone();
    let edges: HashMap<String, Vec<String>> = snapshot.iter()
        .map(|g| (g.id.clone(), g.members.iter().filter_map(|m| match m {
            CompoundMember::Compound(id) => Some(id.clone()), _ => None
        }).collect())).collect();
    fn cyclic(id: &str, edges: &HashMap<String, Vec<String>>, path: &mut HashSet<String>) -> bool {
        if !path.insert(id.to_owned()) { return true; }
        let hit = edges.get(id).is_some_and(|next| next.iter().any(|x| cyclic(x, edges, path)));
        path.remove(id);
        hit
    }
    if edges.keys().any(|id| cyclic(id, &edges, &mut HashSet::new())) {
        timeline.compounds.clear(); // fail-safe: original clips still render.
    }
}

pub(super) fn apply(
    project: &mut Project, mint: &mut IdMint, command: Command,
) -> Result<Outcome, CommandError> {
    match command {
        Command::CreateCompound { clip_ids, compound_ids, name } =>
            create(project, mint, clip_ids, compound_ids, name),
        Command::CreateCompoundAll { name } => {
            let t = project.active();
            let (owned_clips, owned_groups) = owned_members(t);
            let clips = t.clips.iter().filter(|c| !owned_clips.contains(&c.id))
                .map(|c| c.id.clone()).collect();
            let groups = t.compounds.iter().filter(|g| !owned_groups.contains(&g.id))
                .map(|g| g.id.clone()).collect();
            create(project, mint, clips, groups, name)
        },
        Command::CreateCompoundRange { start, end, name } => {
            if start < 0.0 || end <= start {
                return Err(invalid("Compound time range must have a positive duration."));
            }
            let t = project.active();
            let (owned_clips, owned_groups) = owned_members(t);
            let mut clips = Vec::new();
            let mut groups = Vec::new();
            for c in &t.clips {
                if owned_clips.contains(&c.id) { continue; }
                let a = c.start;
                let b = a + c.duration;
                if a < end && b > start {
                    if a < start || b > end { return Err(invalid("Split clips at the range boundaries before compounding.")); }
                    clips.push(c.id.clone());
                }
            }
            for g in &t.compounds {
                if owned_groups.contains(&g.id) { continue; }
                let (a,b) = bounds(t, &g.id, &mut HashSet::new())?;
                if a < end && b > start {
                    if a < start || b > end { return Err(invalid("The time range cuts through an existing compound.")); }
                    groups.push(g.id.clone());
                }
            }
            create(project, mint, clips, groups, name)
        },
        Command::UngroupCompound { compound_id } => {
            let t = project.active_mut();
            let Some(index) = t.compounds.iter().position(|g| g.id == compound_id) else {
                return Ok(Outcome::default());
            };
            let members = t.compounds[index].members.clone();
            t.compounds.remove(index);
            for parent in &mut t.compounds {
                if let Some(index) = parent.members.iter().position(|m| m ==
                    &CompoundMember::Compound(compound_id.clone())) {
                    parent.members.splice(index..=index, members.iter().cloned());
                }
            }
            Ok(Outcome { applied: true, ..Outcome::default() })
        },
        Command::RenameCompound { compound_id, name } => {
            let name = name.trim();
            if name.is_empty() { return Err(invalid("Give the compound a non-empty name.")); }
            let group = project.active_mut().compounds.iter_mut().find(|g| g.id == compound_id);
            let Some(group) = group else { return Ok(Outcome::default()); };
            Ok(Outcome { applied: assign(&mut group.name, name.to_owned()), ..Outcome::default() })
        },
        _ => unreachable!("compound command routing"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doc::{DocumentSettings, from_document, to_document};
    use crate::editor::Editor;
    use crate::model::{Clip, ClipKind};

    fn editor() -> Editor {
        let mut doc = Project::new();
        let t = Arc::make_mut(&mut doc.timelines[0]);
        t.clips.push(Arc::new(Clip::blank("c1", "T1", ClipKind::Text, "A", 0.0, 2.0)));
        t.clips.push(Arc::new(Clip::blank("c2", "T1", ClipKind::Text, "B", 4.0, 1.0)));
        t.clips.push(Arc::new(Clip::blank("c3", "T2", ClipKind::Text, "C", 0.0, 5.0)));
        let settings = DocumentSettings { name: "Group test".into(), width: 1920,
            height: 1080, rate_num: 30, rate_den: 1 };
        Editor::from_document(&to_document(&settings, &doc)).unwrap()
    }
    #[test]
    fn compound_is_nondestructive_and_survives_undo_redo_save_reload() {
        let mut e = editor();
        let original = e.project().active().clips.clone();
        let created = e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into(), "c2".into()], compound_ids: vec![],
            name: " Scene ".into(),
        }).unwrap().created_id.unwrap();
        assert_eq!(e.project().active().clips, original);
        assert_eq!(e.project().active().compounds[0].name, "Scene");
        assert!(e.undo());
        assert!(e.project().active().compounds.is_empty());
        assert!(e.redo());
        assert_eq!(e.project().active().compounds[0].id, created);
        let settings = DocumentSettings { name: "Group test".into(), width: 1920,
            height: 1080, rate_num: 30, rate_den: 1 };
        let document = to_document(&settings, e.project());
        let reopened = from_document(&document).unwrap();
        assert_eq!(reopened.active().clips, original);
        assert_eq!(reopened.active().compounds, e.project().active().compounds);
    }
    #[test]
    fn nested_compounds_do_not_duplicate_members_and_ungroup_in_place() {
        let mut e = editor();
        let first = e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into()], compound_ids: vec![], name: "Inner".into(),
        }).unwrap().created_id.unwrap();
        let second = e.apply(Command::CreateCompound {
            clip_ids: vec!["c2".into()], compound_ids: vec![first.clone()], name: "Outer".into(),
        }).unwrap().created_id.unwrap();
        assert!(e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into()], compound_ids: vec![], name: "Illegal".into(),
        }).is_err());
        assert!(e.apply(Command::CreateCompound {
            clip_ids: vec![], compound_ids: vec![first.clone()], name: "Illegal".into(),
        }).is_err());
        e.apply(Command::UngroupCompound { compound_id: first }).unwrap();
        let outer = e.project().active().compounds.iter().find(|g| g.id == second).unwrap();
        assert_eq!(outer.members, vec![CompoundMember::Clip("c2".into()),
                                       CompoundMember::Clip("c1".into())]);
        assert_eq!(e.project().active().clips.len(), 3);
    }
    #[test]
    fn range_refuses_partial_clips_and_preserves_gaps() {
        let mut e = editor();
        assert!(e.apply(Command::CreateCompoundRange {
            start: 0.5, end: 5.0, name: "Partial".into(),
        }).is_err());
        e.apply(Command::CreateCompoundRange {
            start: 0.0, end: 5.0, name: "Whole".into(),
        }).unwrap();
        assert_eq!(e.project().active().clips[1].start, 4.0);
        assert_eq!(e.project().active().compounds[0].members.len(), 3);
    }
    #[test]
    fn deleting_a_group_member_prunes_only_its_reference_and_undo_restores_it() {
        let mut e = editor();
        e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into(), "c2".into()],
            compound_ids: vec![], name: "Scene".into(),
        }).unwrap();
        e.apply(Command::RemoveClips {
            clip_ids: vec!["c1".into()], ripple: false,
        }).unwrap();
        assert_eq!(e.project().active().compounds[0].members,
            vec![CompoundMember::Clip("c2".into())]);
        assert_eq!(e.project().active().clips.len(), 2);
        assert!(e.undo());
        assert_eq!(e.project().active().clips.len(), 3);
        assert_eq!(e.project().active().compounds[0].members.len(), 2);
    }

    #[test]
    fn curved_source_time_cannot_be_silently_flattened_inside_compound() {
        let mut e = editor();
        e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into()], compound_ids: vec![], name: "Curve".into(),
        }).unwrap();
        let timeline = e.project_mut().active_mut();
        Arc::make_mut(&mut timeline.clips[0]).speed_curve =
            Some(vec![crate::model::SpeedPoint { at: 0.0, speed: 1.5 }]);
        let before = e.project().active().clips.clone();
        assert!(e.apply(Command::SplitClips {
            clip_ids: vec!["c1".into()], time: 1.0,
        }).is_err());
        assert_eq!(e.project().active().clips, before);
        assert_eq!(e.project().active().compounds[0].members.len(), 1);
    }

    #[test]
    fn splitting_inside_a_compound_keeps_both_source_pieces_grouped() {
        let mut e = editor();
        e.apply(Command::CreateCompound {
            clip_ids: vec!["c1".into()], compound_ids: vec![], name: "Scene".into(),
        }).unwrap();
        e.apply(Command::SplitClips {
            clip_ids: vec!["c1".into()], time: 1.0,
        }).unwrap();
        let timeline = e.project().active();
        assert_eq!(timeline.clips.len(), 4);
        let members = &timeline.compounds[0].members;
        assert_eq!(members.len(), 2);
        assert_eq!(members[0], CompoundMember::Clip("c1".into()));
        let CompoundMember::Clip(tail_id) = &members[1] else { panic!("expected tail clip") };
        let tail = timeline.clips.iter().find(|c| &c.id == tail_id).unwrap();
        assert_eq!(tail.start, 1.0);
        assert_eq!(tail.duration, 1.0);
        assert!(e.undo());
        assert_eq!(e.project().active().clips.len(), 3);
        assert_eq!(e.project().active().compounds[0].members.len(), 1);
    }

    #[test]
    fn rename_and_break_apart_keep_source_tracks_and_timing() {
        let mut e = editor();
        let clips = e.project().active().clips.clone();
        let id = e.apply(Command::CreateCompoundAll {
            name: "Whole Timeline".into(),
        }).unwrap().created_id.unwrap();
        e.apply(Command::RenameCompound {
            compound_id: id.clone(), name: "New name".into(),
        }).unwrap();
        assert_eq!(e.project().active().compounds[0].name, "New name");
        e.apply(Command::UngroupCompound { compound_id: id }).unwrap();
        assert!(e.project().active().compounds.is_empty());
        assert_eq!(e.project().active().clips, clips);
        assert!(e.undo());
        assert_eq!(e.project().active().compounds[0].name, "New name");
    }

    #[test]
    fn malformed_cycles_are_dropped_without_deleting_clips() {
        let mut e = editor();
        let mut doc = e.project().clone();
        let t = doc.active_mut();
        t.compounds.push(Compound { id: "cmp10".into(), name: "A".into(),
            members: vec![CompoundMember::Compound("cmp11".into())], extra: Default::default() });
        t.compounds.push(Compound { id: "cmp11".into(), name: "B".into(),
            members: vec![CompoundMember::Compound("cmp10".into())], extra: Default::default() });
        prune_compounds(t);
        assert!(t.compounds.is_empty());
        assert_eq!(t.clips.len(), 3);
        e.apply(Command::CreateCompoundAll { name: "All".into() }).unwrap();
        assert_eq!(e.project().active().compounds[0].members.len(), 3);
    }
}
