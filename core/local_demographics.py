#!/usr/bin/env python3
"""Reusable local demographic database for Tough Shots.

The demographic Google Form no longer has to be selected for every tournament.
Importing a form updates a small SQLite database in the tournament workspace.
A canonical CSV snapshot is exported from that database whenever the existing
matching/division tools need demographic data.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path


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


def database_path(workspace):
    return Path(workspace).expanduser() / "local_demographics.sqlite3"


def snapshot_path(workspace):
    return Path(workspace).expanduser() / "demographic_master.csv"


def _connect(workspace):
    path = database_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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
    conn.commit()
    return conn


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
    """Upsert a demographic export into the local database and write a snapshot."""
    source_csv = Path(source_csv).expanduser()
    first_col, last_col, birth_col, gender_col, usbc_col, email_col = _columns(source_csv)
    conn = _connect(workspace)
    inserted = updated = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with source_csv.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                first = str(row.get(first_col) or "").strip()
                last = str(row.get(last_col) or "").strip()
                birth = str(row.get(birth_col) or "").strip()
                gender = str(row.get(gender_col) or "").strip()
                usbc = str(row.get(usbc_col) or "").strip() if usbc_col else ""
                email = str(row.get(email_col) or "").strip() if email_col else ""
                if not first or not last or not birth:
                    skipped += 1
                    continue
                usbc_digits = _digits(usbc)
                identity = ("usbc:" + usbc_digits) if usbc_digits else f"person:{_norm(first)}|{_norm(last)}|{_norm(birth)}"
                exists = conn.execute("SELECT 1 FROM demographics WHERE identity_key=?", (identity,)).fetchone()
                conn.execute("""
                    INSERT INTO demographics(identity_key,first_name,last_name,birthdate,gender,usbc_id,email,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(identity_key) DO UPDATE SET
                        first_name=excluded.first_name,
                        last_name=excluded.last_name,
                        birthdate=excluded.birthdate,
                        gender=excluded.gender,
                        usbc_id=CASE WHEN excluded.usbc_id<>'' THEN excluded.usbc_id ELSE demographics.usbc_id END,
                        email=CASE WHEN excluded.email<>'' THEN excluded.email ELSE demographics.email END,
                        updated_at=excluded.updated_at
                """, (identity, first, last, birth, gender, usbc, email, now))
                if exists: updated += 1
                else: inserted += 1
        conn.commit()
    finally:
        conn.close()
    export_snapshot(workspace)
    return {"created": inserted, "updated": updated, "skipped": skipped, "database": str(database_path(workspace)), "snapshot": str(snapshot_path(workspace))}


def export_snapshot(workspace):
    """Export the local database using the column names expected by legacy tools."""
    path = snapshot_path(workspace)
    conn = _connect(workspace)
    try:
        rows = conn.execute("""
            SELECT first_name,last_name,birthdate,gender,usbc_id,email
            FROM demographics
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE, birthdate
        """).fetchall()
    finally:
        conn.close()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Bowlers First Name", "Bowlers Last Name", "Date of birth", "Gender", "USBC ID", "Email Address"])
        w.writerows(rows)
    return path


def count_rows(workspace):
    conn = _connect(workspace)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM demographics").fetchone()[0])
    finally:
        conn.close()


def require_snapshot(workspace):
    if count_rows(workspace) < 1:
        raise ValueError(
            "The local demographic database is empty. Open Step 2 — Demographics and import a demographic form once before preparing this tournament."
        )
    return export_snapshot(workspace)
