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


def proper_name(value):
    """Normalize ordinary all-upper/all-lower names while preserving intentional mixed case."""
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    # Preserve mixed-case spellings such as McDonald/deVries exactly as entered.
    # Normalize the common CSV-export cases of ALL CAPS or all lowercase.
    if text.isupper() or text.islower():
        return text.title()
    return text


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
        first = proper_name(row.get("First_Name"))
        last = proper_name(row.get("Last_Name"))
        division = (row.get("Division") or "").strip()
        birthdate = (row.get("Birthdate_Used") or "").strip()
        if not first or not last or not division:
            continue
        # Prefer the permanent Bowler ID carried forward from the master database.
        # Fall back to the historical roster hash for older files.
        bowler_id = (row.get("Bowler_ID") or row.get("BowlerID") or "").strip()
        if not bowler_id:
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


def _load_group_assignments(group_assignments):
    """Normalize an optional bowler_id -> group_id mapping."""
    if not group_assignments:
        return {}
    if isinstance(group_assignments, (str, Path)):
        path = Path(group_assignments)
        if not path.is_file():
            return {}
        group_assignments = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v).strip() for k, v in dict(group_assignments).items() if str(v).strip()}


def _balanced_pair_capacities(total_bowlers, pair_count, division_counts):
    """Return low/high pair capacities arranged to favor division boundaries.

    Pair totals still differ by at most one.  When there is a choice about which
    scorecards receive the extra bowler, prefer boundaries that land exactly
    between divisions so fewer scorecards need to mix divisions.
    """
    low = total_bowlers // pair_count
    high_count = total_bowlers % pair_count
    high = low + (1 if high_count else 0)
    if high_count == 0:
        return [low] * pair_count

    division_boundaries = set()
    running = 0
    for count in division_counts[:-1]:
        running += count
        division_boundaries.add(running)

    # DP state: (pairs_used, highs_used) -> (score, capacities).  Reward exact
    # division boundaries heavily; a small proximity reward makes the fallback
    # arrangement stable and keeps boundaries close even when an exact hit is
    # impossible.
    states = {(0, 0): (0, [])}
    for used in range(pair_count):
        nxt = {}
        for (pairs_used, highs_used), (score, caps) in states.items():
            for cap, add_high in ((low, 0), (high, 1)):
                nh = highs_used + add_high
                if nh > high_count:
                    continue
                remaining_pairs = pair_count - (pairs_used + 1)
                remaining_highs = high_count - nh
                if remaining_highs > remaining_pairs:
                    continue
                new_caps = caps + [cap]
                boundary = sum(new_caps)
                boundary_score = 0
                if pairs_used + 1 < pair_count:
                    if boundary in division_boundaries:
                        boundary_score = 1000
                    elif division_boundaries:
                        boundary_score = max(0, 20 - min(abs(boundary - b) for b in division_boundaries))
                key = (pairs_used + 1, nh)
                candidate = (score + boundary_score, new_caps)
                if key not in nxt or candidate[0] > nxt[key][0]:
                    nxt[key] = candidate
        states = nxt
    return states[(pair_count, high_count)][1]


def _assign_ungrouped_by_division(bowlers, pair_count, rng):
    """Balance scorecards exactly while keeping division blocks contiguous.

    This is the normal/default lane-assignment path. Bowlers are randomized
    *within* their division, divisions remain contiguous in the assignment, and
    only scorecards that straddle a division boundary are mixed.
    """
    division_order = []
    by_division = {}
    for bowler in bowlers:
        div = bowler["division"]
        if div not in by_division:
            division_order.append(div)
            by_division[div] = []
        by_division[div].append(bowler)
    for div in division_order:
        rng.shuffle(by_division[div])

    capacities = _balanced_pair_capacities(
        len(bowlers), pair_count, [len(by_division[d]) for d in division_order]
    )
    ordered = []
    for div in division_order:
        ordered.extend(by_division[div])

    pairs = []
    cursor = 0
    for capacity in capacities:
        members = ordered[cursor:cursor + capacity]
        cursor += capacity
        pairs.append({"members": members, "load": len(members), "bundles": [],
                      "divisions": list(dict.fromkeys(b["division"] for b in members))})
    return pairs


