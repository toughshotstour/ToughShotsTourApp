#!/usr/bin/env python3
"""Reusable local demographic/bowler database for Tough Shots."""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DIVISIONS = ["U12 Mixed", "U14 Boys", "U14 Girls", "U16 Boys", "U16 Girls", "U18 Boys", "U18 Girls"]
JG_STATES = {"", "JG", "Q"}


def _pick_header(headers, exact=(), contains=()):
    lookup = {str(h).casefold().strip(): h for h in headers}
    for name in exact:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    for h in headers:
        key = str(h).casefold()
        if all(term.casefold() in key for term in contains):
            return h
    return None


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _person_identity(first, last, birth):
    return f"person:{_norm(first)}|{_norm(last)}|{_norm(birth)}"


def proper_name(value):
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    # Preserve intentional mixed-case names; normalize all-upper/all-lower input.
    if text.isupper() or text.islower():
        return text.title()
    return text


def normalize_gender(value):
    raw = str(value or "").strip()
    key = raw.casefold()
    if key in {"boy", "boys", "male", "m"}:
        return "Boy"
    if key in {"girl", "girls", "female", "f"}:
        return "Girl"
    return raw


def normalize_birthdate(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Birthdate is required.")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(raw, fmt).date()
            if dt.year < 100:
                dt = dt.replace(year=dt.year + 2000)
            return dt.isoformat()
        except ValueError:
            pass
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s*", raw)
    if m:
        month, day, year = map(int, m.groups())
        if year < 100:
            year += 2000
        return datetime(year, month, day).date().isoformat()
    raise ValueError(f"Invalid birthdate: {raw!r}. Use M/D/YYYY or YYYY-MM-DD.")


def derived_division(birthdate, gender):
    try:
        dob = datetime.strptime(normalize_birthdate(birthdate), "%Y-%m-%d").date()
    except Exception:
        return ""
    if dob >= datetime(2014, 8, 1).date():
        return "U12 Mixed"
    if datetime(2012, 8, 1).date() <= dob <= datetime(2014, 7, 31).date():
        age = "U14"
    elif datetime(2010, 8, 1).date() <= dob <= datetime(2012, 7, 31).date():
        age = "U16"
    elif datetime(2008, 8, 1).date() <= dob <= datetime(2010, 7, 31).date():
        age = "U18"
    else:
        return ""
    g = normalize_gender(gender)
    return f"{age} Boys" if g == "Boy" else (f"{age} Girls" if g == "Girl" else "")


def database_path(workspace):
    return Path(workspace).expanduser() / "local_demographics.sqlite3"


def snapshot_path(workspace):
    return Path(workspace).expanduser() / "demographic_master.csv"


def _connect(workspace):
    path = database_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demographics (
            identity_key TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            birthdate TEXT NOT NULL,
            gender TEXT,
            usbc_id TEXT,
            email TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(demographics)")}
    for name, ddl in (
        ("division_override", "TEXT NOT NULL DEFAULT ''"),
        ("bowler_id", "TEXT NOT NULL DEFAULT ''"),
        ("jr_gold_status", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE demographics ADD COLUMN {name} {ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_demographics_bowler_id ON demographics(bowler_id) WHERE bowler_id<>''")
    conn.commit()
    _backfill_bowler_ids(conn)
    return conn


def _allocate_bowler_id(conn, raw_usbc, exclude_identity=None):
    digits = _digits(raw_usbc)
    if not digits:
        raise ValueError("USBC ID must contain at least one digit.")
    if len(digits) > 10:
        raise ValueError("USBC ID cannot contain more than 10 digits.")
    base = digits.ljust(10, "0")
    def free(candidate):
        row = conn.execute("SELECT identity_key FROM demographics WHERE bowler_id=?", (candidate,)).fetchone()
        return row is None or row["identity_key"] == exclude_identity
    if free(base):
        return base
    prefix, start = base[:9], int(base[9])
    for offset in range(1, 10):
        candidate = prefix + str((start + offset) % 10)
        if free(candidate):
            return candidate
    raise ValueError(f"No duplicate suffix remains available for USBC ID {raw_usbc!r}.")


def _backfill_bowler_ids(conn):
    rows = conn.execute("SELECT identity_key,usbc_id,bowler_id FROM demographics ORDER BY rowid").fetchall()
    changed = False
    for row in rows:
        if row["bowler_id"] or not _digits(row["usbc_id"]):
            continue
        try:
            bid = _allocate_bowler_id(conn, row["usbc_id"], row["identity_key"])
        except ValueError:
            continue
        conn.execute("UPDATE demographics SET bowler_id=? WHERE identity_key=?", (bid, row["identity_key"]))
        changed = True
    if changed:
        conn.commit()


def _columns(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
    first = _pick_header(headers, ["Bowlers First Name", "Bowler First Name", "First Name"])
    last = _pick_header(headers, ["Bowlers Last Name", "Bowler Last Name", "Last Name"])
    birth = _pick_header(headers, ["Date of birth", "Date of Birth", "DOB", "Bowlers Date of Birth"])
    gender = _pick_header(headers, ["Gender", "Bowler Gender"])
    usbc = _pick_header(headers, ["USBC ID", "USBC Number", "USBC Membership ID", "USBC #"], contains=("usbc",))
    email = _pick_header(headers, ["Email Address", "Email"])
    missing = [label for label, col in (("first name", first), ("last name", last), ("birthdate", birth), ("gender", gender)) if not col]
    if missing:
        raise ValueError("Demographic form is missing recognizable columns for: " + ", ".join(missing))
    return first, last, birth, gender, usbc, email


def update_from_csv(workspace, source_csv):
    """Upsert a demographic export and preserve local manual division/JG edits."""
    source_csv = Path(source_csv).expanduser()
    first_col, last_col, birth_col, gender_col, usbc_col, email_col = _columns(source_csv)
    conn = _connect(workspace)
    inserted = updated = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with source_csv.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                first = proper_name(row.get(first_col))
                last = proper_name(row.get(last_col))
                birth_raw = str(row.get(birth_col) or "").strip()
                gender = normalize_gender(row.get(gender_col))
                usbc = str(row.get(usbc_col) or "").strip() if usbc_col else ""
                email = str(row.get(email_col) or "").strip() if email_col else ""
                if not first or not last or not birth_raw:
                    skipped += 1
                    continue
                try:
                    birth = normalize_birthdate(birth_raw)
                except ValueError:
                    birth = birth_raw
                usbc_digits = _digits(usbc)
                identity = _person_identity(first, last, birth)
                existing = conn.execute(
                    "SELECT * FROM demographics WHERE lower(first_name)=? AND lower(last_name)=? AND birthdate=? LIMIT 1",
                    (first.casefold(), last.casefold(), birth),
                ).fetchone()
                # Backward compatibility with databases created before person-based identity keys.
                if not existing and usbc_digits:
                    candidates = conn.execute("SELECT * FROM demographics WHERE replace(replace(usbc_id,'-',''),' ','')=?", (usbc_digits,)).fetchall()
                    same_name = [r for r in candidates if _norm(r['first_name']) == _norm(first) and _norm(r['last_name']) == _norm(last)]
                    if len(same_name) == 1:
                        existing = same_name[0]
                if existing:
                    old_key = existing["identity_key"]
                    bowler_id = existing["bowler_id"] or (_allocate_bowler_id(conn, usbc, old_key) if usbc_digits else "")
                    new_key = identity
                    conflict = conn.execute("SELECT 1 FROM demographics WHERE identity_key=? AND identity_key<>?", (new_key, old_key)).fetchone()
                    if conflict:
                        new_key = old_key
                    conn.execute("""
                        UPDATE demographics SET identity_key=?,first_name=?,last_name=?,birthdate=?,gender=?,
                            usbc_id=CASE WHEN ?<>'' THEN ? ELSE usbc_id END,
                            email=CASE WHEN ?<>'' THEN ? ELSE email END,
                            bowler_id=?,updated_at=?
                        WHERE identity_key=?
                    """, (new_key, first, last, birth, gender, usbc, usbc, email, email, bowler_id, now, old_key))
                    updated += 1
                else:
                    bowler_id = _allocate_bowler_id(conn, usbc) if usbc_digits else ""
                    conn.execute("""
                        INSERT INTO demographics(identity_key,first_name,last_name,birthdate,gender,usbc_id,email,updated_at,division_override,bowler_id,jr_gold_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """, (identity, first, last, birth, gender, usbc, email, now, "", bowler_id, ""))
                    inserted += 1
        conn.commit()
    finally:
        conn.close()
    export_snapshot(workspace)
    return {"created": inserted, "updated": updated, "skipped": skipped, "database": str(database_path(workspace)), "snapshot": str(snapshot_path(workspace))}


def list_local_bowlers(workspace, search=""):
    conn = _connect(workspace)
    try:
        rows = conn.execute("SELECT * FROM demographics ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE, birthdate").fetchall()
        result = []
        needle = _norm(search)
        for r in rows:
            item = dict(r)
            item["division"] = item.get("division_override") or derived_division(item.get("birthdate"), item.get("gender"))
            if needle and needle not in _norm(f"{item['first_name']} {item['last_name']} {item['usbc_id']} {item['bowler_id']} {item['division']}"):
                continue
            result.append(item)
        return result
    finally:
        conn.close()


def add_local_bowler(workspace, *, first_name, last_name, gender, birthdate, usbc_id, division="", jr_gold_status="", email=""):
    conn = _connect(workspace)
    try:
        first, last = proper_name(first_name), proper_name(last_name)
        if not first or not last:
            raise ValueError("First and last name are required.")
        birth = normalize_birthdate(birthdate)
        gender = normalize_gender(gender)
        state = str(jr_gold_status or "").strip().upper()
        if state not in JG_STATES:
            raise ValueError("Jr. Gold status must be blank, JG, or Q.")
        usbc_digits = _digits(usbc_id)
        if not usbc_digits:
            raise ValueError("USBC ID is required.")
        identity = _person_identity(first, last, birth)
        if conn.execute("SELECT 1 FROM demographics WHERE identity_key=?", (identity,)).fetchone():
            raise ValueError("That bowler already exists in the local database.")
        bid = _allocate_bowler_id(conn, usbc_id)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("""
            INSERT INTO demographics(identity_key,first_name,last_name,birthdate,gender,usbc_id,email,updated_at,division_override,bowler_id,jr_gold_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (identity, first, last, birth, gender, str(usbc_id).strip(), str(email or "").strip(), now, str(division or "").strip(), bid, state))
        conn.commit()
    finally:
        conn.close()
    export_snapshot(workspace)
    return bid


def update_local_bowler(workspace, identity_key, *, first_name, last_name, gender, birthdate, usbc_id, division="", jr_gold_status="", email=""):
    conn = _connect(workspace)
    try:
        old = conn.execute("SELECT * FROM demographics WHERE identity_key=?", (identity_key,)).fetchone()
        if not old:
            raise ValueError("The selected bowler no longer exists in the local database.")
        first, last = proper_name(first_name), proper_name(last_name)
        if not first or not last:
            raise ValueError("First and last name are required.")
        birth = normalize_birthdate(birthdate)
        gender = normalize_gender(gender)
        state = str(jr_gold_status or "").strip().upper()
        if state not in JG_STATES:
            raise ValueError("Jr. Gold status must be blank, JG, or Q.")
        usbc_digits = _digits(usbc_id)
        if not usbc_digits:
            raise ValueError("USBC ID is required.")
        new_key = _person_identity(first, last, birth)
        conflict = conn.execute("SELECT 1 FROM demographics WHERE identity_key=? AND identity_key<>?", (new_key, identity_key)).fetchone()
        if conflict:
            raise ValueError("Another local bowler already has that name and birthdate.")
        # Keep a permanent Bowler ID stable once assigned. This prevents history links changing.
        bowler_id = old["bowler_id"] or _allocate_bowler_id(conn, usbc_id, identity_key)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("""
            UPDATE demographics SET identity_key=?,first_name=?,last_name=?,birthdate=?,gender=?,usbc_id=?,email=?,
                division_override=?,bowler_id=?,jr_gold_status=?,updated_at=? WHERE identity_key=?
        """, (new_key, first, last, birth, gender, str(usbc_id).strip(), str(email or "").strip(), str(division or "").strip(), bowler_id, state, now, identity_key))
        conn.commit()
    finally:
        conn.close()
    export_snapshot(workspace)
    return bowler_id


def delete_local_bowler(workspace, identity_key):
    conn = _connect(workspace)
    try:
        row = conn.execute("SELECT first_name,last_name FROM demographics WHERE identity_key=?", (identity_key,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM demographics WHERE identity_key=?", (identity_key,))
        conn.commit()
    finally:
        conn.close()
    export_snapshot(workspace)
    return True


def set_local_jr_gold_by_bowler_ids(workspace, bowler_ids, state="Q"):
    ids = [str(x).strip() for x in bowler_ids if str(x).strip()]
    state = str(state or "").strip().upper()
    if state not in JG_STATES:
        raise ValueError("Jr. Gold status must be blank, JG, or Q.")
    if not ids:
        return 0
    conn = _connect(workspace)
    try:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(f"UPDATE demographics SET jr_gold_status=?,updated_at=? WHERE bowler_id IN ({placeholders})", [state, datetime.now().isoformat(timespec="seconds"), *ids])
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()
    export_snapshot(workspace)
    return count


def sync_from_cloud_bowlers(workspace, bowlers):
    """Merge the private cloud permanent-bowler list into the local master DB.

    Cloud values refresh identity/demographic/Jr. Gold fields. Local-only bowlers
    and local email addresses are preserved, so pulling is non-destructive.
    """
    conn = _connect(workspace)
    created = updated = skipped = 0
    errors = []
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for index, item in enumerate(bowlers or [], start=1):
            first = proper_name(item.get("first_name"))
            last = proper_name(item.get("last_name"))
            label = f"{first} {last}".strip() or f"Cloud bowler #{index}"
            try:
                birth_raw = str(item.get("birthdate") or "").strip()
                if not first or not last or not birth_raw:
                    raise ValueError("First name, last name, and birthdate are required.")
                try:
                    birth = normalize_birthdate(birth_raw)
                except ValueError:
                    birth = birth_raw
                gender = normalize_gender(item.get("gender"))
                usbc = str(item.get("usbc_id_raw") or item.get("usbc_id") or "").strip()
                bowler_id = str(item.get("bowler_id") or "").strip()
                division = str(item.get("division") or "").strip()
                state = str(item.get("jr_gold_state") or item.get("jr_gold_status") or "").strip().upper()
                if state not in JG_STATES:
                    state = ""
                if bowler_id and (not bowler_id.isdigit() or len(bowler_id) != 10):
                    raise ValueError(f"Cloud Bowler ID {bowler_id!r} is not 10 digits.")
                identity = _person_identity(first, last, birth)

                existing = None
                if bowler_id:
                    existing = conn.execute("SELECT * FROM demographics WHERE bowler_id=?", (bowler_id,)).fetchone()
                if not existing:
                    existing = conn.execute("SELECT * FROM demographics WHERE identity_key=?", (identity,)).fetchone()
                if existing:
                    old_key = existing["identity_key"]
                    email = existing["email"] or ""
                    target_bid = bowler_id or existing["bowler_id"]
                    if target_bid and target_bid != existing["bowler_id"]:
                        conflict = conn.execute("SELECT identity_key FROM demographics WHERE bowler_id=? AND identity_key<>?", (target_bid, old_key)).fetchone()
                        if conflict:
                            raise ValueError(f"Bowler ID {target_bid} is already attached to another local bowler.")
                    key_conflict = conn.execute("SELECT identity_key FROM demographics WHERE identity_key=? AND identity_key<>?", (identity, old_key)).fetchone()
                    new_key = old_key if key_conflict else identity
                    conn.execute(
                        "UPDATE demographics SET identity_key=?,first_name=?,last_name=?,birthdate=?,gender=?,usbc_id=CASE WHEN ?<>'' THEN ? ELSE usbc_id END,email=?,division_override=?,bowler_id=?,jr_gold_status=?,updated_at=? WHERE identity_key=?",
                        (new_key, first, last, birth, gender, usbc, usbc, email, division, target_bid, state, now, old_key),
                    )
                    updated += 1
                else:
                    if not bowler_id:
                        if not _digits(usbc):
                            raise ValueError("Cloud record has neither a Bowler ID nor a usable USBC ID.")
                        bowler_id = _allocate_bowler_id(conn, usbc)
                    if conn.execute("SELECT 1 FROM demographics WHERE bowler_id=?", (bowler_id,)).fetchone():
                        raise ValueError(f"Bowler ID {bowler_id} already exists locally.")
                    conn.execute(
                        "INSERT INTO demographics(identity_key,first_name,last_name,birthdate,gender,usbc_id,email,updated_at,division_override,bowler_id,jr_gold_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, first, last, birth, gender, usbc, "", now, division, bowler_id, state),
                    )
                    created += 1
            except Exception as exc:
                skipped += 1
                errors.append(f"{label}: {exc}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    export_snapshot(workspace)
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}

def export_snapshot(workspace):
    """Export the local database using legacy-compatible names plus local metadata."""
    path = snapshot_path(workspace)
    rows = list_local_bowlers(workspace)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Bowlers First Name", "Bowlers Last Name", "Date of birth", "Gender", "USBC ID", "Email Address", "Division", "Bowler ID", "Jr Gold Status"])
        for r in rows:
            w.writerow([r["first_name"], r["last_name"], r["birthdate"], r["gender"], r["usbc_id"], r["email"], r["division"], r["bowler_id"], r["jr_gold_status"]])
    return path


def count_rows(workspace):
    conn = _connect(workspace)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM demographics").fetchone()[0])
    finally:
        conn.close()



def require_database(workspace):
    """Return the local master database path after confirming it has bowler records."""
    if count_rows(workspace) < 1:
        raise ValueError(
            "The local bowler database is empty. Open Step 1 — Bowler Database and import a demographic form once before preparing this tournament."
        )
    return database_path(workspace)


def require_snapshot(workspace):
    if count_rows(workspace) < 1:
        raise ValueError("The local demographic database is empty. Open Step 1 — Bowler Database and import a demographic form once before preparing this tournament.")
    return export_snapshot(workspace)


def missing_from_registration(workspace, registration_csv):
    """Return tournament registration rows that do not have a master DB match."""
    registration_csv = Path(registration_csv)
    with registration_csv.open('r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        first_col = _pick_header(headers, ["Bowlers First Name", "Bowler First Name", "First Name"])
        last_col = _pick_header(headers, ["Bowlers Last Name", "Bowler Last Name", "Last Name"])
        birth_col = _pick_header(headers, ["Bowlers Date of Birth", "Date of birth", "Date of Birth", "DOB"])
        if not first_col or not last_col:
            raise ValueError("Tournament entries need recognizable first- and last-name columns.")
        entries=list(reader)
    conn=_connect(workspace)
    try:
        master=conn.execute("SELECT first_name,last_name,birthdate FROM demographics").fetchall()
    finally:
        conn.close()
    by_name={}
    for r in master:
        by_name.setdefault((_norm(r['first_name']),_norm(r['last_name'])),[]).append(r)
    missing=[]
    for row_no,row in enumerate(entries,start=2):
        first=proper_name(row.get(first_col)); last=proper_name(row.get(last_col))
        if not first or not last: continue
        matches=by_name.get((_norm(first),_norm(last)),[])
        birth=(row.get(birth_col) or '').strip() if birth_col else ''
        if birth and matches:
            try: birth=normalize_birthdate(birth)
            except Exception: pass
            exact=[r for r in matches if str(r['birthdate']).strip()==birth]
            if exact: matches=exact
        if not matches:
            missing.append({"row":row_no,"first_name":first,"last_name":last,"birthdate":birth})
    return missing
