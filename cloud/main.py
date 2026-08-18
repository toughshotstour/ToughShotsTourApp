#!/usr/bin/env python3
"""Cloud/mobile scoring service for Tough Shots lane-pair score sheets."""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

APP_TITLE = "Tough Shots Mobile Scoring"
ADMIN_KEY = os.environ.get("TOUGHSHOTS_ADMIN_KEY", "").strip()
DB_PATH = Path(os.environ.get("TOUGHSHOTS_CLOUD_DB", "/var/data/toughshots_cloud.sqlite3"))
if not DB_PATH.parent.exists():
    DB_PATH = Path(__file__).resolve().parent / "data" / "toughshots_cloud.sqlite3"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SESSION_HOURS = int(os.environ.get("TOUGHSHOTS_SCORER_SESSION_HOURS", "12"))

app = FastAPI(title=APP_TITLE)
_login_failures: dict[str, list[float]] = {}


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
        CREATE TABLE IF NOT EXISTS lane_pair_sessions (
            token TEXT PRIMARY KEY,
            tournament_id TEXT NOT NULL,
            pair_no INTEGER NOT NULL,
            lane_a INTEGER NOT NULL,
            lane_b INTEGER,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(tournament_id, pair_no),
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
        CREATE TABLE IF NOT EXISTS scorers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pin_salt TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scorer_sessions (
            token TEXT PRIMARY KEY,
            scorer_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(scorer_id) REFERENCES scorers(id) ON DELETE CASCADE
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
            source TEXT NOT NULL,
            scorer_id INTEGER,
            scorer_name TEXT
        );
        """)
        # Safe migration from earlier cloud DBs.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(score_audit)")}
        if "scorer_id" not in cols:
            conn.execute("ALTER TABLE score_audit ADD COLUMN scorer_id INTEGER")
        if "scorer_name" not in cols:
            conn.execute("ALTER TABLE score_audit ADD COLUMN scorer_name TEXT")


init_db()


def require_admin(x_admin_key: str | None):
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="Server admin key is not configured.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin key.")


def pin_digest(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 180_000).hex()


def new_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def scorer_from_request(request: Request):
    token = request.cookies.get("toughshots_scorer")
    if not token:
        return None
    now = now_iso()
    with db() as conn:
        row = conn.execute(
            "SELECT s.id,s.name,ss.expires_at FROM scorer_sessions ss JOIN scorers s ON s.id=ss.scorer_id "
            "WHERE ss.token=? AND s.active=1 AND ss.expires_at>?",
            (token, now),
        ).fetchone()
    return row


def login_key(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return ip


def check_login_rate(request: Request):
    key = login_key(request)
    cutoff = time.time() - 600
    attempts = [t for t in _login_failures.get(key, []) if t >= cutoff]
    _login_failures[key] = attempts
    if len(attempts) >= 8:
        raise HTTPException(status_code=429, detail="Too many incorrect PIN attempts. Try again in about 10 minutes.")


def record_login_failure(request: Request):
    _login_failures.setdefault(login_key(request), []).append(time.time())


@app.get("/health")
def health():
    return {"ok": True, "service": APP_TITLE}


@app.get("/api/scorers")
def api_list_scorers(x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    with db() as conn:
        rows = conn.execute("SELECT id,name,active,created_at FROM scorers ORDER BY name COLLATE NOCASE").fetchall()
    return {"scorers": [dict(r) for r in rows]}


@app.post("/api/scorers")
async def api_create_scorer(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Scorer name is required.")
    pin = new_pin(); salt = secrets.token_hex(16); digest = pin_digest(pin, salt)
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO scorers(name,pin_salt,pin_hash,active,created_at) VALUES(?,?,?,?,?)",
                (name, salt, digest, 1, now_iso()),
            )
            scorer_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A scorer with that name already exists.")
    return {"ok": True, "scorer": {"id": scorer_id, "name": name, "pin": pin}}


@app.post("/api/scorers/{scorer_id}/reset-pin")
def api_reset_pin(scorer_id: int, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    pin = new_pin(); salt = secrets.token_hex(16); digest = pin_digest(pin, salt)
    with db() as conn:
        row = conn.execute("SELECT name FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Scorer not found.")
        conn.execute("UPDATE scorers SET pin_salt=?,pin_hash=?,active=1 WHERE id=?", (salt, digest, scorer_id))
        conn.execute("DELETE FROM scorer_sessions WHERE scorer_id=?", (scorer_id,))
    return {"ok": True, "scorer": {"id": scorer_id, "name": row["name"], "pin": pin}}


@app.delete("/api/scorers/{scorer_id}")
def api_delete_scorer(scorer_id: int, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    with db() as conn:
        row = conn.execute("SELECT name FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Scorer not found.")
        conn.execute("DELETE FROM scorer_sessions WHERE scorer_id=?", (scorer_id,))
        conn.execute("DELETE FROM scorers WHERE id=?", (scorer_id,))
    return {"ok": True, "deleted": scorer_id}


@app.post("/api/tournaments/publish")
async def publish(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    tournament_id = str(payload.get("tournament_id", "")).strip()
    name = str(payload.get("tournament_name", "Tough Shots Tournament")).strip()
    lanes = payload.get("lanes") or []
    pairs = payload.get("lane_pairs") or []
    games = int(payload.get("qualifying_games", 6))
    if not tournament_id or not lanes or games != 6:
        raise HTTPException(status_code=400, detail="Tournament ID, lanes, and exactly 6 games are required.")
    if not pairs:
        # Allow one transition publish from an older desktop build by pairing consecutive lanes.
        pairs = []
        for i in range(0, len(lanes), 2):
            chunk = lanes[i:i+2]
            pairs.append({"pair_no": i//2 + 1, "token": chunk[0].get("token") or secrets.token_urlsafe(24), "lane_nos": [int(x["lane_no"]) for x in chunk]})

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO tournaments(tournament_id,name,qualifying_games,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name, qualifying_games=excluded.qualifying_games, updated_at=excluded.updated_at",
            (tournament_id, name, games, now_iso()),
        )
        conn.execute("DELETE FROM score_audit WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM scores WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM assignments WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM bowlers WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM lane_pair_sessions WHERE tournament_id=?", (tournament_id,))

        seen = set()
        for lane in lanes:
            lane_no = int(lane["lane_no"])
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
        valid_lanes = {int(x["lane_no"]) for x in lanes}
        for pair in pairs:
            lane_nos = [int(x) for x in pair.get("lane_nos") or []]
            if not 1 <= len(lane_nos) <= 2 or any(x not in valid_lanes for x in lane_nos):
                raise HTTPException(status_code=400, detail="Each lane pair must contain one or two published lanes.")
            token = str(pair.get("token", "")).strip()
            if not token:
                raise HTTPException(status_code=400, detail="Lane pair has no QR token.")
            conn.execute(
                "INSERT INTO lane_pair_sessions(token,tournament_id,pair_no,lane_a,lane_b,version) VALUES(?,?,?,?,?,1)",
                (token, tournament_id, int(pair["pair_no"]), lane_nos[0], lane_nos[1] if len(lane_nos)>1 else None),
            )
        conn.commit()
    return {"ok": True, "tournament_id": tournament_id, "lanes": len(lanes), "pairs": len(pairs), "bowlers": len(seen)}


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
            WHERE s.tournament_id=? ORDER BY a.lane_no, a.position, s.game_no
            """, (tournament_id,),
        ).fetchall()
    return {"tournament_id": tournament_id, "scores": [dict(r) for r in rows]}


