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


def _row_highlights(rows):
    high_game = -1
    high_game_names = []
    high3 = -1
    high3_names = []
    for row in rows:
        name = f"{proper_name(row.get('first_name'))} {proper_name(row.get('last_name'))}".strip()
        scores = row.get("scores") or []
        valid = [int(x) for x in scores if x is not None]
        if valid:
            hg = max(valid)
            if hg > high_game:
                high_game, high_game_names = hg, [name]
            elif hg == high_game:
                high_game_names.append(name)
        if len(scores) >= 3 and all(x is not None for x in scores[:3]):
            h3 = sum(int(x) for x in scores[:3])
            if h3 > high3:
                high3, high3_names = h3, [name]
            elif h3 == high3:
                high3_names.append(name)
    return high_game, high_game_names, high3, high3_names


def _draw_standings_page(c, w, h, *, title, heading, rows, cut=0, games=6, show_jg=False, show_highlights=False, highlight_rows=None):
    """Draw the paper standings using the same columns/order as the public site."""
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

    # Website-equivalent columns: Rank | Bowler | [JG] | Games 1-6 | Total | Avg.
    rank_w = 0.52 * inch
    name_w = 2.25 * inch
    jg_w = 0.50 * inch if show_jg else 0
    total_w = 0.72 * inch
    avg_w = 0.72 * inch
    games_w = (w - 2 * margin - rank_w - name_w - jg_w - total_w - avg_w)
    widths = [rank_w, name_w]
    if show_jg:
        widths.append(jg_w)
    widths += [games_w, total_w, avg_w]
    xs = [margin]
    for width in widths:
        xs.append(xs[-1] + width)
    headers = ["Rank", "Bowler"] + (["JG"] if show_jg else []) + ["Games 1–6", "Total", "Avg."]

    display_rows = []
    for idx, row in enumerate(rows, 1):
        display_rows.append(row)
        row_rank = int(row.get("rank", idx) or idx)
        if cut and row_rank == cut:
            display_rows.append(None)

    header_h = 0.34 * inch
    stats_h = 0.64 * inch if show_highlights else 0
    available = y - margin - header_h - stats_h
    row_h = min(0.34 * inch, available / max(1, len(display_rows)))
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

    c.setFont("Helvetica-Bold", 9)
    for i, label in enumerate(headers):
        c.drawCentredString((xs[i] + xs[i + 1]) / 2, y - header_h / 2 - 3, label)

    c.setFont("Helvetica", 9)
    for display_idx, row in enumerate(display_rows):
        if row is None:
            continue
        ym = y - header_h - (display_idx + 0.5) * row_h
        values = [str(row.get("rank", display_idx + 1)), f"{proper_name(row.get('first_name'))} {proper_name(row.get('last_name'))}".strip()]
        if show_jg:
            values.append(str(row.get("jr_gold_state") or ""))
        scores = row.get("scores") or []
        game_text = " / ".join("—" if i >= len(scores) or scores[i] is None else str(scores[i]) for i in range(games))
        total = str(row.get("total", 0)) if row.get("complete") else str(row.get("total", "") or "")
        avg = row.get("average")
        if avg is None and row.get("complete") and games:
            try:
                avg = float(row.get("total", 0)) / games
            except Exception:
                avg = None
        avg_text = "—" if avg is None else f"{float(avg):.2f}"
        values += [game_text, total, avg_text]
        for col, value in enumerate(values):
            if col == 1:
                c.drawString(xs[col] + 5, ym - 3, value)
            else:
                c.drawCentredString((xs[col] + xs[col + 1]) / 2, ym - 3, value)

    if show_highlights:
        hg, hg_names, h3, h3_names = _row_highlights(highlight_rows if highlight_rows is not None else rows)
        stat_y = bottom - 18
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, stat_y, "High Game")
        c.setFont("Helvetica", 10)
        hg_text = "—" if hg < 0 else f"{hg} — {', '.join(hg_names)}"
        c.drawString(margin + 0.82 * inch, stat_y, hg_text)
        stat_y -= 18
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, stat_y, "High 3-Game Set")
        c.setFont("Helvetica", 10)
        h3_text = "—" if h3 < 0 else f"{h3} — {', '.join(h3_names)}"
        c.drawString(margin + 1.18 * inch, stat_y, h3_text)


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
            # Keep a whole division together whenever practical so high-game/high-3
            # summaries match the website page directly.
            chunks = [rows[i:i+17] for i in range(0, len(rows), 17)] or [[]]
            for page_index, chunk in enumerate(chunks):
                if not first:
                    c.showPage()
                first = False
                _draw_standings_page(
                    c, w, h, title=print_title, heading=f"Qualifying - {division}",
                    rows=chunk, cut=db.cut_size(division),
                    games=db.qualifying_games, show_jg=False,
                    show_highlights=(page_index == len(chunks) - 1), highlight_rows=rows,
                )
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
        for i, r in enumerate(rows, 1):
            r["rank"] = i
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
                if not first:
                    c.showPage()
                first = False
                cut = int(settings.get("cuts", {}).get(group, 0) or 0)
                _draw_standings_page(c, w, h, title=print_title, heading=f"Jr. Gold - {group}", rows=chunk, cut=cut, games=db.qualifying_games, show_jg=True, show_highlights=False)
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
                c.drawRightString(w-margin, y, "8-Bowler Bracket Sheet")
                y -= 16; c.line(margin, y, w-margin, y); y -= 18
                # Every printed bracket sheet is sized for four matches (eight bowlers).
                # If the round has fewer than four matches, the unused match boxes stay blank.
                match_h = (y-margin) / 4; box_w = w - 2*margin
                for m_idx in range(4):
                    match = chunk[m_idx] if m_idx < len(chunk) else None
                    top = y - m_idx*match_h; bottom = top-match_h+10; mid=(top+bottom)/2
                    c.setLineWidth(1.3); c.rect(margin,bottom,box_w,match_h-10); c.line(margin,mid,w-margin,mid)
                    for slot, yy in ((1,(top+mid)/2),(2,(mid+bottom)/2)):
                        bid=match.get(f"p{slot}") if match else None
                        seed=seed_by_bowler.get(bid) if bid else None
                        name=" ".join(proper_name(part) for part in db.display_name(bid).split()) if bid else ("BYE / Waiting" if match else "")
                        prefix=f"#{seed}  " if seed else ""
                        c.setFont("Helvetica-Bold",11); c.drawString(margin+12,yy-4,prefix+name)
                        if match:
                            c.setFont("Helvetica",9); c.drawRightString(w-margin-72,yy-4,"Score:"); c.rect(w-margin-64,yy-11,50,20)
        c.save()
        return path, stage + 1, [x["division"] for x in selected]
    finally:
        db.close()
