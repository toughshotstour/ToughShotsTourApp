from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from core.lane_scoring import proper_name


def _output_dir(workspace):
    path = Path(workspace) / "printed_forms"
    path.mkdir(parents=True, exist_ok=True)
    return path


def send_pdf_to_printer(pdf_path):
    pdf_path = Path(pdf_path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(pdf_path), "print")  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["lp", str(pdf_path)])
        return True
    except Exception:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(pdf_path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(pdf_path)])
            else:
                subprocess.Popen(["xdg-open", str(pdf_path)])
        except Exception:
            pass
        return False


def _open_db(roster_path):
    from tournament.bowling_tournament_manager import TournamentDB, resolve_database_path
    roster_path = Path(roster_path)
    db = TournamentDB(resolve_database_path(roster_path))
    if not db.roster_loaded():
        db.import_roster(roster_path)
    return db


def _draw_standings_page(c, w, h, *, title, heading, rows, cut=0, games=6):
    from reportlab.lib.units import inch
    margin = 0.35 * inch
    y = h - margin
    if title:
        c.setFont("Helvetica-Bold", 17)
        c.drawCentredString(w / 2, y - 2, title)
        y -= 24
    c.setFont("Helvetica-Bold", 15)
    c.drawString(margin, y, "Tough Shots Tour")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - margin, y, heading)
    y -= 22

    name_w = 2.35 * inch
    rank_w = 0.42 * inch
    total_w = 0.68 * inch
    game_w = (w - 2 * margin - rank_w - name_w - total_w) / max(1, games)
    xs = [margin, margin + rank_w, margin + rank_w + name_w]
    for _ in range(games):
        xs.append(xs[-1] + game_w)
    xs.append(w - margin)
    header_h = 0.34 * inch

    # A cut is shown as a completely blank table row after the final qualifier,
    # rather than as a heavy rule. This is easier to spot on paper and mirrors
    # the public standings page.
    display_rows = []
    for idx, row in enumerate(rows, 1):
        display_rows.append(row)
        if cut and idx == cut and idx < len(rows):
            display_rows.append(None)

    row_h = min(0.34 * inch, (y - margin - header_h) / max(1, len(display_rows)))
    bottom = y - header_h - row_h * len(display_rows)
    c.setLineWidth(1.2)
    c.rect(margin, bottom, w - 2 * margin, y - bottom)
    c.setLineWidth(0.7)
    for x in xs[1:-1]:
        c.line(x, bottom, x, y)
    c.line(margin, y - header_h, w - margin, y - header_h)
    for r in range(1, len(display_rows)):
        yy = y - header_h - r * row_h
        c.line(margin, yy, w - margin, yy)

    headers = ["#", "Bowler"] + [str(i) for i in range(1, games + 1)] + ["Total"]
    c.setFont("Helvetica-Bold", 9)
    for i, label in enumerate(headers):
        c.drawCentredString((xs[i] + xs[i + 1]) / 2, y - header_h / 2 - 3, label)

    c.setFont("Helvetica", 9)
    for display_idx, row in enumerate(display_rows):
        if row is None:
            continue
        ym = y - header_h - (display_idx + 0.5) * row_h
        c.drawCentredString((xs[0] + xs[1]) / 2, ym - 3, str(row.get("rank", display_idx + 1)))
        c.drawString(xs[1] + 5, ym - 3, f"{proper_name(row.get('first_name'))} {proper_name(row.get('last_name'))}")
        scores = row.get("scores") or []
        for g in range(games):
            val = "" if g >= len(scores) or scores[g] is None else str(scores[g])
            c.drawCentredString((xs[2 + g] + xs[3 + g]) / 2, ym - 3, val)
        total = row.get("total", "") if row.get("complete") else ""
        c.drawCentredString((xs[-2] + xs[-1]) / 2, ym - 3, str(total))


def create_qualifying_pdf(roster_path, workspace, print_title=""):
    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("Printing requires reportlab. Install requirements.txt.") from exc
    db = _open_db(roster_path)
    try:
        path = _output_dir(workspace) / "qualifying_all_divisions.pdf"
        c = canvas.Canvas(str(path), pagesize=landscape(letter))
        w, h = landscape(letter)
        first = True
        for division in db.divisions():
            rows = db.qualifying_rows(division)
            chunks = [rows[i:i+18] for i in range(0, len(rows), 18)] or [[]]
            for page_index, chunk in enumerate(chunks):
                if not first: c.showPage()
                first = False
                # rank stays tournament-wide within division even on page 2
                _draw_standings_page(c, w, h, title=print_title, heading=f"Qualifying - {division}", rows=chunk, cut=(db.cut_size(division) if page_index == 0 else 0), games=db.qualifying_games)
        c.save()
        return path
    finally:
        db.close()


