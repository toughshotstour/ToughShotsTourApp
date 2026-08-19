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

    # Balance scorecards (lane pairs) first, then split each pair across its lanes.
    # This prevents situations such as one scorecard having 6 bowlers while another
    # has only 4 when the total field can be distributed more evenly.
    pair_lane_numbers = [list(range(start, min(start + 2, lane_count + 1))) for start in range(1, lane_count + 1, 2)]
    pair_bowlers = [[] for _ in pair_lane_numbers]
    for idx, bowler in enumerate(shuffled):
        pair_bowlers[idx % len(pair_bowlers)].append(bowler)

    lanes = {lane: [] for lane in range(1, lane_count + 1)}
    for pair_idx, lane_numbers in enumerate(pair_lane_numbers):
        rows = pair_bowlers[pair_idx]
        random.SystemRandom().shuffle(rows)
        # Alternate within each scorecard so the two lanes stay as even as possible.
        for idx, bowler in enumerate(rows):
            lane = lane_numbers[idx % len(lane_numbers)]
            lanes[lane].append(bowler)

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




def _position_label(index):
    """Return spreadsheet-style alphabet labels: 0->A, 25->Z, 26->AA."""
    index = int(index)
    if index < 0:
        raise ValueError("Position index cannot be negative.")
    label = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(ord("A") + rem) + label
    return label

def create_scoresheet_pdf(manifest, output_path, base_url):
    """Create one landscape letter score sheet per balanced lane pair, with one QR per pair."""
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
        qr_size = 0.82 * inch
        top = page_h - margin

        qr = qrcode.QRCode(version=None, box_size=8, border=2)
        qr.add_data(score_url); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        c.drawImage(ImageReader(buf), page_w - margin - qr_size, top - qr_size, qr_size, qr_size)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(page_w - margin - qr_size / 2, top - qr_size - 9, "SCAN TO SCORE")

        # Clean three-line heading requested for the qualifying sheet.
        header_left = margin
        header_right = page_w - margin - qr_size - 0.15 * inch
        c.setFont("Helvetica-Bold", 15)
        c.drawString(header_left, top - 14, "Tough Shots Tour")
        c.setFont("Helvetica-Bold", 10)
        c.drawString(header_left, top - 32, "Qualifying - 6 Games")
        c.setFont("Helvetica-Bold", 13)
        c.drawString(header_left, top - 51, _pair_label(lane_nos))
        c.setLineWidth(1.5)
        c.line(header_left, top - 60, header_right, top - 60)

        table_top = top - qr_size - 0.20 * inch
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

        # Leave room underneath the final competitor for the public-site link.
        link_space = 0.28 * inch
        available = table_top - margin - link_space - header_h2
        row_h = min(0.46 * inch, available / max(1, len(rows)))
        row_h = max(0.17 * inch, row_h)
        table_bottom = table_top - header_h2 - row_h * len(rows)

        xs = [table_left, table_left + lane_w, table_left + lane_w + pos_w, table_left + lane_w + pos_w + name_w]
        for _ in range(6): xs.append(xs[-1] + game_w)
        xs.append(table_right)

        c.setLineWidth(1.8); c.rect(table_left, table_bottom, table_width, table_top - table_bottom)
        c.setLineWidth(0.9)
        for x in xs[1:-1]: c.line(x, table_bottom, x, table_top)
        c.line(table_left, table_top - header_h2, table_right, table_top - header_h2)
        for r in range(1, len(rows)):
            y = table_top - header_h2 - r * row_h
            c.line(table_left, y, table_right, y)

        first_lane_count = len(lanes_by_no.get(lane_nos[0], {}).get("bowlers", [])) if lane_nos else 0
        if len(lane_nos) == 2 and 0 < first_lane_count < len(rows):
            y = table_top - header_h2 - first_lane_count * row_h
            c.setLineWidth(2.0); c.line(table_left, y, table_right, y); c.setLineWidth(0.9)

        headers = ["Ln", "Pos.", "Competitor", "1", "2", "3", "4", "5", "6", "Total"]
        c.setFont("Helvetica-Bold", 9)
        for i, label in enumerate(headers):
            c.drawCentredString((xs[i]+xs[i+1])/2, table_top - header_h2/2 - 3, label)

        # Position letters are assigned across the whole lane pair. If a pair has
        # N bowlers, use the first N alphabet labels. The odd lane receives the
        # first ceil(N/2) labels and the even lane receives the remaining floor(N/2).
        # Examples: 4 bowlers => odd A/B, even C/D; 7 => odd A/B/C/D, even E/F/G.
        pair_counts = {lane_no: len(lanes_by_no.get(lane_no, {}).get("bowlers", [])) for lane_no in lane_nos}
        position_offsets = {}
        running_offset = 0
        for lane_no in lane_nos:
            position_offsets[lane_no] = running_offset
            running_offset += pair_counts[lane_no]

        name_font = 9.5 if row_h >= 0.36 * inch else (8 if row_h >= 0.25 * inch else 6.8)
        for idx, (lane_no, pos, bowler) in enumerate(rows):
            y_mid = table_top - header_h2 - (idx + 0.5) * row_h
            c.setFont("Helvetica", 9)
            c.drawCentredString((xs[0]+xs[1])/2, y_mid - 3, str(lane_no))
            position = _position_label(position_offsets[lane_no] + pos - 1)
            c.drawCentredString((xs[1]+xs[2])/2, y_mid - 3, position)
            c.setFont("Helvetica", name_font)
            full = f"{bowler['first_name']} {bowler['last_name']}"
            max_chars = 31 if name_font >= 9 else 36
            if len(full) > max_chars: full = full[:max_chars-3] + "..."
            c.drawString(xs[2] + 5, y_mid - name_font * 0.35, full)

        # Public site link directly beneath the last competitor; no extra footer text.
        c.setFont("Helvetica", 7.5)
        link_y = max(margin, table_bottom - 14)
        label = f"Public standings/results: {base_url}"
        c.drawString(table_left, link_y, label)
        try:
            c.linkURL(base_url, (table_left, link_y - 2, min(table_right, table_left + 330), link_y + 9), relative=0)
        except Exception:
            pass

        if page_index != len(pairs) - 1: c.showPage()

    c.save()
    return output_path