def assign_lanes(roster_path, lane_count, *, tournament_name="Tough Shots Tournament", tournament_id=None, group_assignments=None):
    """Create balanced lane-pair assignments with optional same-pair groups.

    Priority order:
      1. Keep any explicit group ID together on one lane pair.
      2. Keep pair sizes as even as those groups allow.
      3. By default, keep bowlers from the same division together and on
         neighboring scorecards; mix divisions only when balance/group
         constraints make that necessary.
    """
    lane_count = int(lane_count)
    if lane_count < 1:
        raise ValueError("Number of lanes must be at least 1.")

    bowlers = roster_rows(roster_path)
    if lane_count > len(bowlers):
        raise ValueError(
            f"There are {len(bowlers)} bowlers, so the number of lanes cannot exceed {len(bowlers)}."
        )

    pair_lane_numbers = [list(range(start, min(start + 2, lane_count + 1))) for start in range(1, lane_count + 1, 2)]
    pair_count = len(pair_lane_numbers)
    target_low = len(bowlers) // pair_count
    target_high = (len(bowlers) + pair_count - 1) // pair_count

    group_map = _load_group_assignments(group_assignments)
    rng = random.SystemRandom()

    if not group_map:
        # Normal path: exact global balance first, with each division laid out as
        # one contiguous block. This keeps division-mates together by default and
        # mixes only at the few boundaries required by the balanced pair sizes.
        pairs = _assign_ungrouped_by_division(bowlers, pair_count, rng)
    else:
        # Explicit lane groups are atomic. They take precedence over the normal
        # division-block layout, but the grouped fallback still prefers division
        # proximity and then repairs pair-size spread where possible.
        bundles_by_key = {}
        for bowler in bowlers:
            gid = group_map.get(str(bowler["bowler_id"]), "")
            key = f"group:{gid}" if gid else f"single:{bowler['bowler_id']}"
            bundles_by_key.setdefault(key, []).append(bowler)

        bundles = []
        for key, members in bundles_by_key.items():
            rng.shuffle(members)
            divisions = []
            for b in members:
                if b["division"] not in divisions:
                    divisions.append(b["division"])
            bundles.append({
                "key": key,
                "group_id": key.split(":", 1)[1] if key.startswith("group:") else "",
                "members": members,
                "size": len(members),
                "divisions": divisions,
            })

        max_group = max(b["size"] for b in bundles)
        if max_group > target_high:
            target_high = max_group

        rng.shuffle(bundles)
        bundles.sort(key=lambda b: (-b["size"], b["divisions"][0] if b["divisions"] else ""))

        pairs = [{"bundles": [], "load": 0, "divisions": []} for _ in range(pair_count)]
        for bundle in bundles:
            best_idx = None
            best_score = None
            for idx, pair in enumerate(pairs):
                projected = pair["load"] + bundle["size"]
                overflow = max(0, projected - target_high)
                shared = any(d in pair["divisions"] for d in bundle["divisions"])
                division_penalty = 0 if shared or not pair["divisions"] else 1
                distance = abs(projected - target_low)
                score = (overflow, division_penalty, pair["load"], distance, idx)
                if best_score is None or score < best_score:
                    best_score, best_idx = score, idx
            pair = pairs[best_idx]
            pair["bundles"].append(bundle)
            pair["load"] += bundle["size"]
            for d in bundle["divisions"]:
                if d not in pair["divisions"]:
                    pair["divisions"].append(d)

        improved = True
        while improved:
            improved = False
            loads = [p["load"] for p in pairs]
            current_spread = max(loads) - min(loads)
            for src_i in sorted(range(pair_count), key=lambda i: pairs[i]["load"], reverse=True):
                for dst_i in sorted(range(pair_count), key=lambda i: pairs[i]["load"]):
                    if src_i == dst_i:
                        continue
                    for bidx, bundle in enumerate(list(pairs[src_i]["bundles"])):
                        new_loads = list(loads)
                        new_loads[src_i] -= bundle["size"]
                        new_loads[dst_i] += bundle["size"]
                        spread = max(new_loads) - min(new_loads)
                        if spread < current_spread:
                            pairs[src_i]["bundles"].pop(bidx)
                            pairs[dst_i]["bundles"].append(bundle)
                            pairs[src_i]["load"] -= bundle["size"]
                            pairs[dst_i]["load"] += bundle["size"]
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break

    lanes = {lane: [] for lane in range(1, lane_count + 1)}
    pair_divisions = {}
    pair_group_ids = {}
    for pair_index, (lane_numbers, pair) in enumerate(zip(pair_lane_numbers, pairs)):
        # Keep members of the same division adjacent on the printed scorecard.
        if "members" in pair:
            members = list(pair["members"])
            group_ids = []
        else:
            members = []
            for bundle in pair["bundles"]:
                members.extend(bundle["members"])
            # Keep division blocks together even in the explicit-group fallback.
            division_rank = {}
            for b in members:
                division_rank.setdefault(b["division"], len(division_rank))
            members.sort(key=lambda b: division_rank[b["division"]])
            group_ids = sorted({bundle["group_id"] for bundle in pair["bundles"] if bundle["group_id"]})

        pair_divisions[pair_index] = " / ".join(dict.fromkeys(b["division"] for b in members))
        pair_group_ids[pair_index] = group_ids
        if len(lane_numbers) == 1:
            lanes[lane_numbers[0]].extend(members)
        else:
            odd_count = (len(members) + 1) // 2
            lanes[lane_numbers[0]].extend(members[:odd_count])
            lanes[lane_numbers[1]].extend(members[odd_count:])

    tournament_id = tournament_id or secrets.token_urlsafe(12)
    lane_rows = [{"lane_no": lane, "bowlers": lanes[lane]} for lane in sorted(lanes)]
    lane_pairs = []
    for pair_index, lane_numbers in enumerate(pair_lane_numbers):
        lane_pairs.append({
            "pair_no": pair_index + 1,
            "token": secrets.token_urlsafe(24),
            "lane_nos": lane_numbers,
            "division": pair_divisions.get(pair_index, ""),
            "group_ids": pair_group_ids.get(pair_index, []),
        })

    return {
        "schema_version": 4,
        "tournament_id": tournament_id,
        "tournament_name": tournament_name.strip() or "Tough Shots Tournament",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "qualifying_games": 6,
        "lane_count": lane_count,
        "assignment_method": "balanced_pairs_division_clustered_with_optional_groups",
        "lanes": lane_rows,
        "lane_pairs": lane_pairs,
    }