def _jr_gold_local_groups(db):
    settings = db.jr_gold_settings()
    groups = {name: [] for name in db.jr_gold_group_names(settings)}
    for division in db.divisions():
        for row in db.qualifying_rows(division):
            raw = db.bowler(row["bowler_id"])
            try:
                src = json.loads(raw["source_json"] or "{}") if raw else {}
            except Exception:
                src = {}
            status = str(src.get("Jr_Gold_Status") or src.get("Jr Gold Status") or src.get("JG Status") or "").strip().upper()
            if status not in {"JG", "Q"}:
                continue
            group = division
            for age in ("U14", "U16", "U18"):
                if settings["merges"].get(age) and division in {f"{age} Boys", f"{age} Girls"}:
                    group = f"{age} Combined"
            item = dict(row)
            item["jr_gold_state"] = status
            groups.setdefault(group, []).append(item)
    for rows in groups.values():
        rows.sort(key=lambda r: (-r["total"], tuple(-(x if x is not None else -1) for x in reversed(r["scores"])), r["last_name"].casefold(), r["first_name"].casefold()))
        for i, r in enumerate(rows, 1): r["rank"] = i
    return settings, groups


def create_jr_gold_pdf(roster_path, workspace, print_title=""):
    try:
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("Printing requires reportlab. Install requirements.txt.") from exc
    db = _open_db(roster_path)
    try:
        settings, groups = _jr_gold_local_groups(db)
        path = _output_dir(workspace) / "jr_gold_qualifying_all_groups.pdf"
        c = canvas.Canvas(str(path), pagesize=landscape(letter))
        w, h = landscape(letter)
        first = True
        for group in db.jr_gold_group_names(settings):
            rows = groups.get(group, [])
            chunks = [rows[i:i+18] for i in range(0, len(rows), 18)] or [[]]
            for page_index, chunk in enumerate(chunks):
                if not first: c.showPage()
                first = False
                cut = int(settings.get("cuts", {}).get(group, 0) or 0) if page_index == 0 else 0
                _draw_standings_page(c, w, h, title=print_title, heading=f"Jr. Gold - {group}", rows=chunk, cut=cut, games=db.qualifying_games)
        c.save()
        return path
    finally:
        db.close()


def _current_round_info(db, division):
    from tournament.bowling_tournament_manager import bracket_round_name
    state = db.load_bracket(division)
    if not state: return None
    for idx, matches in enumerate(state.get("rounds", [])):
        if any(m.get("p1") and m.get("p2") and not m.get("winner") for m in matches):
            return {"division": division, "state": state, "round_index": idx, "matches": matches, "round_name": bracket_round_name(len(matches))}
    return None


def create_current_brackets_pdf(roster_path, workspace, print_title=""):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("Bracket printing requires reportlab. Install requirements.txt.") from exc
    db = _open_db(roster_path)
    try:
        infos = [x for d in db.divisions() if (x := _current_round_info(db, d))]
        if not infos:
            raise ValueError("There are no unfinished match-play rounds to print.")
        # Tournament round = relative round index. First round of an 8-person cut
        # prints alongside first round of a 16-person cut.
        stage = min(x["round_index"] for x in infos)
        selected = [x for x in infos if x["round_index"] == stage]
        path = _output_dir(workspace) / f"brackets_round_{stage + 1}_all_divisions.pdf"
        c = canvas.Canvas(str(path), pagesize=letter)
        w, h = letter; margin = 0.45 * inch; first_page = True
        for info in selected:
            division, state, matches, round_name = info["division"], info["state"], info["matches"], info["round_name"]
            active = [m for m in matches if m.get("p1") or m.get("p2")]
            chunks = [active[i:i+4] for i in range(0, len(active), 4)]
            seed_by_bowler = {bid: int(seed) for seed, bid in state.get("seed_map", {}).items() if bid}
            for chunk in chunks:
                if not first_page: c.showPage()
                first_page = False
                y = h - margin
                if print_title:
                    c.setFont("Helvetica-Bold", 17); c.drawCentredString(w/2, y, print_title); y -= 25
                c.setFont("Helvetica-Bold", 15); c.drawString(margin, y, "Tough Shots Tour")
                c.setFont("Helvetica-Bold", 12); c.drawRightString(w-margin, y, division); y -= 19
                c.setFont("Helvetica-Bold", 12); c.drawString(margin, y, f"Tournament Round {stage + 1} - {round_name}")
                c.drawRightString(w-margin, y, f"{min(8, len(chunk)*2)}-Bowler Bracket Sheet")
                y -= 16; c.line(margin, y, w-margin, y); y -= 18
                match_h = (y-margin) / max(1, len(chunk)); box_w = w - 2*margin
                for m_idx, match in enumerate(chunk):
                    top = y - m_idx*match_h; bottom = top-match_h+10; mid=(top+bottom)/2
                    c.setLineWidth(1.3); c.rect(margin,bottom,box_w,match_h-10); c.line(margin,mid,w-margin,mid)
                    for slot, yy in ((1,(top+mid)/2),(2,(mid+bottom)/2)):
                        bid=match.get(f"p{slot}"); seed=seed_by_bowler.get(bid) if bid else None
                        name=db.display_name(bid) if bid else "BYE / Waiting"; prefix=f"#{seed}  " if seed else ""
                        c.setFont("Helvetica-Bold",11); c.drawString(margin+12,yy-4,prefix+name)
                        c.setFont("Helvetica",9); c.drawRightString(w-margin-72,yy-4,"Score:"); c.rect(w-margin-64,yy-11,50,20)
        c.save()
        return path, stage + 1, [x["division"] for x in selected]
    finally:
        db.close()
