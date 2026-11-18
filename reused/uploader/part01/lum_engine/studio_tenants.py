"""Fail-closed per-account ownership for uploader database entities.

One independent table per Song/Short/Copyright DB. Never infer a user from an
OAuth channel, shared Sheets row, or a legacy unassigned record; these remain
admin-only until explicitly assigned. No worker/queue mutations in this module.
"""
from __future__ import annotations

import os
import re

KINDS = frozenset({"channel", "group", "sheet", "profile"})
USER = re.compile(r"^[a-zA-Z0-9_.@+-]{1,160}$")


class StudioTenants:
    def __init__(self, repository):
        self.db = repository

    @staticmethod
    def username(username):
        username = str(username or "").strip()
        if not USER.fullmatch(username):
            raise ValueError("Verified Studio username is required")
        return username.casefold()

    @staticmethod
    def identity(entity_kind, entity_id):
        if entity_kind not in KINDS or int(entity_id) <= 0:
            raise ValueError("Invalid Studio ownership target")
        return entity_kind, int(entity_id)

    def _initialize(self, connection):
        connection.execute("""
            CREATE TABLE IF NOT EXISTS studio_entity_owners (
                entity_kind TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                owner_username TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(entity_kind, entity_id)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_studio_entity_owner
              ON studio_entity_owners (owner_username, entity_kind)
        """)

    def owner(self, kind, entity_id):
        kind, entity_id = self.identity(kind, entity_id)
        with self.db.connect() as con:
            self._initialize(con)
            row = con.execute(
                "SELECT owner_username FROM studio_entity_owners WHERE entity_kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
            return str(row[0]) if row else ""

    def bind(self, kind, entity_id, username):
        """First ownership claim only; existing owner cannot be replaced here."""
        kind, entity_id = self.identity(kind, entity_id)
        owner = self.username(username)
        with self.db.connect() as con:
            self._initialize(con)
            con.execute(
                "INSERT OR IGNORE INTO studio_entity_owners"
                " (entity_kind, entity_id, owner_username) VALUES (?,?,?)",
                (kind, entity_id, owner),
            )
            actual = con.execute(
                "SELECT owner_username FROM studio_entity_owners WHERE entity_kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
            if not actual or str(actual[0]).casefold() != owner:
                raise PermissionError("Studio resource belongs to a different user")
        return owner

    def require(self, kind, entity_id, user):
        self.identity(kind, entity_id)
        if not user or not str(user.get("username") or "").strip():
            raise PermissionError("Authenticated Studio account is required")
        if int(user.get("is_admin") or 0) and os.getenv("LUM_ENGINE_INSTANCE") not in {"copyright", "concat"}:
            return
        owner = self.owner(kind, entity_id)
        if not owner or owner != self.username(user["username"]):
            raise PermissionError("Studio resource is not assigned to this account")

    def allowed_ids(self, kind, user):
        if kind not in KINDS:
            raise ValueError("Unknown Studio resource type")
        if not user or not str(user.get("username") or "").strip():
            raise PermissionError("Authenticated Studio account is required")
        if int(user.get("is_admin") or 0) and os.getenv("LUM_ENGINE_INSTANCE") not in {"copyright", "concat"}:
            return None  # Non-Copyright admins may inspect legacy records.
        owner = self.username(user["username"])
        with self.db.connect() as con:
            self._initialize(con)
            return {int(row[0]) for row in con.execute(
                "SELECT entity_id FROM studio_entity_owners WHERE entity_kind=? AND owner_username=?",
                (kind, owner),
            ).fetchall()}

    def create_channel(self, username, channel_id, channel_name, email=""):
        """Atomically create a NEW UC channel, without upserting legacy OAuth."""
        owner=self.username(username)
        channel_id=str(channel_id or "").strip()
        channel_name=str(channel_name or "").strip()
        email=str(email or "").strip()
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",channel_id):
            raise ValueError("Enter the exact 24-character YouTube UC Channel ID")
        if not (1 <= len(channel_name) <= 120) or any(ord(c)<32 for c in channel_name):
            raise ValueError("Channel name must be 1–120 printable characters")
        if len(email)>160 or any(c in email for c in "\r\n"):
            raise ValueError("Invalid account email")
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            # An existing channel ID is globally unique. Never overwrite it,
            # its token path, historical queue, or another tenant's credentials.
            if con.execute("SELECT 1 FROM channels WHERE channel_id=?",
                           (channel_id,)).fetchone():
                raise PermissionError("This YouTube channel is already registered")
            cur=con.execute("""
                INSERT INTO channels(channel_id,channel_name,google_account_email,
                                     token_file_path,channel_url,status)
                VALUES (?,?,?,'',?,'ACTIVE')
                """,(channel_id,channel_name,email or None,
                     "https://www.youtube.com/channel/"+channel_id))
            cid=int(cur.lastrowid)
            con.execute("""INSERT INTO studio_entity_owners
                (entity_kind,entity_id,owner_username) VALUES ('channel',?,?)""",
                (cid,owner))
            return cid

    def create_group(self, username, group_name, channel_ids):
        """New group with ONLY owner's channels, in one SQLite transaction."""
        owner=self.username(username)
        name=str(group_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{1,79}",name):
            raise ValueError("Group name must be 2–80 safe characters")
        ids=sorted({int(value) for value in channel_ids})
        if not ids or len(ids)>500 or any(cid<=0 for cid in ids):
            raise ValueError("Select 1–500 owned channels")
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM channel_groups WHERE group_name=? COLLATE NOCASE",
                           (name,)).fetchone():
                raise PermissionError("This channel-group name is already in use")
            marks=",".join("?" for _ in ids)
            owned=con.execute(
                "SELECT c.id FROM channels c JOIN studio_entity_owners o "
                "ON o.entity_kind='channel' AND o.entity_id=c.id "
                "WHERE o.owner_username=? AND c.id IN ("+marks+")",
                (owner,*ids)).fetchall()
            if len(owned)!=len(ids):
                raise PermissionError("Group contains a channel not owned by this user")
            cur=con.execute("INSERT INTO channel_groups(group_name,notes) VALUES (?,?)",
                            (name,"Created by authorized Studio account"))
            gid=int(cur.lastrowid)
            con.executemany(
                "INSERT INTO channel_group_members(group_id,channel_db_id) VALUES (?,?)",
                [(gid,cid) for cid in ids])
            con.execute("""INSERT INTO studio_entity_owners
                (entity_kind,entity_id,owner_username) VALUES ('group',?,?)""",
                (gid,owner))
            return gid

    def sync_owned_sheet_rows(self, username, sheet_id, rows, *, sync_sheet_groups=False):
        """Atomically reconcile only this account's Sheet channels and, for
        Copyright, the groups listed in the same rows. Never modify other
        owners' memberships, OAuth, token files, queues or manual groups.
        """
        owner=self.username(username)
        source_id=int(sheet_id)
        if source_id<=0 or len(rows)>2000:
            raise ValueError("Invalid Sheet source or row limit")
        prepared=[]
        seen_keys=set()
        for raw in rows:
            key=str(getattr(raw,"key","") or "").strip().lower()
            name=str(getattr(raw,"channel_name","") or "").strip()
            channel_id=str(getattr(raw,"channel_id","") or "").strip()
            email=str(getattr(raw,"gmail","") or "").strip()
            if not key or len(key)>180 or not name or len(name)>180:
                raise ValueError("Sheet row has invalid channel key/name")
            if key in seen_keys:
                raise ValueError("Duplicate Sheet row key")
            seen_keys.add(key)
            if channel_id.lower().startswith("sheet:"):
                channel_id=f"sheet:{source_id}:{key}"
            elif not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",channel_id):
                raise ValueError("Sheet channel ID must be a verified UC ID or a provisional row")
            notes=str(getattr(raw,"notes","") or "").strip()[:500]
            group_names=()
            if sync_sheet_groups:
                parts=[re.sub(r"\s+"," ",part).strip() for part in
                       re.split(r"[,;|]+",str(getattr(raw,"group","") or ""))]
                group_names=tuple(dict.fromkeys(
                    part for part in parts if part and
                    part.casefold() not in {"none","no","na","n/a","-"}))
                if any(len(part)>70 or any(ord(ch)<32 for ch in part)
                       for part in group_names):
                    raise ValueError("Sheet group name must be 1-70 printable characters")
            prepared.append((key,channel_id,name,email,notes,group_names))
        if not prepared:
            raise ValueError("Google Sheet returned zero usable rows; no changes applied")

        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            source=con.execute(
                "SELECT id FROM sheet_sources WHERE id=?",(source_id,)).fetchone()
            assigned=con.execute("""SELECT owner_username FROM studio_entity_owners
                 WHERE entity_kind='sheet' AND entity_id=?""",(source_id,)).fetchone()
            if not source or not assigned or str(assigned[0]).casefold()!=owner:
                raise PermissionError("Sheet source is not owned by this account")
            updates=0
            sheet_group_targets=[]
            for key,ucid,name,email,notes,group_names in prepared:
                by_row=con.execute(
                    "SELECT id,channel_id FROM channels WHERE sheet_source_id=? AND sheet_row_key=?",
                    (source_id,key)).fetchone()
                by_id=con.execute(
                    "SELECT id FROM channels WHERE channel_id=?",
                    (ucid,)).fetchone()
                if by_id and (not by_row or int(by_row[0])!=int(by_id[0])):
                    # The user may attach their OWN previously registered manual
                    # UC channel. Keep its same DB row and OAuth token file.
                    existing=con.execute(
                        "SELECT id,channel_id,sheet_source_id FROM channels WHERE id=?",
                        (int(by_id[0]),)).fetchone()
                    linked=con.execute("""SELECT owner_username FROM studio_entity_owners
                        WHERE entity_kind='channel' AND entity_id=?""",
                        (int(by_id[0]),)).fetchone()
                    if (by_row or not existing or existing[2] is not None or
                        not linked or str(linked[0]).casefold()!=owner):
                        raise PermissionError("Sheet channel ID exists outside this owned Sheet row")
                    by_row=(int(existing[0]),str(existing[1]))
                if by_row:
                    cid=int(by_row[0])
                    assigned_channel=con.execute("""SELECT owner_username
                        FROM studio_entity_owners WHERE entity_kind='channel' AND entity_id=?""",
                        (cid,)).fetchone()
                    if not assigned_channel or str(assigned_channel[0]).casefold()!=owner:
                        raise PermissionError("Existing Sheet channel is not owned by this account")
                    previous=str(by_row[1] or "")
                    if previous.startswith("UC") and previous!=ucid:
                        raise PermissionError("Existing authorized UC Channel ID cannot change via Sheet")
                    con.execute("""UPDATE channels SET channel_id=?,channel_name=?,
                        google_account_email=?,sheet_notes=?,sheet_source_id=?,sheet_row_key=?,
                        sheet_present=1,sheet_last_seen_at=CURRENT_TIMESTAMP,
                        last_used_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (ucid,name,email,notes,source_id,key,cid))
                else:
                    cur=con.execute("""INSERT INTO channels(
                        channel_id,channel_name,google_account_email,token_file_path,
                        status,sheet_source_id,sheet_row_key,sheet_present,sheet_notes,
                        sheet_last_seen_at)
                        VALUES (?,?,?,'','ACTIVE',?,?,1,?,CURRENT_TIMESTAMP)""",
                        (ucid,name,email,source_id,key,notes))
                    cid=int(cur.lastrowid)
                    con.execute("""INSERT INTO studio_entity_owners
                        (entity_kind,entity_id,owner_username) VALUES ('channel',?,?)""",
                        (cid,owner))
                updates+=1
                if sync_sheet_groups:
                    sheet_group_targets.extend((cid,name) for name in group_names)
            keys=[entry[0] for entry in prepared]
            placeholders=",".join("?" for _ in keys)
            # No queue/history deletion or token rotation.
            removed=con.execute("""UPDATE channels SET sheet_present=0
                WHERE sheet_source_id=? AND COALESCE(sheet_row_key,'') NOT IN ("""+
                placeholders+""") AND COALESCE(sheet_present,1)<>0""",
                (source_id,*keys)).rowcount
            groups_touched=0
            memberships_updated=0
            if sync_sheet_groups and sheet_group_targets:
                # Track ONLY memberships created by this Sheet, so a later sync
                # cannot delete manually assigned or another Sheet's groups.
                con.execute("""CREATE TABLE IF NOT EXISTS studio_sheet_group_links (
                    sheet_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    channel_db_id INTEGER NOT NULL,
                    PRIMARY KEY(sheet_id,group_id,channel_db_id)
                )""")
                from desktop_core.modules.google_sheet_sync import smart_group_name
                known_owned=[str(r[0]) for r in con.execute("""
                    SELECT g.group_name FROM channel_groups g
                    JOIN studio_entity_owners o ON o.entity_kind='group'
                      AND o.entity_id=g.id AND o.owner_username=? COLLATE NOCASE
                    ORDER BY g.id""",(owner,))]
                cache={}
                desired=set()
                for channel_db_id,raw_name in sheet_group_targets:
                    canonical,_=smart_group_name(raw_name,known_owned)
                    if not canonical:
                        continue
                    label=canonical
                    if len(label)>80:
                        raise ValueError("Sheet group name exceeds 80 characters")
                    for _attempt in range(2):
                        existing=con.execute("""SELECT g.id,o.owner_username FROM channel_groups g
                            LEFT JOIN studio_entity_owners o ON
                              o.entity_kind='group' AND o.entity_id=g.id
                            WHERE g.group_name=? COLLATE NOCASE""",(label,)).fetchone()
                        if not existing or (existing[1] and str(existing[1]).casefold()==owner):
                            break
                        # Group names are globally unique in legacy SQLite.
                        # A same-named foreign/legacy group must never be claimed.
                        suffix=" ("+owner[:18]+")"
                        label=canonical[:80-len(suffix)]+suffix
                    else:
                        raise PermissionError("Sheet group name conflicts with a foreign group")
                    key_name=label.casefold()
                    if key_name not in cache:
                        if existing:
                            group_id=int(existing[0])
                        else:
                            cur=con.execute("""INSERT INTO channel_groups(group_name,notes)
                                VALUES (?,?)""",(label,"Created from owned Copyright Sheet sync"))
                            group_id=int(cur.lastrowid)
                            con.execute("""INSERT INTO studio_entity_owners
                                (entity_kind,entity_id,owner_username) VALUES ('group',?,?)""",
                                (group_id,owner))
                        cache[key_name]=group_id
                        known_owned.append(label)
                    desired.add((cache[key_name],int(channel_db_id)))
                previous={(int(r[0]),int(r[1])) for r in con.execute("""
                    SELECT group_id,channel_db_id FROM studio_sheet_group_links
                    WHERE sheet_id=?""",(source_id,))}
                for gid,cid in previous-desired:
                    used_elsewhere=con.execute("""SELECT 1 FROM studio_sheet_group_links
                        WHERE sheet_id<>? AND group_id=? AND channel_db_id=?""",
                        (source_id,gid,cid)).fetchone()
                    if not used_elsewhere:
                        con.execute("""DELETE FROM channel_group_members
                            WHERE group_id=? AND channel_db_id=?""",(gid,cid))
                        memberships_updated+=1
                tracked=previous & desired
                for gid,cid in desired:
                    cur=con.execute("""INSERT OR IGNORE INTO channel_group_members
                        (group_id,channel_db_id) VALUES (?,?)""",(gid,cid))
                    if cur.rowcount:
                        tracked.add((gid,cid))
                        memberships_updated+=1
                con.execute("DELETE FROM studio_sheet_group_links WHERE sheet_id=?",(source_id,))
                con.executemany("""INSERT INTO studio_sheet_group_links
                    (sheet_id,group_id,channel_db_id) VALUES (?,?,?)""",
                    [(source_id,gid,cid) for gid,cid in sorted(tracked)])
                groups_touched=len({gid for gid,_ in desired})
            con.execute("""UPDATE sheet_sources SET last_sync_at=CURRENT_TIMESTAMP,
                last_sync_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (f"OWNED_SYNC: rows={updates} hidden={removed} groups={groups_touched}",source_id))
            return {"ok":True,"rows":updates,"updated":updates,"hidden_removed":removed,
                    "groups_touched":groups_touched,
                    "memberships_updated":memberships_updated,"source_id":source_id}

    def delete_owned_sheet(self, username, sheet_id):
        """Detach only owned channels; do not delete media, groups or tokens."""
        owner=self.username(username)
        source_id=int(sheet_id)
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            assigned=con.execute("""SELECT owner_username FROM studio_entity_owners
                WHERE entity_kind='sheet' AND entity_id=?""",(source_id,)).fetchone()
            if not assigned or str(assigned[0]).casefold()!=owner:
                raise PermissionError("Sheet source belongs to a different user")
            channels=[int(row[0]) for row in con.execute(
                "SELECT id FROM channels WHERE sheet_source_id=?",(source_id,))]
            for cid in channels:
                linked=con.execute("""SELECT owner_username FROM studio_entity_owners
                    WHERE entity_kind='channel' AND entity_id=?""",(cid,)).fetchone()
                if not linked or str(linked[0]).casefold()!=owner:
                    raise PermissionError("Sheet contains an unowned or foreign channel")
            con.execute("""UPDATE channels SET sheet_present=0,sheet_source_id=NULL,
                sheet_row_key=NULL WHERE sheet_source_id=?""",(source_id,))
            con.execute("DELETE FROM sheet_sources WHERE id=?",(source_id,))
            con.execute("""DELETE FROM studio_entity_owners
                WHERE entity_kind='sheet' AND entity_id=?""",(source_id,))
            return len(channels)

    def assign_by_admin(self, kind, entity_id, owner, user):
        """First assignment of a legacy entity; never silently transfer history."""
        if not user or not int(user.get("is_admin") or 0):
            raise PermissionError("Administrator required")
        return self.bind(kind, entity_id, owner)
