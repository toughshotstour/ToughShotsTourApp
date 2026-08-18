#!/usr/bin/env python3
"""Cloud/mobile scoring service for Tough Shots lane score sheets."""

from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

APP_TITLE = "Tough Shots Mobile Scoring"
ADMIN_KEY = os.environ.get("TOUGHSHOTS_ADMIN_KEY", "").strip()
DB_PATH = Path(os.environ.get("TOUGHSHOTS_CLOUD_DB", "/var/data/toughshots_cloud.sqlite3"))
if not DB_PATH.parent.exists():
    DB_PATH = Path(__file__).resolve().parent / "data" / "toughshots_cloud.sqlite3"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_TITLE)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tournaments (
            tournament_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            qualifying_games INTEGER NOT NULL DEFAULT 6,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lane_sessions (
            token TEXT PRIMARY KEY,
            tournament_id TEXT NOT NULL,
            lane_no INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(tournament_id, lane_no),
            FOREIGN KEY(tournament_id) REFERENCES tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bowlers (
            tournament_id TEXT NOT NULL,
            bowler_id TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            division TEXT,
            PRIMARY KEY(tournament_id, bowler_id),
            FOREIGN KEY(tournament_id) REFERENCES tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS assignments (
            tournament_id TEXT NOT NULL,
            lane_no INTEGER NOT NULL,
            position INTEGER NOT NULL,
            bowler_id TEXT NOT NULL,
            PRIMARY KEY(tournament_id, bowler_id),
            FOREIGN KEY(tournament_id, bowler_id) REFERENCES bowlers(tournament_id, bowler_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scores (
            tournament_id TEXT NOT NULL,
            bowler_id TEXT NOT NULL,
            game_no INTEGER NOT NULL,
            score INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tournament_id, bowler_id, game_no),
            FOREIGN KEY(tournament_id, bowler_id) REFERENCES bowlers(tournament_id, bowler_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS score_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id TEXT NOT NULL,
            lane_no INTEGER NOT NULL,
            bowler_id TEXT NOT NULL,
            game_no INTEGER NOT NULL,
            old_score INTEGER,
            new_score INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            source TEXT NOT NULL
        );
        """)


init_db()


def require_admin(x_admin_key: str | None):
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="Server admin key is not configured.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin key.")


@app.get("/health")
def health():
    return {"ok": True, "service": APP_TITLE}


@app.post("/api/tournaments/publish")
async def publish(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    tournament_id = str(payload.get("tournament_id", "")).strip()
    name = str(payload.get("tournament_name", "Tough Shots Tournament")).strip()
    lanes = payload.get("lanes") or []
    games = int(payload.get("qualifying_games", 6))
    if not tournament_id or not lanes or games != 6:
        raise HTTPException(status_code=400, detail="Tournament ID, lanes, and exactly 6 games are required.")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO tournaments(tournament_id,name,qualifying_games,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name, qualifying_games=excluded.qualifying_games, updated_at=excluded.updated_at",
            (tournament_id, name, games, now_iso()),
        )
        # Republishing is a fresh lane draw. Clear assignments and qualifying scores for safety.
        conn.execute("DELETE FROM score_audit WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM scores WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM assignments WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM bowlers WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM lane_sessions WHERE tournament_id=?", (tournament_id,))

        seen = set()
        for lane in lanes:
            lane_no = int(lane["lane_no"])
            token = str(lane["token"]).strip()
            if not token:
                raise HTTPException(status_code=400, detail=f"Lane {lane_no} has no token.")
            conn.execute(
                "INSERT INTO lane_sessions(token,tournament_id,lane_no,version) VALUES(?,?,?,1)",
                (token, tournament_id, lane_no),
            )
            for position, bowler in enumerate(lane.get("bowlers") or [], start=1):
                bid = str(bowler["bowler_id"])
                if bid in seen:
                    raise HTTPException(status_code=400, detail=f"Bowler {bid} appears on multiple lanes.")
                seen.add(bid)
                conn.execute(
                    "INSERT INTO bowlers(tournament_id,bowler_id,first_name,last_name,division) VALUES(?,?,?,?,?)",
                    (tournament_id, bid, bowler["first_name"], bowler["last_name"], bowler.get("division", "")),
                )
                conn.execute(
                    "INSERT INTO assignments(tournament_id,lane_no,position,bowler_id) VALUES(?,?,?,?)",
                    (tournament_id, lane_no, position, bid),
                )
        conn.commit()
    return {"ok": True, "tournament_id": tournament_id, "lanes": len(lanes), "bowlers": len(seen)}


@app.get("/api/tournaments/{tournament_id}/scores")
def get_scores(tournament_id: str, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM tournaments WHERE tournament_id=?", (tournament_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Tournament not found.")
        rows = conn.execute(
            """
            SELECT s.bowler_id, s.game_no, s.score, s.updated_at, a.lane_no,
                   b.first_name, b.last_name
            FROM scores s
            JOIN assignments a ON a.tournament_id=s.tournament_id AND a.bowler_id=s.bowler_id
            JOIN bowlers b ON b.tournament_id=s.tournament_id AND b.bowler_id=s.bowler_id
            WHERE s.tournament_id=?
            ORDER BY a.lane_no, a.position, s.game_no
            """,
            (tournament_id,),
        ).fetchall()
        return {"tournament_id": tournament_id, "scores": [dict(r) for r in rows]}


def lane_context(token):
    with db() as conn:
        lane = conn.execute(
            "SELECT ls.*, t.name, t.qualifying_games FROM lane_sessions ls "
            "JOIN tournaments t ON t.tournament_id=ls.tournament_id WHERE ls.token=?",
            (token,),
        ).fetchone()
        if not lane:
            return None
        bowlers = conn.execute(
            """
            SELECT b.*, a.position
            FROM assignments a JOIN bowlers b
              ON b.tournament_id=a.tournament_id AND b.bowler_id=a.bowler_id
            WHERE a.tournament_id=? AND a.lane_no=? ORDER BY a.position
            """,
            (lane["tournament_id"], lane["lane_no"]),
        ).fetchall()
        scores = conn.execute(
            "SELECT bowler_id,game_no,score FROM scores WHERE tournament_id=?",
            (lane["tournament_id"],),
        ).fetchall()
    score_map = {(r["bowler_id"], int(r["game_no"])): int(r["score"]) for r in scores}
    return lane, bowlers, score_map


CSS = """
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#132238;background:#f3f6fa}
body{margin:0}.wrap{max-width:980px;margin:auto;padding:16px}.card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 2px 12px #0001}
h1{margin:0 0 4px;font-size:24px}.sub{color:#607086;margin-bottom:14px}.notice{padding:11px;border-radius:8px;background:#edf7ee;color:#275c2d;margin-bottom:12px}.warn{background:#fff3cd;color:#6c5300}
.bowler{border:1px solid #c3ceda;border-radius:10px;padding:12px;margin:10px 0;background:#fbfcfe}.bowler-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px}.name{font-weight:800;font-size:17px}.total{font-weight:800;white-space:nowrap}
.scoregrid{display:grid;grid-template-columns:repeat(6,minmax(72px,1fr));gap:8px}.game label{display:block;font-size:12px;font-weight:700;color:#5d6c7f;margin-bottom:3px}.game input{box-sizing:border-box;width:100%;font-size:20px;padding:10px 5px;text-align:center;border:1px solid #9aa9ba;border-radius:7px}
button{background:#1769d2;color:#fff;border:0;border-radius:9px;padding:13px 18px;font-size:17px;font-weight:700;margin-top:14px;width:100%}
@media(max-width:600px){.wrap{padding:8px}.card{padding:10px;border-radius:9px}h1{font-size:21px}.scoregrid{grid-template-columns:repeat(3,1fr)}.game input{font-size:22px;padding:11px 5px}}
</style>
"""

def render_lane(token, *, notice="", warn=False, status=200):
    ctx = lane_context(token)
    if not ctx:
        return HTMLResponse("Score sheet not found.", status_code=404)
    lane, bowlers, score_map = ctx
    cards = []
    for b in bowlers:
        fields = []
        total = 0
        complete = True
        for game in range(1, 7):
            value = score_map.get((b["bowler_id"], game))
            if value is None:
                complete = False
            else:
                total += value
            val = "" if value is None else str(value)
            field = f"score__{b['bowler_id']}__{game}"
            fields.append(
                f'<div class="game"><label>Game {game}</label><input aria-label="{html.escape(b["first_name"])} game {game}" type="number" inputmode="numeric" min="0" max="300" name="{field}" value="{val}"></div>'
            )
        total_text = str(total) if complete else "-"
        cards.append(
            f'<section class="bowler"><div class="bowler-head"><div class="name">{html.escape(b["first_name"] + " " + b["last_name"])}</div><div class="total">Total: <span>{total_text}</span></div></div><div class="scoregrid">'
            + "".join(fields) + "</div></section>"
        )
    notice_html = ""
    if notice:
        notice_html = f'<div class="notice {"warn" if warn else ""}">{html.escape(notice)}</div>'
    script = """
<script>
document.querySelectorAll('.bowler').forEach(function(card){
  const inputs=[...card.querySelectorAll('input[type=number]')];
  const total=card.querySelector('.total span');
  function update(){
    const vals=inputs.map(i=>i.value.trim());
    if(vals.every(v=>v!=='')){ total.textContent=vals.reduce((a,v)=>a+Number(v),0); }
    else { total.textContent='-'; }
  }
  inputs.forEach(i=>i.addEventListener('input',update)); update();
});
</script>
"""
    page = f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">{CSS}<title>Lane {lane['lane_no']} Scores</title></head>
<body><div class="wrap"><div class="card"><h1>{html.escape(lane['name'])} - Lane {lane['lane_no']}</h1><div class="sub">Enter qualifying scores for Games 1-6. Scores must be 0-300.</div>{notice_html}
<form method="post" action="/s/{html.escape(token)}" onsubmit="return confirm('Submit these lane scores?')"><input type="hidden" name="version" value="{lane['version']}">{''.join(cards)}<button type="submit">Submit Scores</button></form></div></div>{script}</body></html>"""
    return HTMLResponse(page, status_code=status)


@app.get("/s/{token}", response_class=HTMLResponse)
def score_page(token: str):
    return render_lane(token)


@app.post("/s/{token}", response_class=HTMLResponse)
async def submit_scores(token: str, request: Request):
    ctx = lane_context(token)
    if not ctx:
        return HTMLResponse("Score sheet not found.", status_code=404)
    lane, bowlers, _ = ctx
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
    try:
        submitted_version = int(form.get("version", "0"))
    except ValueError:
        submitted_version = 0
    if submitted_version != int(lane["version"]):
        return render_lane(token, notice="Another device changed this lane after you opened it. Reloaded the newest scores; please review and submit again.", warn=True, status=409)

    allowed = {b["bowler_id"] for b in bowlers}
    changes = []
    for key, raw_value in form.items():
        if not key.startswith("score__") or not raw_value.strip():
            continue
        try:
            _, bowler_id, game_text = key.split("__", 2)
            game_no = int(game_text)
            score = int(raw_value)
        except Exception:
            return render_lane(token, notice="One of the score fields was invalid. Please check the entries.", warn=True, status=400)
        if bowler_id not in allowed or game_no not in range(1, 7) or not 0 <= score <= 300:
            return render_lane(token, notice="Scores must be 0-300 and belong to this lane.", warn=True, status=400)
        changes.append((bowler_id, game_no, score))

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version FROM lane_sessions WHERE token=?", (token,)).fetchone()
        if not current or int(current["version"]) != submitted_version:
            conn.rollback()
            return render_lane(token, notice="Another device submitted first. Reloaded the latest scores; please review and submit again.", warn=True, status=409)
        changed_count = 0
        for bowler_id, game_no, score in changes:
            old = conn.execute(
                "SELECT score FROM scores WHERE tournament_id=? AND bowler_id=? AND game_no=?",
                (lane["tournament_id"], bowler_id, game_no),
            ).fetchone()
            old_score = None if old is None else int(old["score"])
            if old_score == score:
                continue
            conn.execute(
                "INSERT INTO scores(tournament_id,bowler_id,game_no,score,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tournament_id,bowler_id,game_no) DO UPDATE SET score=excluded.score,updated_at=excluded.updated_at",
                (lane["tournament_id"], bowler_id, game_no, score, now_iso()),
            )
            conn.execute(
                "INSERT INTO score_audit(tournament_id,lane_no,bowler_id,game_no,old_score,new_score,submitted_at,source) VALUES(?,?,?,?,?,?,?,?)",
                (lane["tournament_id"], lane["lane_no"], bowler_id, game_no, old_score, score, now_iso(), "mobile_qr"),
            )
            changed_count += 1
        if changed_count:
            conn.execute("UPDATE lane_sessions SET version=version+1 WHERE token=?", (token,))
        conn.commit()

    return render_lane(token, notice=f"Saved {changed_count} score change{'s' if changed_count != 1 else ''} successfully.")
