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
    lane_rows = [
        {
            "lane_no": lane,
            "bowlers": lanes[lane],
        }
        for lane in sorted(lanes)
    ]
    lane_pairs = []
    for pair_index in range(0, len(lane_rows), 2):
        pair_lanes = lane_rows[pair_index:pair_index + 2]
        lane_pairs.append({
            "pair_no": (pair_index // 2) + 1,
            "token": secrets.token_urlsafe(24),
            "lane_nos": [lane["lane_no"] for lane in pair_lanes],
        })

    manifest = {
        "schema_version": 2,
        "tournament_id": tournament_id,
        "tournament_name": tournament_name.strip() or "Tough Shots Tournament",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "qualifying_games": 6,
        "lane_count": lane_count,
        "lanes": lane_rows,
        "lane_pairs": lane_pairs,
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


def list_scorers(base_url, admin_key):
    base_url = normalize_base_url(base_url)
    return _json_request(f"{base_url}/api/scorers", admin_key=admin_key.strip())


def _validate_scorer_pin(pin):
    pin = (pin or "").strip()
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("Scorer PIN must be exactly 6 digits.")
    return pin


def create_scorer(base_url, admin_key, name, pin):
    base_url = normalize_base_url(base_url)
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a scorer name.")
    pin = _validate_scorer_pin(pin)
    return _json_request(
        f"{base_url}/api/scorers", method="POST",
        payload={"name": name, "pin": pin}, admin_key=admin_key.strip()
    )


def reset_scorer_pin(base_url, admin_key, scorer_id, pin):
    base_url = normalize_base_url(base_url)
    pin = _validate_scorer_pin(pin)
    return _json_request(
        f"{base_url}/api/scorers/{int(scorer_id)}/reset-pin", method="POST",
        payload={"pin": pin}, admin_key=admin_key.strip()
    )


def delete_scorer(base_url, admin_key, scorer_id):
    base_url = normalize_base_url(base_url)
    return _json_request(
        f"{base_url}/api/scorers/{int(scorer_id)}", method="DELETE", admin_key=admin_key.strip()
    )


def _pair_label(lane_nos):
    if len(lane_nos) == 1:
        return f"Lane {lane_nos[0]}"
    return f"Lanes {lane_nos[0]}-{lane_nos[1]}"


def create_scoresheet_pdf(manifest, output_path, base_url):
    """Create one landscape letter score sheet per lane pair, with one QR per pair."""
    try:
        import qrcode
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise RuntimeError(
            "Score-sheet generation needs reportlab and qrcode. Install requirements.txt."
        ) from exc

    base_url = normalize_base_url(base_url)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    page_size = landscape(letter)
    c = canvas.Canvas(str(output_path), pagesize=page_size)
    page_w, page_h = page_size

    lanes_by_no = {int(lane["lane_no"]): lane for lane in manifest["lanes"]}
    pairs = manifest.get("lane_pairs") or []
    if not pairs:
        # Backward compatibility for manifests generated by the previous iteration.
        old_lanes = manifest["lanes"]
        pairs = []
        for i in range(0, len(old_lanes), 2):
            chunk = old_lanes[i:i+2]
            token = chunk[0].get("token") or secrets.token_urlsafe(24)
            pairs.append({"pair_no": i // 2 + 1, "token": token, "lane_nos": [x["lane_no"] for x in chunk]})

    for page_index, pair in enumerate(pairs):
        lane_nos = [int(x) for x in pair["lane_nos"]]
        score_url = f"{base_url}/s/{pair['token']}"
        margin = 0.30 * inch
        qr_size = 0.83 * inch
        top = page_h - margin

        # Header modeled after the compact bowling-sheet reference.
        qr = qrcode.QRCode(version=None, box_size=8, border=2)
        qr.add_data(score_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        c.drawImage(ImageReader(buf), margin, top - qr_size, qr_size, qr_size)

        info_left = margin + qr_size + 0.12 * inch
        info_right = page_w - margin
        header_h = qr_size
        c.setLineWidth(1.5)
        c.rect(info_left, top - header_h, info_right - info_left, header_h)
        split_y1 = top - header_h / 3
        split_y2 = top - 2 * header_h / 3
        c.setLineWidth(0.9)
        c.line(info_left, split_y1, info_right, split_y1)
        c.line(info_left, split_y2, info_right, split_y2)
        c.setFont("Helvetica", 9)
        c.drawString(info_left + 7, top - 16, f"Tournament: {manifest.get('tournament_name', 'Tough Shots Tournament')}")
        c.drawString(info_left + 7, split_y1 - 16, "Round: Qualifying - 6 Games")
        c.setFont("Helvetica-Bold", 10)
        c.drawString(info_left + 7, split_y2 - 16, _pair_label(lane_nos))
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(margin + qr_size / 2, top - qr_size - 9, "SCAN TO SCORE")

        table_top = top - qr_size - 0.22 * inch
        table_left = margin
        table_right = page_w - margin
        table_width = table_right - table_left
        lane_w = 0.52 * inch
        pos_w = 0.48 * inch
        name_w = 2.55 * inch
        total_w = 0.72 * inch
        game_w = (table_width - lane_w - pos_w - name_w - total_w) / 6
        header_h2 = 0.42 * inch

        rows = []
        for lane_no in lane_nos:
            lane = lanes_by_no.get(lane_no, {"bowlers": []})
            for pos, bowler in enumerate(lane.get("bowlers") or [], start=1):
                rows.append((lane_no, pos, bowler))
        # Reserve a little footer space. Grow rows for handwriting while fitting the page.
        available = table_top - 0.40 * inch - header_h2
        row_h = min(0.46 * inch, available / max(1, len(rows)))
        # Dense lane pairs (for example 13 bowlers on each lane) intentionally
        # use a compact row height like a traditional bowling recap sheet.
        row_h = max(0.17 * inch, row_h)
        table_bottom = table_top - header_h2 - row_h * len(rows)

        xs = [table_left, table_left + lane_w, table_left + lane_w + pos_w, table_left + lane_w + pos_w + name_w]
        for _ in range(6):
            xs.append(xs[-1] + game_w)
        xs.append(table_right)

        c.setLineWidth(1.8)
        c.rect(table_left, table_bottom, table_width, table_top - table_bottom)
        c.setLineWidth(0.9)
        for x in xs[1:-1]:
            c.line(x, table_bottom, x, table_top)
        c.line(table_left, table_top - header_h2, table_right, table_top - header_h2)
        for r in range(1, len(rows)):
            y = table_top - header_h2 - r * row_h
            c.line(table_left, y, table_right, y)

        # Heavier separator between the two lanes.
        first_lane_count = len(lanes_by_no.get(lane_nos[0], {}).get("bowlers", [])) if lane_nos else 0
        if len(lane_nos) == 2 and 0 < first_lane_count < len(rows):
            y = table_top - header_h2 - first_lane_count * row_h
            c.setLineWidth(2.0); c.line(table_left, y, table_right, y); c.setLineWidth(0.9)

        headers = ["Ln", "Pos.", "Competitor", "1", "2", "3", "4", "5", "6", "Total"]
        c.setFont("Helvetica-Bold", 9)
        for i, label in enumerate(headers):
            x0, x1 = xs[i], xs[i+1]
            c.drawCentredString((x0+x1)/2, table_top - header_h2/2 - 3, label)

        pos_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        name_font = 9.5 if row_h >= 0.36 * inch else (8 if row_h >= 0.25 * inch else 6.8)
        for idx, (lane_no, pos, bowler) in enumerate(rows):
            y_mid = table_top - header_h2 - (idx + 0.5) * row_h
            c.setFont("Helvetica", 9)
            c.drawCentredString((xs[0]+xs[1])/2, y_mid - 3, str(lane_no))
            c.drawCentredString((xs[1]+xs[2])/2, y_mid - 3, pos_letters[pos-1] if pos <= len(pos_letters) else str(pos))
            c.setFont("Helvetica", name_font)
            full = f"{bowler['first_name']} {bowler['last_name']}"
            max_chars = 31 if name_font >= 9 else 36
            if len(full) > max_chars: full = full[:max_chars-3] + "..."
            c.drawString(xs[2] + 5, y_mid - name_font * 0.35, full)

        c.setFont("Helvetica", 6.4)
        c.drawString(margin, 13, f"Tournament ID: {manifest['tournament_id']} | {_pair_label(lane_nos)}")
        c.drawRightString(page_w - margin, 13, "One QR opens both lanes. Scorer PIN required; submitted scores remain editable.")

        if page_index != len(pairs) - 1:
            c.showPage()

    c.save()
    return output_path
