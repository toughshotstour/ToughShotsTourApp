#!/usr/bin/env python3
"""Permanent bowler database and public-results publishing helpers."""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path

from core.lane_scoring import _json_request, normalize_base_url, proper_name

DIVISIONS = ["U12 Mixed","U14 Boys","U14 Girls","U16 Boys","U16 Girls","U18 Boys","U18 Girls"]


def _pick_header(headers, exact=(), contains=()):
    lookup = {h.casefold().strip(): h for h in headers}
    for name in exact:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    for h in headers:
        key = h.casefold()
        if all(term.casefold() in key for term in contains):
            return h
    return None


def demographic_rows(path):
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        first = _pick_header(headers, ["Bowlers First Name", "Bowler First Name", "First Name"])
        last = _pick_header(headers, ["Bowlers Last Name", "Bowler Last Name", "Last Name"])
        birth = _pick_header(headers, ["Date of birth", "Date of Birth", "DOB"])
        gender = _pick_header(headers, ["Gender", "Bowler Gender"])
        usbc = _pick_header(headers, ["USBC ID", "USBC Number", "USBC Membership ID", "USBC #"], contains=("usbc",))
        missing = [label for label, col in [("first name",first),("last name",last),("birthdate",birth),("gender",gender),("USBC ID",usbc)] if not col]
        if missing:
            raise ValueError("Demographic form is missing recognizable columns for: " + ", ".join(missing) + ". The USBC column name must contain 'USBC'.")
        result=[]
        for row_no,row in enumerate(reader,start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            result.append({"first_name":proper_name(row.get(first)),"last_name":proper_name(row.get(last)),"birthdate":(row.get(birth) or "").strip(),"gender":(row.get(gender) or "").strip(),"usbc_id":(row.get(usbc) or "").strip(),"source_row":row_no})
    return result


def import_bowlers(base_url, admin_key, demographic_path):
    rows = demographic_rows(demographic_path)
    return _json_request(normalize_base_url(base_url)+"/api/bowlers/import", method="POST", payload={"bowlers": rows}, admin_key=admin_key.strip())


def list_bowlers(base_url, admin_key):
    return _json_request(normalize_base_url(base_url)+"/api/bowlers", admin_key=admin_key.strip())


def set_jr_gold(base_url, admin_key, bowler_id, state):
    return _json_request(normalize_base_url(base_url)+f"/api/bowlers/{bowler_id}/jr-gold", method="PATCH", payload={"state":state}, admin_key=admin_key.strip())


def _current_tournament_id(manifest_path, db_path):
    p=Path(manifest_path) if manifest_path else None
    if p and p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["tournament_id"]
        except Exception:
            pass
    # Stable fallback for a local DB if lane scoring has not been prepared yet.
    import hashlib
    return "local-" + hashlib.sha1(str(Path(db_path).resolve()).encode()).hexdigest()[:16]


def qualifying_payload(roster_path, manifest_path, tournament_name, event_date=None):
    from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
    db_path = resolve_database_path(Path(roster_path))
    tdb = TournamentDB(db_path)
    try:
        if not tdb.roster_loaded():
            tdb.import_roster(Path(roster_path))
        divisions={}
        for division in DIVISIONS:
            if division not in tdb.divisions():
                divisions[division]=[]
                continue
            out=[]
            for r in tdb.qualifying_rows(division):
                item={k:r[k] for k in ("first_name","last_name","birthdate","rank","scores","complete","total","average")}
                item["first_name"]=proper_name(item["first_name"]); item["last_name"]=proper_name(item["last_name"])
                out.append(item)
            divisions[division]=out
    finally:
        tdb.close()
    return {"tournament_id":_current_tournament_id(manifest_path,db_path),"tournament_name":tournament_name.strip() or "Tough Shots Tournament","event_date":event_date or date.today().isoformat(),"divisions":divisions}


def publish_qualifying(base_url, admin_key, roster_path, manifest_path, tournament_name, event_date=None):
    payload=qualifying_payload(roster_path,manifest_path,tournament_name,event_date)
    return _json_request(normalize_base_url(base_url)+"/api/public/qualifying",method="POST",payload=payload,admin_key=admin_key.strip())


def _performance_for_division(tdb, division):
    qrows=tdb.qualifying_rows(division)
    bracket=tdb.load_bracket(division)
    perf={r["bowler_id"]:{"first_name":proper_name(r["first_name"]),"last_name":proper_name(r["last_name"]),"birthdate":r["birthdate"],"division":division,"qualifying_rank":r["rank"],"scores":r["scores"],"qualifying_total":r["total"],"qualifying_average":r["average"],"high_game":max([x for x in r["scores"] if x is not None],default=None),"match_wins":0,"match_losses":0,"finish_label":"Did not enter match play","boy_points":0} for r in qrows}
    rounds=(bracket or {}).get("rounds") or []
    # Track every played match. A loss identifies the elimination round; final winner is champion.
    for ridx, matches in enumerate(rounds):
        for m in matches:
            p1,p2,w=m.get("p1"),m.get("p2"),m.get("winner")
            if not p1 or not p2 or not w:
                continue
            loser=p2 if w==p1 else p1
            if w in perf: perf[w]["match_wins"] += 1
            if loser in perf:
                perf[loser]["match_losses"] += 1
                remaining=len(matches)
                if ridx==len(rounds)-1: label="Runner-up"
                elif remaining==2: label="Semifinalist"
                elif remaining==4: label="Quarterfinalist"
                else: label=f"Eliminated in round {ridx+1}"
                perf[loser]["finish_label"]=label
    if rounds:
        final=rounds[-1][0]
        champ=final.get("winner")
        if champ in perf: perf[champ]["finish_label"]="Champion"
        # Any bracket entrant without a finished loss yet is still in match play.
        entrants=set()
        for m in rounds[0]:
            entrants.update(x for x in (m.get("p1"),m.get("p2")) if x)
        for bid in entrants:
            if bid in perf and perf[bid]["finish_label"]=="Did not enter match play":
                perf[bid]["finish_label"]="Match play participant"
    # Bowler of the Year points:
    # qualifying: last place=1, then +1 per place through first;
    # match play: 5 per win; runner-up +10; champion +20.
    field_size = len(qrows)
    for item in perf.values():
        rank = item.get("qualifying_rank")
        qualifying_points = (field_size - int(rank) + 1) if rank else 0
        match_points = int(item.get("match_wins", 0)) * 5
        bonus = 20 if item.get("finish_label") == "Champion" else (10 if item.get("finish_label") == "Runner-up" else 0)
        item["boy_points"] = qualifying_points + match_points + bonus
    return list(perf.values())


def _norm_birthdate_local(value):
    raw=(value or "").strip()
    for fmt in ("%Y-%m-%d","%m/%d/%Y","%m/%d/%y"):
        try: return datetime.strptime(raw,fmt).date().isoformat()
        except ValueError: pass
    return raw

def _norm_person_key(first, last, birthdate):
    return (" ".join((first or "").strip().casefold().split()), " ".join((last or "").strip().casefold().split()), _norm_birthdate_local(birthdate))


def _jg_settings(tdb):
    try:
        raw = json.loads(tdb.get_meta("jr_gold_settings", "{}") or "{}")
    except Exception:
        raw = {}
    merges = {age: bool((raw.get("merges") or {}).get(age, False)) for age in ("U14","U16","U18")}
    cuts = {str(k): int(v) for k,v in (raw.get("cuts") or {}).items() if str(v).isdigit()}
    return {"merges": merges, "cuts": cuts}


def jr_gold_payload(base_url, admin_key, roster_path, manifest_path, tournament_name, event_date=None):
    from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
    db_path = resolve_database_path(Path(roster_path)); tdb = TournamentDB(db_path)
    try:
        if not tdb.roster_loaded(): tdb.import_roster(Path(roster_path))
        permanent = list_bowlers(base_url, admin_key).get("bowlers", [])
        eligible = {}
        for b in permanent:
            if (b.get("jr_gold_state") or "") in {"JG","Q"}:
                eligible[_norm_person_key(b.get("first_name"), b.get("last_name"), b.get("birthdate"))] = b
        settings = _jg_settings(tdb)
        groups = {}
        for division in DIVISIONS:
            if division not in tdb.divisions(): continue
            for r in tdb.qualifying_rows(division):
                # Cloud stores normalized ISO birthdates, while local roster may use M/D/Y.
                matches = [b for b in permanent if (b.get("jr_gold_state") or "") in {"JG","Q"} and
                           " ".join((b.get("first_name") or "").strip().casefold().split()) == " ".join(r["first_name"].strip().casefold().split()) and
                           " ".join((b.get("last_name") or "").strip().casefold().split()) == " ".join(r["last_name"].strip().casefold().split())]
                if r.get("birthdate"):
                    local_dob=_norm_birthdate_local(r.get("birthdate"))
                    exact=[b for b in matches if _norm_birthdate_local(b.get("birthdate")) == local_dob]
                    if exact: matches=exact
                if len(matches) != 1: continue
                pb=matches[0]
                group=division
                for age in ("U14","U16","U18"):
                    if settings["merges"].get(age) and division in {f"{age} Boys",f"{age} Girls"}:
                        group=f"{age} Combined"
                item={k:r[k] for k in ("first_name","last_name","birthdate","scores","complete","total","average")}
                item["first_name"]=proper_name(item["first_name"]); item["last_name"]=proper_name(item["last_name"])
                item["jr_gold_state"]=pb.get("jr_gold_state") or ""
                item["bowler_id"]=pb.get("bowler_id")
                item["source_division"]=division
                item["_reverse_games"]=tuple((v if v is not None else -1) for v in reversed(r["scores"]))
                groups.setdefault(group,[]).append(item)
        out={}
        for group, rows in groups.items():
            rows.sort(key=lambda r:(-r["total"], tuple(-x for x in r["_reverse_games"]), r["last_name"].casefold(), r["first_name"].casefold()))
            for i,r in enumerate(rows,1): r["rank"]=i; r.pop("_reverse_games",None)
            cut=settings["cuts"].get(group, min(1,len(rows)))
            out[group]={"cut_size": max(0,min(int(cut),len(rows))), "rows":rows}
        # Include empty configured groups so the public navigation stays predictable.
        expected=["U12 Mixed"]
        for age in ("U14","U16","U18"):
            expected += [f"{age} Combined"] if settings["merges"].get(age) else [f"{age} Boys",f"{age} Girls"]
        for group in expected: out.setdefault(group,{"cut_size":settings["cuts"].get(group,0),"rows":[]})
    finally: tdb.close()
    return {"tournament_id":_current_tournament_id(manifest_path,db_path),"tournament_name":tournament_name.strip() or "Tough Shots Tournament","event_date":event_date or date.today().isoformat(),"groups":out,"merges":settings["merges"]}


def publish_jr_gold(base_url, admin_key, roster_path, manifest_path, tournament_name, event_date=None):
    payload=jr_gold_payload(base_url,admin_key,roster_path,manifest_path,tournament_name,event_date)
    return _json_request(normalize_base_url(base_url)+"/api/public/jr-gold",method="POST",payload=payload,admin_key=admin_key.strip())


def archive_payload(roster_path, manifest_path, tournament_name, event_date=None):
    from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
    db_path=resolve_database_path(Path(roster_path)); tdb=TournamentDB(db_path)
    try:
        if not tdb.roster_loaded():
            tdb.import_roster(Path(roster_path))
        rows=[]
        for division in DIVISIONS:
            if division in tdb.divisions(): rows.extend(_performance_for_division(tdb,division))
    finally: tdb.close()
    return {"tournament_id":_current_tournament_id(manifest_path,db_path),"tournament_name":tournament_name.strip() or "Tough Shots Tournament","event_date":event_date or date.today().isoformat(),"performances":rows}


def archive_tournament(base_url, admin_key, roster_path, manifest_path, tournament_name, event_date=None):
    payload=archive_payload(roster_path,manifest_path,tournament_name,event_date)
    return _json_request(normalize_base_url(base_url)+"/api/public/archive",method="POST",payload=payload,admin_key=admin_key.strip())
