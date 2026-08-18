#!/usr/bin/env python3
"""Lane assignment, cloud publishing, score-sheet generation, and score sync."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import secrets
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def roster_rows(roster_path):
    roster_path = Path(roster_path)
    with roster_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"First_Name", "Last_Name", "Division"}
        headers = set(reader.fieldnames or [])
        missing = required - headers
        if missing:
            raise ValueError("Roster is missing required columns: " + ", ".join(sorted(missing)))
        rows = list(reader)

    result = []
    for source_row, row in enumerate(rows, start=2):
        first = (row.get("First_Name") or "").strip()
        last = (row.get("Last_Name") or "").strip()
        division = (row.get("Division") or "").strip()
        birthdate = (row.get("Birthdate_Used") or "").strip()
        if not first or not last or not division:
            continue
        raw_key = f"{source_row}|{first}|{last}|{birthdate}|{division}"
        bowler_id = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]
        result.append({
            "bowler_id": bowler_id,
            "first_name": first,
            "last_name": last,
            "division": division,
            "source_row": source_row,
        })
    if not result:
        raise ValueError("The roster has no usable bowlers.")
    return result


def assign_lanes(roster_path, lane_count, *, tournament_name="Tough Shots Tournament", tournament_id=None):
    lane_count = int(lane_count)
    if lane_count < 1:
        raise ValueError("Number of lanes must be at least 1.")

    bowlers = roster_rows(roster_path)
    if lane_count > len(bowlers):
        raise ValueError(
            f"There are {len(bowlers)} bowlers, so the number of lanes cannot exceed {len(bowlers)}."
        )

    # SystemRandom uses OS randomness and avoids deterministic division/age patterns.
    shuffled = list(bowlers)
    random.SystemRandom().shuffle(shuffled)

    lanes = {lane: [] for lane in range(1, lane_count + 1)}
    for idx, bowler in enumerate(shuffled):
        lane = (idx % lane_count) + 1
        lanes[lane].append(bowler)

    # Randomize lane traversal too, then normalize output numerically. This keeps the
    # assignment balanced while avoiding any meaningful relation to roster ordering.
    for lane_rows in lanes.values():
        random.SystemRandom().shuffle(lane_rows)

    tournament_id = tournament_id or secrets.token_urlsafe(12)
    manifest = {
        "schema_version": 1,
        "tournament_id": tournament_id,
        "tournament_name": tournament_name.strip() or "Tough Shots Tournament",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "qualifying_games": 6,
        "lane_count": lane_count,
        "lanes": [
            {
                "lane_no": lane,
                "token": secrets.token_urlsafe(24),
                "bowlers": lanes[lane],
            }
            for lane in sorted(lanes)
        ],
    }
    return manifest


def save_manifest(manifest, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "lane_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    csv_path = folder / "lane_assignments.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Lane", "Bowler_ID", "First_Name", "Last_Name", "Division"])
        for lane in manifest["lanes"]:
            for bowler in lane["bowlers"]:
                writer.writerow([
                    lane["lane_no"], bowler["bowler_id"], bowler["first_name"],
                    bowler["last_name"], bowler["division"],
                ])
    return manifest_path, csv_path


def load_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_request(url, *, method="GET", payload=None, admin_key=None, timeout=20):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if admin_key:
        headers["X-Admin-Key"] = admin_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail", detail)
        except Exception:
            pass
        raise RuntimeError(f"Cloud server returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the cloud scoring server: {exc.reason}") from exc


def normalize_base_url(base_url):
    url = (base_url or "").strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError("Cloud URL must start with https:// (or http:// for local testing).")
    return url


def publish_manifest(manifest, base_url, admin_key):
    base_url = normalize_base_url(base_url)
    if not (admin_key or "").strip():
        raise ValueError("Enter the cloud admin key.")
    payload = dict(manifest)
    payload["reset_scores"] = True
    result = _json_request(
        f"{base_url}/api/tournaments/publish",
        method="POST",
        payload=payload,
        admin_key=admin_key.strip(),
    )
    manifest["cloud_base_url"] = base_url
    manifest["published_at"] = datetime.now().isoformat(timespec="seconds")
    return result


def fetch_cloud_scores(manifest, base_url, admin_key):
    base_url = normalize_base_url(base_url)
    tournament_id = manifest["tournament_id"]
    return _json_request(
        f"{base_url}/api/tournaments/{tournament_id}/scores",
        admin_key=admin_key.strip(),
    )


def create_scoresheet_pdf(manifest, output_path, base_url):
    """Create one printable letter-size score sheet per lane."""
    try:
        import qrcode
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "Score-sheet generation needs reportlab and qrcode. Install requirements.txt."
        ) from exc

    base_url = normalize_base_url(base_url)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=letter)
    page_w, page_h = letter

    for lane_index, lane in enumerate(manifest["lanes"]):
        lane_no = lane["lane_no"]
        bowlers = lane["bowlers"]
        score_url = f"{base_url}/s/{lane['token']}"

        margin = 0.42 * inch
        qr_size = 0.92 * inch
        top = page_h - margin

        c.setFont("Helvetica-Bold", 15)
        c.drawString(margin, top - 12, manifest.get("tournament_name", "Tough Shots Tournament"))
        c.setFont("Helvetica-Bold", 24)
        c.drawCentredString(page_w / 2, top - 42, f"LANE {lane_no}")
        c.setFont("Helvetica", 8.5)
        c.drawString(margin, top - 31, "Qualifying - 6 Games")

        qr = qrcode.QRCode(version=None, box_size=8, border=2)
        qr.add_data(score_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        from reportlab.lib.utils import ImageReader
        c.drawImage(ImageReader(buf), page_w - margin - qr_size, top - qr_size, qr_size, qr_size)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawRightString(page_w - margin - qr_size - 5, top - 57, "SCAN TO ENTER SCORES")
        c.setFont("Helvetica", 6.5)
        c.drawRightString(page_w - margin - qr_size - 5, top - 69, "Mobile page is specific to this lane")

        table_top = top - 1.13 * inch
        table_left = margin
        table_right = page_w - margin
        table_width = table_right - table_left
        name_w = table_width * 0.36
        total_w = table_width * 0.11
        game_w = (table_width - name_w - total_w) / 6
        header1_h = 26
        header2_h = 24

        # Fit all bowlers on one page whenever practical.
        bottom_margin = 0.48 * inch
        available = table_top - bottom_margin - header1_h - header2_h
        row_h = min(36.0, available / max(1, len(bowlers)))
        row_h = max(15.0, row_h)
        max_rows = int(available // row_h)
        if len(bowlers) > max_rows:
            # Extremely crowded lane: reduce to a compact but still writable row.
            row_h = available / len(bowlers)

        table_bottom = table_top - header1_h - header2_h - row_h * len(bowlers)

        # Heavy outer border, modeled after the provided paper sheet.
        c.setLineWidth(2.2)
        c.rect(table_left, table_bottom, table_width, table_top - table_bottom)

        # Name and total columns span both header rows.
        x_name_end = table_left + name_w
        x_total_start = table_right - total_w

        # Horizontal header lines. The upper split only crosses the game area,
        # leaving NAME and TOTAL as tall merged cells like a paper bowling sheet.
        c.setLineWidth(1.4)
        y_header1 = table_top - header1_h
        y_header2 = y_header1 - header2_h
        c.line(x_name_end, y_header1, x_total_start, y_header1)
        c.line(table_left, y_header2, table_right, y_header2)

        c.line(x_name_end, table_top, x_name_end, table_bottom)
        c.line(x_total_start, table_top, x_total_start, table_bottom)

        # Game columns start after top merged "GAMES" cell.
        for i in range(1, 6):
            x = x_name_end + i * game_w
            c.line(x, y_header1, x, table_bottom)

        # Bowler row lines.
        for i in range(1, len(bowlers)):
            y = y_header2 - i * row_h
            c.setLineWidth(1.0)
            c.line(table_left, y, table_right, y)

        # Header labels.
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(table_left + name_w / 2, table_top - 35, "NAME")
        c.drawCentredString(x_total_start + total_w / 2, table_top - 35, "TOTAL")
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString((x_name_end + x_total_start) / 2, table_top - 18, "GAMES")
        c.setFont("Helvetica-Bold", 12)
        for game in range(1, 7):
            x = x_name_end + (game - 0.5) * game_w
            c.drawCentredString(x, y_header1 - 17, str(game))

        # Names are prefilled, leaving the six score boxes and total blank for paper backup.
        font_size = 10.5 if row_h >= 24 else 8.5
        c.setFont("Helvetica-Bold", font_size)
        for idx, bowler in enumerate(bowlers):
            y_mid = y_header2 - (idx + 0.5) * row_h
            full_name = f"{bowler['first_name']} {bowler['last_name']}"
            max_chars = 31 if font_size >= 10 else 38
            if len(full_name) > max_chars:
                full_name = full_name[: max_chars - 3] + "..."
            c.drawString(table_left + 6, y_mid - font_size * 0.35, full_name)

        c.setFont("Helvetica", 6.4)
        c.drawString(margin, 17, f"Tournament ID: {manifest['tournament_id']}  |  Lane {lane_no}")
        c.drawRightString(page_w - margin, 17, "Paper scores may be entered later from the same QR page.")

        if lane_index != len(manifest["lanes"]) - 1:
            c.showPage()

    c.save()
    return output_path