def pair_context(token):
    with db() as conn:
        pair = conn.execute(
            "SELECT ps.*,t.name,t.qualifying_games FROM lane_pair_sessions ps JOIN tournaments t ON t.tournament_id=ps.tournament_id WHERE ps.token=?",
            (token,),
        ).fetchone()
        if not pair:
            return None
        lane_nos = [int(pair["lane_a"])] + ([int(pair["lane_b"])] if pair["lane_b"] is not None else [])
        qmarks = ",".join("?" for _ in lane_nos)
        bowlers = conn.execute(
            f"""SELECT b.*,a.position,a.lane_no FROM assignments a JOIN bowlers b
            ON b.tournament_id=a.tournament_id AND b.bowler_id=a.bowler_id
            WHERE a.tournament_id=? AND a.lane_no IN ({qmarks}) ORDER BY a.lane_no,a.position""",
            (pair["tournament_id"], *lane_nos),
        ).fetchall()
        scores = conn.execute("SELECT bowler_id,game_no,score FROM scores WHERE tournament_id=?", (pair["tournament_id"],)).fetchall()
    score_map = {(r["bowler_id"], int(r["game_no"])): int(r["score"]) for r in scores}
    return pair, lane_nos, bowlers, score_map


CSS = """
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#132238;background:#f3f6fa}
body{margin:0}.wrap{max-width:1040px;margin:auto;padding:16px}.card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 2px 12px #0001}
h1{margin:0 0 4px;font-size:24px}.sub{color:#607086;margin-bottom:14px}.notice{padding:11px;border-radius:8px;background:#edf7ee;color:#275c2d;margin-bottom:12px}.warn{background:#fff3cd;color:#6c5300}
.login{max-width:420px;margin:12vh auto}.login input{box-sizing:border-box;width:100%;font-size:24px;letter-spacing:5px;text-align:center;padding:12px;border:1px solid #9aa9ba;border-radius:8px}
.lane-title{font-size:20px;font-weight:900;margin:18px 0 7px;border-bottom:2px solid #26384d;padding-bottom:5px}.bowler{border:1px solid #c3ceda;border-radius:10px;padding:12px;margin:10px 0;background:#fbfcfe}.bowler-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px}.name{font-weight:800;font-size:17px}.total{font-weight:800;white-space:nowrap}
.scoregrid{display:grid;grid-template-columns:repeat(6,minmax(72px,1fr));gap:8px}.game label{display:block;font-size:12px;font-weight:700;color:#5d6c7f;margin-bottom:3px}.game input{box-sizing:border-box;width:100%;font-size:20px;padding:10px 5px;text-align:center;border:1px solid #9aa9ba;border-radius:7px}
button{background:#1769d2;color:#fff;border:0;border-radius:9px;padding:13px 18px;font-size:17px;font-weight:700;margin-top:14px;width:100%}.who{font-size:13px;color:#52677f;margin-bottom:10px}.logout{float:right;font-size:13px;color:#1769d2;text-decoration:none}
@media(max-width:600px){.wrap{padding:8px}.card{padding:10px;border-radius:9px}h1{font-size:21px}.scoregrid{grid-template-columns:repeat(3,1fr)}.game input{font-size:22px;padding:11px 5px}}
</style>
"""


