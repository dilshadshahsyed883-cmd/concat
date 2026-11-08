"""Fail-closed native compound membership validation for both CUDA modes.

Compounds are currently *structural*, not render layers. The native renderer
still processes the original timeline clips exactly once. Group-level visual
effects, transforms and time-remapping are intentionally rejected until the
native preview and CUDA worker have a shared nested compositor. This also
guards against an older worker silently ignoring future project features.
"""
from __future__ import annotations

def validate_compounds(timeline: dict) -> None:
    if not isinstance(timeline, dict):
        raise ValueError("Invalid compound timeline")
    groups = timeline.get("compounds", [])
    if not isinstance(groups, list):
        raise ValueError("Compound groups must be a list")
    if len(groups) > 10000:
        raise ValueError("Too many compound groups")
    if not groups:
        return
    clips = timeline.get("clips")
    if not isinstance(clips, list):
        raise ValueError("Compound timeline has no clip list")
    clip_ids = {c["id"] for c in clips if isinstance(c, dict) and
                isinstance(c.get("id"), str)}
    groups_by_id: dict[str, list] = {}
    total = 0
    for group in groups:
        if not isinstance(group, dict) or set(group) - {"id", "name", "members"}:
            raise ValueError("Unsupported compound-level effect, transform or field")
        ident = group.get("id")
        name = group.get("name")
        members = group.get("members")
        if not isinstance(ident, str) or not ident or ident in groups_by_id:
            raise ValueError("Invalid or duplicate compound ID")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Compound requires a name")
        if not isinstance(members, list) or not members:
            raise ValueError("Compound requires at least one member")
        total += len(members)
        if total > 200000:
            raise ValueError("Compound membership budget exceeded")
        groups_by_id[ident] = members
    owned_clips, owned_groups = set(), set()
    for ident, members in groups_by_id.items():
        for member in members:
            if not isinstance(member, dict) or set(member) != {"kind", "id"}:
                raise ValueError("Invalid compound member")
            kind = member.get("kind")
            target = member.get("id")
            if not isinstance(target, str):
                raise ValueError("Invalid compound member ID")
            if kind == "clip":
                if target not in clip_ids:
                    raise ValueError(f"Compound {ident} references missing clip {target}")
                if target in owned_clips:
                    raise ValueError(f"Clip {target} belongs to multiple compounds")
                owned_clips.add(target)
            elif kind == "compound":
                if target not in groups_by_id:
                    raise ValueError(f"Compound {ident} references missing compound {target}")
                if target in owned_groups:
                    raise ValueError(f"Nested compound {target} has multiple parents")
                owned_groups.add(target)
            else:
                raise ValueError(f"Unknown compound member kind {kind}")
    # Explicit DFS avoids both recursive Python stack overflow and
    # group cycles, including malicious self-refs in uploaded manifests.
    done = set()
    for root in groups_by_id:
        if root in done:
            continue
        active = set()
        stack = [(root, False)]
        while stack:
            ident, finished = stack.pop()
            if finished:
                active.remove(ident)
                done.add(ident)
                continue
            if ident in done:
                continue
            if ident in active:
                raise ValueError("Compound nesting contains a cycle")
            active.add(ident)
            stack.append((ident, True))
            for member in reversed(groups_by_id[ident]):
                if member["kind"] == "compound":
                    child = member["id"]
                    if child in active:
                        raise ValueError("Compound nesting contains a cycle")
                    if child not in done:
                        stack.append((child, False))