def move_bowler_to_lane(manifest, bowler_id, target_lane):
    """Move one bowler to a specific lane in an existing manifest."""
    target_lane = int(target_lane)
    lanes = {int(x["lane_no"]): x for x in manifest.get("lanes", [])}
    if target_lane not in lanes:
        raise ValueError(f"Lane {target_lane} is not part of this assignment.")
    found = None
    for lane in lanes.values():
        for i, bowler in enumerate(lane.get("bowlers", [])):
            if str(bowler.get("bowler_id")) == str(bowler_id):
                found = lane["bowlers"].pop(i)
                break
        if found:
            break
    if not found:
        raise ValueError("Bowler was not found in this lane assignment.")
    lanes[target_lane].setdefault("bowlers", []).append(found)
    manifest["edited_at"] = datetime.now().isoformat(timespec="seconds")
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


def publish_manifest(manifest, base_url, admin_key, *, reset_scores=True):
    base_url = normalize_base_url(base_url)
    if not (admin_key or "").strip():
        raise ValueError("Enter the cloud admin key.")
    payload = dict(manifest)
    payload["reset_scores"] = bool(reset_scores)
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

def create_scoresheet_pdf(manifest, output_path, base_url, print_title=""):
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

        # Shared event print title, followed by the compact Tough Shots heading.
        header_left = margin
        header_right = page_w - margin - qr_size - 0.15 * inch
        title_offset = 0
        clean_title = (print_title or "").strip()
        if clean_title:
            c.setFont("Helvetica-Bold", 16)
            c.drawCentredString((header_left + header_right) / 2, top - 14, clean_title)
            title_offset = 20
        c.setFont("Helvetica-Bold", 15)
        c.drawString(header_left, top - 14 - title_offset, "Tough Shots Tour")
        c.setFont("Helvetica-Bold", 10)
        c.drawString(header_left, top - 32 - title_offset, "Qualifying - 6 Games")
        c.setFont("Helvetica-Bold", 13)
        c.drawString(header_left, top - 51 - title_offset, _pair_label(lane_nos))
        c.setLineWidth(1.5)
        c.line(header_left, top - 60 - title_offset, header_right, top - 60 - title_offset)

        table_top = top - qr_size - 0.20 * inch - title_offset
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
        c.setFont("Helvetica", name_font)
        link_y = max(margin, table_bottom - 14)
        label = f"Public standings/results: {base_url}"
        c.drawString(table_left, link_y, label)
        try:
            c.linkURL(base_url, (table_left, link_y - 2, table_right, link_y + max(10, name_font + 2)), relative=0)
        except Exception:
            pass

        if page_index != len(pairs) - 1: c.showPage()

    c.save()
    return output_path