def render_login(token, message="", status=200):
    ctx = pair_context(token)
    if not ctx:
        return HTMLResponse("Score sheet not found.", status_code=404)
    pair, lane_nos, _, _ = ctx
    label = f"Lane {lane_nos[0]}" if len(lane_nos)==1 else f"Lanes {lane_nos[0]}-{lane_nos[1]}"
    msg = f'<div class="notice warn">{html.escape(message)}</div>' if message else ""
    return HTMLResponse(f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">{CSS}<title>{label}</title></head><body><div class="wrap"><div class="card login"><h1>{html.escape(pair['name'])}</h1><div class="sub">{label} - enter your scorer PIN to continue.</div>{msg}<form method="post" action="/s/{html.escape(token)}/login"><input name="pin" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" autofocus><button type="submit">Continue</button></form></div></div></body></html>''', status_code=status)


def render_pair(token, scorer, *, notice="", warn=False, status=200):
    ctx = pair_context(token)
    if not ctx:
        return HTMLResponse("Score sheet not found.", status_code=404)
    pair, lane_nos, bowlers, score_map = ctx
    sections = []
    for lane_no in lane_nos:
        cards = []
        lane_bowlers = [b for b in bowlers if int(b["lane_no"]) == lane_no]
        for b in lane_bowlers:
            fields=[]; total=0; complete=True
            for game in range(1,7):
                value=score_map.get((b["bowler_id"],game)); complete &= value is not None
                if value is not None: total += value
                val="" if value is None else str(value)
                field=f"score__{b['bowler_id']}__{game}"
                fields.append(f'<div class="game"><label>Game {game}</label><input aria-label="{html.escape(b["first_name"])} game {game}" type="number" inputmode="numeric" min="0" max="300" name="{field}" value="{val}"></div>')
            cards.append(f'<section class="bowler"><div class="bowler-head"><div class="name">{html.escape(b["first_name"]+" "+b["last_name"])}</div><div class="total">Total: <span>{total if complete else "-"}</span></div></div><div class="scoregrid">{"".join(fields)}</div></section>')
        sections.append(f'<div class="lane-title">Lane {lane_no}</div>{"".join(cards)}')
    label=f"Lane {lane_nos[0]}" if len(lane_nos)==1 else f"Lanes {lane_nos[0]}-{lane_nos[1]}"
    notice_html=f'<div class="notice {"warn" if warn else ""}">{html.escape(notice)}</div>' if notice else ""
    script="""<script>document.querySelectorAll('.bowler').forEach(function(card){const inputs=[...card.querySelectorAll('input[type=number]')],total=card.querySelector('.total span');function update(){const vals=inputs.map(i=>i.value.trim());total.textContent=vals.every(v=>v!=='')?vals.reduce((a,v)=>a+Number(v),0):'-'}inputs.forEach(i=>i.addEventListener('input',update));update();});</script>"""
    page=f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">{CSS}<title>{label} Scores</title></head><body><div class="wrap"><div class="card"><a class="logout" href="/logout?next=/s/{html.escape(token)}">Sign out</a><h1>{html.escape(pair['name'])} - {label}</h1><div class="who">Scorer: <strong>{html.escape(scorer['name'])}</strong> &nbsp;|&nbsp; Scores remain editable after saving.</div>{notice_html}<form method="post" action="/s/{html.escape(token)}" onsubmit="return confirm('Save these scores?')"><input type="hidden" name="version" value="{pair['version']}">{''.join(sections)}<button type="submit">Save Scores</button></form></div></div>{script}</body></html>'''
    return HTMLResponse(page,status_code=status)


@app.get("/s/{token}", response_class=HTMLResponse)
def score_page(token: str, request: Request):
    scorer=scorer_from_request(request)
    return render_pair(token, scorer) if scorer else render_login(token)


@app.post("/s/{token}/login")
async def scorer_login(token: str, request: Request):
    if not pair_context(token):
        return HTMLResponse("Score sheet not found.", status_code=404)
    check_login_rate(request)
    raw=(await request.body()).decode("utf-8",errors="replace")
    form={k:v[-1] for k,v in parse_qs(raw,keep_blank_values=True).items()}
    pin=form.get("pin","").strip()
    with db() as conn:
        rows=conn.execute("SELECT id,name,pin_salt,pin_hash FROM scorers WHERE active=1").fetchall()
        matched=None
        for r in rows:
            if secrets.compare_digest(pin_digest(pin,r["pin_salt"]),r["pin_hash"]): matched=r; break
        if not matched:
            record_login_failure(request)
            return render_login(token,"Incorrect scorer PIN.",status=401)
        session_token=secrets.token_urlsafe(32)
        created=datetime.now(timezone.utc); expires=created+timedelta(hours=SESSION_HOURS)
        conn.execute("DELETE FROM scorer_sessions WHERE expires_at<=?",(now_iso(),))
        conn.execute("INSERT INTO scorer_sessions(token,scorer_id,created_at,expires_at) VALUES(?,?,?,?)",(session_token,matched["id"],created.isoformat(timespec="seconds"),expires.isoformat(timespec="seconds")))
    response=RedirectResponse(url=f"/s/{token}",status_code=303)
    response.set_cookie("toughshots_scorer",session_token,max_age=SESSION_HOURS*3600,httponly=True,samesite="lax",secure=(request.url.scheme=="https"))
    return response


@app.get("/logout")
def logout(request: Request, next: str = "/"):
    token=request.cookies.get("toughshots_scorer")
    if token:
        with db() as conn: conn.execute("DELETE FROM scorer_sessions WHERE token=?",(token,))
    target=next if next.startswith("/s/") else "/"
    response=RedirectResponse(url=target,status_code=303); response.delete_cookie("toughshots_scorer"); return response


@app.post("/s/{token}", response_class=HTMLResponse)
async def submit_scores(token: str, request: Request):
    scorer=scorer_from_request(request)
    if not scorer:
        return render_login(token,"Your scorer session expired. Enter your PIN again.",status=401)
    ctx=pair_context(token)
    if not ctx: return HTMLResponse("Score sheet not found.",status_code=404)
    pair,lane_nos,bowlers,_=ctx
    raw=(await request.body()).decode("utf-8",errors="replace")
    form={k:v[-1] for k,v in parse_qs(raw,keep_blank_values=True).items()}
    try: submitted_version=int(form.get("version","0"))
    except ValueError: submitted_version=0
    if submitted_version != int(pair["version"]):
        return render_pair(token,scorer,notice="Another device changed this lane pair after you opened it. The newest scores are shown; review them and save again.",warn=True,status=409)
    allowed={b["bowler_id"]:int(b["lane_no"]) for b in bowlers}; changes=[]
    for key,raw_value in form.items():
        if not key.startswith("score__") or not raw_value.strip(): continue
        try:
            _,bowler_id,game_text=key.split("__",2); game_no=int(game_text); score=int(raw_value)
        except Exception:
            return render_pair(token,scorer,notice="One of the score fields was invalid.",warn=True,status=400)
        if bowler_id not in allowed or game_no not in range(1,7) or not 0<=score<=300:
            return render_pair(token,scorer,notice="Scores must be 0-300 and belong to this lane pair.",warn=True,status=400)
        changes.append((bowler_id,game_no,score,allowed[bowler_id]))
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current=conn.execute("SELECT version FROM lane_pair_sessions WHERE token=?",(token,)).fetchone()
        if not current or int(current["version"]) != submitted_version:
            conn.rollback(); return render_pair(token,scorer,notice="Another device saved this lane pair first. The latest values are shown; review and save again.",warn=True,status=409)
        changed_count=0
        for bowler_id,game_no,score,lane_no in changes:
            old=conn.execute("SELECT score FROM scores WHERE tournament_id=? AND bowler_id=? AND game_no=?",(pair["tournament_id"],bowler_id,game_no)).fetchone()
            old_score=None if old is None else int(old["score"])
            if old_score==score: continue
            conn.execute("INSERT INTO scores(tournament_id,bowler_id,game_no,score,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(tournament_id,bowler_id,game_no) DO UPDATE SET score=excluded.score,updated_at=excluded.updated_at",(pair["tournament_id"],bowler_id,game_no,score,now_iso()))
            conn.execute("INSERT INTO score_audit(tournament_id,lane_no,bowler_id,game_no,old_score,new_score,submitted_at,source,scorer_id,scorer_name) VALUES(?,?,?,?,?,?,?,?,?,?)",(pair["tournament_id"],lane_no,bowler_id,game_no,old_score,score,now_iso(),"mobile_qr",scorer["id"],scorer["name"]))
            changed_count+=1
        if changed_count: conn.execute("UPDATE lane_pair_sessions SET version=version+1 WHERE token=?",(token,))
        conn.commit()
    return render_pair(token,scorer,notice=f"Saved {changed_count} score change{'s' if changed_count != 1 else ''} successfully.")
