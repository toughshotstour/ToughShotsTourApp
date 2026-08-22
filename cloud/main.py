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


def proper_name(value):
    """Normalize common CSV casing while preserving intentional mixed-case names."""
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if text.isupper() or text.islower():
        return text.title()
    return text


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


def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if len(pin) != 6 or not pin.isdigit():
        raise HTTPException(status_code=400, detail="Scorer PIN must be exactly 6 digits.")
    return pin


def ensure_pin_available(conn: sqlite3.Connection, pin: str, exclude_scorer_id: int | None = None):
    rows = conn.execute("SELECT id,pin_salt,pin_hash FROM scorers").fetchall()
    for row in rows:
        if exclude_scorer_id is not None and int(row["id"]) == int(exclude_scorer_id):
            continue
        if secrets.compare_digest(pin_digest(pin, row["pin_salt"]), row["pin_hash"]):
            raise HTTPException(status_code=409, detail="That PIN is already assigned to another scorer.")


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
    pin = validate_pin(str(payload.get("pin", "")))
    salt = secrets.token_hex(16); digest = pin_digest(pin, salt)
    try:
        with db() as conn:
            ensure_pin_available(conn, pin)
            cur = conn.execute(
                "INSERT INTO scorers(name,pin_salt,pin_hash,active,created_at) VALUES(?,?,?,?,?)",
                (name, salt, digest, 1, now_iso()),
            )
            scorer_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A scorer with that name already exists.")
    return {"ok": True, "scorer": {"id": scorer_id, "name": name, "pin": pin}}


@app.post("/api/scorers/{scorer_id}/reset-pin")
async def api_reset_pin(scorer_id: int, request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    pin = validate_pin(str(payload.get("pin", "")))
    salt = secrets.token_hex(16); digest = pin_digest(pin, salt)
    with db() as conn:
        row = conn.execute("SELECT name FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Scorer not found.")
        ensure_pin_available(conn, pin, exclude_scorer_id=scorer_id)
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
                    (tournament_id, bid, proper_name(bowler["first_name"]), proper_name(bowler["last_name"]), bowler.get("division", "")),
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
button{background:#1769d2;color:#fff;border:0;border-radius:9px;padding:13px 18px;font-size:17px;font-weight:700;margin-top:14px;width:100%}.who{font-size:13px;color:#52677f;margin-bottom:10px}.logout{float:right;font-size:13px;color:#1769d2;text-decoration:none}.pairnav{display:flex;justify-content:space-between;gap:10px;margin:12px 0}.navbtn{display:inline-block;padding:10px 12px;border-radius:8px;background:#eef3f8;text-decoration:none;font-weight:700;color:#1769d2}
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
                fields.append(f'<div class="game"><label>Game {game}</label><input aria-label="{html.escape(proper_name(b["first_name"]))} game {game}" type="number" inputmode="numeric" min="0" max="300" name="{field}" value="{val}"></div>')
            cards.append(f'<section class="bowler"><div class="bowler-head"><div class="name">{html.escape(proper_name(b["first_name"])+" "+proper_name(b["last_name"]))}</div><div class="total">Total: <span>{total if complete else "-"}</span></div></div><div class="scoregrid">{"".join(fields)}</div></section>')
        sections.append(f'<div class="lane-title">Lane {lane_no}</div>{"".join(cards)}')
    label=f"Lane {lane_nos[0]}" if len(lane_nos)==1 else f"Lanes {lane_nos[0]}-{lane_nos[1]}"
    with db() as conn:
        prev_pair=conn.execute("SELECT token,lane_a,lane_b FROM lane_pair_sessions WHERE tournament_id=? AND pair_no<? ORDER BY pair_no DESC LIMIT 1",(pair["tournament_id"],pair["pair_no"])).fetchone()
        next_pair=conn.execute("SELECT token,lane_a,lane_b FROM lane_pair_sessions WHERE tournament_id=? AND pair_no>? ORDER BY pair_no ASC LIMIT 1",(pair["tournament_id"],pair["pair_no"])).fetchone()
    nav=[]
    if prev_pair:
        pl=f"Lane {prev_pair['lane_a']}" if prev_pair['lane_b'] is None else f"Lanes {prev_pair['lane_a']}-{prev_pair['lane_b']}"
        nav.append(f'<a class="navbtn" href="/s/{html.escape(prev_pair["token"])}">← {pl}</a>')
    if next_pair:
        nl=f"Lane {next_pair['lane_a']}" if next_pair['lane_b'] is None else f"Lanes {next_pair['lane_a']}-{next_pair['lane_b']}"
        nav.append(f'<a class="navbtn" href="/s/{html.escape(next_pair["token"])}">{nl} →</a>')
    nav_html='<div class="pairnav">'+''.join(nav)+'</div>' if nav else ''
    notice_html=f'<div class="notice {"warn" if warn else ""}">{html.escape(notice)}</div>' if notice else ""
    script="""<script>document.querySelectorAll('.bowler').forEach(function(card){const inputs=[...card.querySelectorAll('input[type=number]')],total=card.querySelector('.total span');function update(){const vals=inputs.map(i=>i.value.trim());total.textContent=vals.every(v=>v!=='')?vals.reduce((a,v)=>a+Number(v),0):'-'}inputs.forEach(i=>i.addEventListener('input',update));update();});</script>"""
    page=f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">{CSS}<title>{label} Scores</title></head><body><div class="wrap"><div class="card"><a class="logout" href="/logout?next=/s/{html.escape(token)}">Sign out</a><h1>{html.escape(pair['name'])} - {label}</h1><div class="who">Scorer: <strong>{html.escape(scorer['name'])}</strong> &nbsp;|&nbsp; Scores remain editable after saving.</div>{nav_html}{notice_html}<form method="post" action="/s/{html.escape(token)}" onsubmit="return confirm('Save these scores?')"><input type="hidden" name="version" value="{pair['version']}">{''.join(sections)}<button type="submit">Save Scores</button></form></div></div>{script}</body></html>'''
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

# ============================================================
# Permanent bowler database + public Tough Shots results portal
# ============================================================

DIVISIONS = [
    "U12 Mixed", "U14 Boys", "U14 Girls", "U16 Boys", "U16 Girls", "U18 Boys", "U18 Girls"
]


def init_portal_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS permanent_bowlers (
            bowler_id TEXT PRIMARY KEY,
            usbc_id_raw TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            gender TEXT NOT NULL,
            birthdate TEXT NOT NULL,
            division TEXT NOT NULL,
            jr_gold_state TEXT NOT NULL DEFAULT '' CHECK(jr_gold_state IN ('','JG','Q')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(first_name, last_name, birthdate)
        );
        CREATE TABLE IF NOT EXISTS public_tournaments (
            tournament_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'LIVE',
            qualifying_updated_at TEXT,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS public_qualifying (
            tournament_id TEXT NOT NULL,
            division TEXT NOT NULL,
            bowler_id TEXT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            rank INTEGER NOT NULL,
            scores_json TEXT NOT NULL,
            total INTEGER NOT NULL,
            average REAL,
            high_game INTEGER,
            complete INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(tournament_id, division, first_name, last_name, rank),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS tournament_performance (
            tournament_id TEXT NOT NULL,
            bowler_id TEXT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            division TEXT NOT NULL,
            qualifying_rank INTEGER,
            scores_json TEXT NOT NULL,
            qualifying_total INTEGER NOT NULL DEFAULT 0,
            qualifying_average REAL,
            high_game INTEGER,
            match_wins INTEGER NOT NULL DEFAULT 0,
            match_losses INTEGER NOT NULL DEFAULT 0,
            finish_label TEXT,
            boy_points REAL,
            PRIMARY KEY(tournament_id, division, first_name, last_name),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS public_jr_gold_groups (
            tournament_id TEXT NOT NULL,
            group_name TEXT NOT NULL,
            cut_size INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(tournament_id, group_name),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS public_jr_gold (
            tournament_id TEXT NOT NULL,
            group_name TEXT NOT NULL,
            bowler_id TEXT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            source_division TEXT,
            jr_gold_state TEXT NOT NULL,
            rank INTEGER NOT NULL,
            scores_json TEXT NOT NULL,
            total INTEGER NOT NULL,
            average REAL,
            complete INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(tournament_id, group_name, rank, first_name, last_name),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS public_qualifying_settings (
            tournament_id TEXT NOT NULL,
            division TEXT NOT NULL,
            cut_size INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(tournament_id, division),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS public_match_play (
            tournament_id TEXT NOT NULL,
            division TEXT NOT NULL,
            bracket_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tournament_id, division),
            FOREIGN KEY(tournament_id) REFERENCES public_tournaments(tournament_id) ON DELETE CASCADE
        );
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(public_tournaments)").fetchall()}
        if "jr_gold_updated_at" not in cols:
            conn.execute("ALTER TABLE public_tournaments ADD COLUMN jr_gold_updated_at TEXT")
        if "match_play_updated_at" not in cols:
            conn.execute("ALTER TABLE public_tournaments ADD COLUMN match_play_updated_at TEXT")
        # Backfill the now-finalized BOY formula for any tournaments archived by an older build.
        perf = conn.execute("SELECT tournament_id,division,first_name,last_name,qualifying_rank,match_wins,finish_label,boy_points FROM tournament_performance").fetchall()
        sizes = {}
        for r in perf:
            sizes[(r["tournament_id"],r["division"])] = sizes.get((r["tournament_id"],r["division"]),0) + 1
        for r in perf:
            n=sizes[(r["tournament_id"],r["division"])]
            rank=int(r["qualifying_rank"] or 0)
            qp=(n-rank+1) if rank else 0
            bonus=20 if r["finish_label"]=="Champion" else (10 if r["finish_label"]=="Runner-up" else 0)
            points=qp + int(r["match_wins"] or 0)*5 + bonus
            conn.execute("UPDATE tournament_performance SET boy_points=? WHERE tournament_id=? AND division=? AND first_name=? AND last_name=?",(points,r["tournament_id"],r["division"],r["first_name"],r["last_name"]))


init_portal_db()


def _norm_name(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def _normalize_birthdate(value: str) -> str:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    # Repair 0010/0009 style years used by older forms.
    import re
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{1,4})\s*", raw)
    if m:
        month, day, year = map(int, m.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass
    raise HTTPException(status_code=400, detail=f"Invalid birthdate: {raw}")


def _normalize_gender(value: str) -> str:
    value = (value or "").strip()
    key = value.casefold()
    if key in {"m", "male", "boy", "boys"}:
        return "Boy"
    if key in {"f", "female", "girl", "girls"}:
        return "Girl"
    return value


def _division_for(birthdate: str, gender: str) -> str:
    dob = datetime.strptime(birthdate, "%Y-%m-%d").date()
    if dob >= datetime(2014, 8, 1).date():
        return "U12 Mixed"
    if datetime(2012, 8, 1).date() <= dob <= datetime(2014, 7, 31).date():
        age = "U14"
    elif datetime(2010, 8, 1).date() <= dob <= datetime(2012, 7, 31).date():
        age = "U16"
    elif datetime(2008, 8, 1).date() <= dob <= datetime(2010, 7, 31).date():
        age = "U18"
    else:
        return ""
    g = _normalize_gender(gender)
    return f"{age} Boys" if g == "Boy" else (f"{age} Girls" if g == "Girl" else "")


def _allocate_bowler_id(conn: sqlite3.Connection, raw_usbc: str) -> str:
    import re
    digits = re.sub(r"\D", "", str(raw_usbc or ""))
    if not digits:
        raise HTTPException(status_code=400, detail="Every demographic row needs a USBC ID containing digits.")
    if len(digits) > 10:
        raise HTTPException(status_code=400, detail=f"USBC ID {raw_usbc!r} is longer than 10 digits.")
    base = digits.ljust(10, "0")
    if not conn.execute("SELECT 1 FROM permanent_bowlers WHERE bowler_id=?", (base,)).fetchone():
        return base
    prefix = base[:9]
    start = int(base[9])
    for offset in range(1, 10):
        candidate = prefix + str((start + offset) % 10)
        if not conn.execute("SELECT 1 FROM permanent_bowlers WHERE bowler_id=?", (candidate,)).fetchone():
            return candidate
    raise HTTPException(status_code=409, detail=f"No duplicate suffix remains available for USBC ID {raw_usbc!r}.")


def _resolve_permanent(conn: sqlite3.Connection, first: str, last: str, birthdate: str | None):
    first_n, last_n = _norm_name(first), _norm_name(last)
    rows = conn.execute("SELECT * FROM permanent_bowlers").fetchall()
    target_dob = None
    if birthdate:
        try:
            target_dob = _normalize_birthdate(str(birthdate))
        except HTTPException:
            target_dob = None
    matches = [r for r in rows if _norm_name(r["first_name"]) == first_n and _norm_name(r["last_name"]) == last_n]
    if target_dob:
        exact = [r for r in matches if r["birthdate"] == target_dob]
        if len(exact) == 1:
            return exact[0]
    return matches[0] if len(matches) == 1 else None


@app.get("/api/bowlers")
def api_bowlers(x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    with db() as conn:
        rows = conn.execute("SELECT bowler_id,first_name,last_name,gender,birthdate,division,jr_gold_state,usbc_id_raw,updated_at FROM permanent_bowlers ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE").fetchall()
    return {"bowlers": [dict(r) for r in rows]}


@app.post("/api/bowlers/import")
async def api_import_bowlers(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    rows = payload.get("bowlers") or []
    if not rows:
        raise HTTPException(status_code=400, detail="No demographic bowler rows were supplied.")
    created = updated = skipped = 0
    errors = []
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for index, item in enumerate(rows, start=2):
            first = proper_name(item.get("first_name", ""))
            last = proper_name(item.get("last_name", ""))
            try:
                raw_usbc = str(item.get("usbc_id", "")).strip()
                birthdate = _normalize_birthdate(str(item.get("birthdate", "")))
                gender = _normalize_gender(str(item.get("gender", "")))
                division = str(item.get("division", "")).strip() or _division_for(birthdate, gender)
                if not first or not last or not division:
                    raise ValueError("first name, last name, gender/birthdate division, and USBC ID are required")
                requested_state = str(item.get("jr_gold_state", "")).strip().upper() if "jr_gold_state" in item else None
                if requested_state is not None and requested_state not in {"", "JG", "Q"}:
                    raise ValueError("Jr. Gold state must be blank, JG, or Q")
                existing = _resolve_permanent(conn, first, last, birthdate)
                if existing:
                    if requested_state is None:
                        conn.execute("UPDATE permanent_bowlers SET usbc_id_raw=?,first_name=?,last_name=?,gender=?,birthdate=?,division=?,updated_at=? WHERE bowler_id=?",
                                     (raw_usbc, first, last, gender, birthdate, division, now_iso(), existing["bowler_id"]))
                    else:
                        conn.execute("UPDATE permanent_bowlers SET usbc_id_raw=?,first_name=?,last_name=?,gender=?,birthdate=?,division=?,jr_gold_state=?,updated_at=? WHERE bowler_id=?",
                                     (raw_usbc, first, last, gender, birthdate, division, requested_state, now_iso(), existing["bowler_id"]))
                    updated += 1
                else:
                    requested_id = str(item.get("bowler_id", "")).strip()
                    if requested_id:
                        if not (requested_id.isdigit() and len(requested_id) == 10):
                            raise ValueError("Bowler ID must be exactly 10 digits")
                        if conn.execute("SELECT 1 FROM permanent_bowlers WHERE bowler_id=?", (requested_id,)).fetchone():
                            requested_id = ""
                    bowler_id = requested_id or _allocate_bowler_id(conn, raw_usbc)
                    conn.execute("INSERT INTO permanent_bowlers(bowler_id,usbc_id_raw,first_name,last_name,gender,birthdate,division,jr_gold_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (bowler_id, raw_usbc, first, last, gender, birthdate, division, requested_state or "", now_iso(), now_iso()))
                    created += 1
            except Exception as exc:
                skipped += 1
                bowler_name = (f"{first} {last}".strip() or "Unknown bowler")
                errors.append(f"{bowler_name} (row {index}): {exc}")
        conn.commit()
    return {"ok": True, "created": created, "updated": updated, "skipped": skipped, "errors": errors[:25]}


@app.patch("/api/bowlers/{bowler_id}/jr-gold")
async def api_set_jr_gold(bowler_id: str, request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    state = str(payload.get("state", "")).strip().upper()
    if state not in {"", "JG", "Q"}:
        raise HTTPException(status_code=400, detail="Jr. Gold state must be blank, JG, or Q.")
    with db() as conn:
        cur = conn.execute("UPDATE permanent_bowlers SET jr_gold_state=?,updated_at=? WHERE bowler_id=?", (state, now_iso(), bowler_id))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Bowler not found.")
    return {"ok": True, "bowler_id": bowler_id, "state": state}


@app.post("/api/public/qualifying")
async def api_publish_qualifying(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    tournament_id = str(payload.get("tournament_id", "")).strip()
    name = str(payload.get("tournament_name", "Tough Shots Tournament")).strip()
    event_date = str(payload.get("event_date", "")).strip() or datetime.now().date().isoformat()
    divisions = payload.get("divisions") or {}
    cuts = payload.get("cuts") or {}
    if not tournament_id:
        raise HTTPException(status_code=400, detail="Tournament ID is required.")
    unmatched = 0
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO public_tournaments(tournament_id,name,event_date,status,qualifying_updated_at) VALUES(?,?,?,?,?) ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name,event_date=excluded.event_date,status='LIVE',qualifying_updated_at=excluded.qualifying_updated_at",
                     (tournament_id, name, event_date, "LIVE", now_iso()))
        conn.execute("DELETE FROM public_qualifying WHERE tournament_id=?", (tournament_id,))
        conn.execute("DELETE FROM public_qualifying_settings WHERE tournament_id=?", (tournament_id,))
        for division, rows in divisions.items():
            if division not in DIVISIONS:
                continue
            conn.execute("INSERT INTO public_qualifying_settings(tournament_id,division,cut_size) VALUES(?,?,?)", (tournament_id,division,max(0,int(cuts.get(division,0) or 0))))
            for row in rows:
                permanent = _resolve_permanent(conn, row.get("first_name", ""), row.get("last_name", ""), row.get("birthdate"))
                if not permanent:
                    unmatched += 1
                scores = row.get("scores") or []
                valid_scores = [int(x) for x in scores if x is not None and str(x) != ""]
                conn.execute("INSERT INTO public_qualifying(tournament_id,division,bowler_id,first_name,last_name,rank,scores_json,total,average,high_game,complete) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (tournament_id, division, permanent["bowler_id"] if permanent else None, proper_name(row.get("first_name", "")), proper_name(row.get("last_name", "")), int(row.get("rank", 0)), __import__('json').dumps(scores), int(row.get("total", 0)), row.get("average"), max(valid_scores) if valid_scores else None, 1 if row.get("complete") else 0))
        conn.commit()
    return {"ok": True, "tournament_id": tournament_id, "unmatched_bowlers": unmatched}


@app.post("/api/public/match-play")
async def api_publish_match_play(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload=await request.json()
    tournament_id=str(payload.get("tournament_id","")).strip()
    name=str(payload.get("tournament_name","Tough Shots Tournament")).strip()
    event_date=str(payload.get("event_date","")).strip() or datetime.now().date().isoformat()
    divisions=payload.get("divisions") or {}
    if not tournament_id:
        raise HTTPException(status_code=400,detail="Tournament ID is required.")
    import json as _json
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO public_tournaments(tournament_id,name,event_date,status,match_play_updated_at) VALUES(?,?,?,?,?) ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name,event_date=excluded.event_date,status='LIVE',match_play_updated_at=excluded.match_play_updated_at",(tournament_id,name,event_date,"LIVE",now_iso()))
        conn.execute("DELETE FROM public_match_play WHERE tournament_id=?",(tournament_id,))
        published=0
        for division,spec in divisions.items():
            if division in DIVISIONS and (spec or {}).get("rounds"):
                conn.execute("INSERT INTO public_match_play(tournament_id,division,bracket_json,updated_at) VALUES(?,?,?,?)",(tournament_id,division,_json.dumps(spec),now_iso()))
                published += 1
        conn.commit()
    return {"ok":True,"tournament_id":tournament_id,"divisions":published}


@app.delete("/api/public/current")
def api_clear_current(x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    with db() as conn:
        ids={r["tournament_id"] for r in conn.execute("SELECT DISTINCT tournament_id FROM public_qualifying UNION SELECT DISTINCT tournament_id FROM public_jr_gold UNION SELECT DISTINCT tournament_id FROM public_match_play").fetchall()}
        conn.execute("DELETE FROM public_qualifying")
        conn.execute("DELETE FROM public_qualifying_settings")
        conn.execute("DELETE FROM public_jr_gold")
        conn.execute("DELETE FROM public_jr_gold_groups")
        conn.execute("DELETE FROM public_match_play")
        conn.execute("UPDATE public_tournaments SET qualifying_updated_at=NULL,jr_gold_updated_at=NULL,match_play_updated_at=NULL")
        # Keep archived tournament rows/performance history; discard empty live shells.
        conn.execute("DELETE FROM public_tournaments WHERE archived_at IS NULL AND qualifying_updated_at IS NULL AND jr_gold_updated_at IS NULL AND match_play_updated_at IS NULL")
        conn.commit()
    return {"ok":True,"cleared_tournaments":len(ids)}


@app.post("/api/public/archive")
async def api_archive_tournament(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload = await request.json()
    tournament_id = str(payload.get("tournament_id", "")).strip()
    name = str(payload.get("tournament_name", "Tough Shots Tournament")).strip()
    event_date = str(payload.get("event_date", "")).strip() or datetime.now().date().isoformat()
    rows = payload.get("performances") or []
    if not tournament_id or not rows:
        raise HTTPException(status_code=400, detail="Tournament ID and performance rows are required.")
    unmatched = 0
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO public_tournaments(tournament_id,name,event_date,status,qualifying_updated_at,archived_at) VALUES(?,?,?,?,?,?) ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name,event_date=excluded.event_date,status='FINAL',archived_at=excluded.archived_at",
                     (tournament_id, name, event_date, "FINAL", now_iso(), now_iso()))
        conn.execute("DELETE FROM tournament_performance WHERE tournament_id=?", (tournament_id,))
        for row in rows:
            division = str(row.get("division", ""))
            if division not in DIVISIONS:
                continue
            permanent = _resolve_permanent(conn, row.get("first_name", ""), row.get("last_name", ""), row.get("birthdate"))
            if not permanent:
                unmatched += 1
            rank=int(row.get("qualifying_rank") or 0)
            field_size=sum(1 for x in rows if str(x.get("division", ""))==division)
            finish=str(row.get("finish_label", ""))
            calc_points=(field_size-rank+1 if rank else 0) + int(row.get("match_wins",0))*5 + (20 if finish=="Champion" else (10 if finish=="Runner-up" else 0))
            conn.execute("INSERT INTO tournament_performance(tournament_id,bowler_id,first_name,last_name,division,qualifying_rank,scores_json,qualifying_total,qualifying_average,high_game,match_wins,match_losses,finish_label,boy_points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tournament_id, permanent["bowler_id"] if permanent else None, proper_name(row.get("first_name", "")), proper_name(row.get("last_name", "")), division, row.get("qualifying_rank"), __import__('json').dumps(row.get("scores") or []), int(row.get("qualifying_total", 0)), row.get("qualifying_average"), row.get("high_game"), int(row.get("match_wins", 0)), int(row.get("match_losses", 0)), finish, calc_points))

        # Jr. Gold qualification is finalized at archive time. Any bowler who was
        # marked JG, completed all six qualifying games, and finished at or above
        # the configured cut line for their Jr. Gold group is permanently promoted
        # to Q. Bowlers already marked Q remain Q. A cut size of 0 qualifies nobody.
        qualified = conn.execute(
            """
            SELECT DISTINCT j.bowler_id
            FROM public_jr_gold j
            JOIN public_jr_gold_groups g
              ON g.tournament_id=j.tournament_id AND g.group_name=j.group_name
            JOIN permanent_bowlers b ON b.bowler_id=j.bowler_id
            WHERE j.tournament_id=?
              AND j.complete=1
              AND g.cut_size>0
              AND j.rank<=g.cut_size
              AND b.jr_gold_state='JG'
            """,
            (tournament_id,),
        ).fetchall()
        promoted_ids = [r["bowler_id"] for r in qualified]
        if promoted_ids:
            conn.executemany(
                "UPDATE permanent_bowlers SET jr_gold_state='Q', updated_at=? WHERE bowler_id=?",
                [(now_iso(), bid) for bid in promoted_ids],
            )
            conn.executemany(
                "UPDATE public_jr_gold SET jr_gold_state='Q' WHERE tournament_id=? AND bowler_id=?",
                [(tournament_id, bid) for bid in promoted_ids],
            )
        conn.commit()
    return {"ok": True, "archived": len(rows), "unmatched_bowlers": unmatched, "points_formula_configured": True, "jr_gold_promoted": len(promoted_ids), "jr_gold_promoted_ids": promoted_ids}


@app.post("/api/public/jr-gold")
async def api_publish_jr_gold(request: Request, x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    payload=await request.json()
    tournament_id=str(payload.get("tournament_id","")).strip()
    name=str(payload.get("tournament_name","Tough Shots Tournament")).strip()
    event_date=str(payload.get("event_date","")).strip() or datetime.now().date().isoformat()
    groups=payload.get("groups") or {}
    if not tournament_id: raise HTTPException(status_code=400,detail="Tournament ID is required.")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO public_tournaments(tournament_id,name,event_date,status,jr_gold_updated_at) VALUES(?,?,?,?,?) ON CONFLICT(tournament_id) DO UPDATE SET name=excluded.name,event_date=excluded.event_date,jr_gold_updated_at=excluded.jr_gold_updated_at",(tournament_id,name,event_date,"LIVE",now_iso()))
        conn.execute("DELETE FROM public_jr_gold WHERE tournament_id=?",(tournament_id,))
        conn.execute("DELETE FROM public_jr_gold_groups WHERE tournament_id=?",(tournament_id,))
        count=0
        for group_name, spec in groups.items():
            rows=(spec or {}).get("rows") or []
            cut=max(0,int((spec or {}).get("cut_size",0) or 0))
            conn.execute("INSERT INTO public_jr_gold_groups(tournament_id,group_name,cut_size) VALUES(?,?,?)",(tournament_id,str(group_name),cut))
            for row in rows:
                conn.execute("INSERT INTO public_jr_gold(tournament_id,group_name,bowler_id,first_name,last_name,source_division,jr_gold_state,rank,scores_json,total,average,complete) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(tournament_id,str(group_name),row.get("bowler_id"),proper_name(row.get("first_name","")),proper_name(row.get("last_name","")),row.get("source_division",""),row.get("jr_gold_state",""),int(row.get("rank",0)),__import__('json').dumps(row.get("scores") or []),int(row.get("total",0)),row.get("average"),1 if row.get("complete") else 0))
                count += 1
        conn.commit()
    return {"ok":True,"tournament_id":tournament_id,"published_bowlers":count,"groups":len(groups)}


PORTAL_CSS = """
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#152238;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}a{color:#1769d2}.hero{background:#13233a;color:#fff;padding:34px 18px}.hero .inner,.main{max-width:1080px;margin:auto}.hero h1{font-size:34px;margin:0 0 6px}.hero p{margin:0;color:#c7d2e2}.main{padding:24px 16px 50px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.tile{display:block;background:#fff;border:1px solid #dbe3ed;border-radius:14px;padding:20px;text-decoration:none;color:#152238}.tile:hover{border-color:#1769d2}.tile h2{margin:0 0 7px;font-size:19px}.muted{color:#66788d}.buttons{display:flex;gap:10px;flex-wrap:wrap}.btn{display:inline-block;background:#1769d2;color:#fff;text-decoration:none;padding:11px 14px;border-radius:9px;font-weight:700}.tablewrap{overflow:auto;background:#fff;border-radius:12px;border:1px solid #dbe3ed}table{border-collapse:collapse;width:100%}th,td{padding:10px 11px;border-bottom:1px solid #e6ebf1;text-align:left;white-space:nowrap}th{background:#eef3f8}.rank{font-weight:800}.games{font-variant-numeric:tabular-nums}.status{font-size:12px;font-weight:800;padding:3px 7px;border-radius:20px;background:#edf3fb}.search{display:flex;gap:8px;margin:16px 0}.search input{flex:1;padding:11px;border:1px solid #aab6c4;border-radius:8px;font-size:16px}.search button{padding:10px 15px;background:#1769d2;color:#fff;border:0;border-radius:8px;font-weight:700}@media(max-width:600px){.hero h1{font-size:27px}th,td{padding:8px}}
</style>
"""


def _page(title: str, body: str):
    return HTMLResponse(f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>{PORTAL_CSS}</head><body><div class='hero'><div class='inner'><h1>Tough Shots</h1><p>Youth Bowling Tournament Results</p></div></div><main class='main'>{body}</main></body></html>")


def _division_slug(d):
    return d.lower().replace(" ", "-")


def _division_from_slug(slug):
    for d in DIVISIONS:
        if _division_slug(d) == slug:
            return d
    return None


def _latest_current_tournament(conn):
    return conn.execute(
        """SELECT * FROM public_tournaments
           WHERE qualifying_updated_at IS NOT NULL OR jr_gold_updated_at IS NOT NULL OR match_play_updated_at IS NOT NULL
           ORDER BY COALESCE(match_play_updated_at,jr_gold_updated_at,qualifying_updated_at,event_date) DESC LIMIT 1"""
    ).fetchone()


@app.get("/", response_class=HTMLResponse)
def public_home():
    with db() as conn:
        archive_count = conn.execute("SELECT COUNT(*) AS n FROM public_tournaments WHERE archived_at IS NOT NULL").fetchone()["n"]
    body = f"<div class='grid'><a class='tile' href='/current'><h2>Current Tournament</h2></a><a class='tile' href='/bowler-of-the-year'><h2>Bowler of the Year</h2></a><a class='tile' href='/archive'><h2>Tournament Archive</h2><p>{archive_count} tournament(s) archived.</p></a></div>"
    return _page("Tough Shots", body)


@app.get("/current", response_class=HTMLResponse)
def current_tournament_index():
    with db() as conn:
        t=_latest_current_tournament(conn)
    event="" if not t else f"<p><strong>{html.escape(t['name'])}</strong><br><span class='muted'>{html.escape(t['event_date'])}</span></p>"
    tiles=("<div class='grid'>"
           "<a class='tile' href='/standings'><h2>Qualifying</h2></a>"
           "<a class='tile' href='/jr-gold'><h2>Jr. Gold Qualifying</h2></a>"
           "<a class='tile' href='/match-play'><h2>Match Play</h2></a>"
           "</div>")
    return _page("Current Tournament", f"<p><a href='/'>← Home</a></p><h2>Current Tournament</h2>{event}{tiles}")


@app.get("/standings", response_class=HTMLResponse)
def standings_index():
    tiles = "".join(f"<a class='tile' href='/standings/{_division_slug(d)}'><h2>{html.escape(d)}</h2></a>" for d in DIVISIONS)
    return _page("Qualifying Standings", f"<p><a href='/current'>← Current Tournament</a></p><h2>Qualifying Standings</h2><div class='grid'>{tiles}</div>")


@app.get("/standings/{division_slug}", response_class=HTMLResponse)
def standings_division(division_slug: str):
    division = _division_from_slug(division_slug)
    if not division:
        raise HTTPException(status_code=404, detail="Division not found")
    with db() as conn:
        t = conn.execute("SELECT * FROM public_tournaments WHERE qualifying_updated_at IS NOT NULL ORDER BY qualifying_updated_at DESC LIMIT 1").fetchone()
        rows = [] if not t else conn.execute("SELECT * FROM public_qualifying WHERE tournament_id=? AND division=? ORDER BY rank", (t["tournament_id"], division)).fetchall()
        setting = None if not t else conn.execute("SELECT cut_size FROM public_qualifying_settings WHERE tournament_id=? AND division=?",(t["tournament_id"],division)).fetchone()
    if not t:
        return _page(division, f"<p><a href='/standings'>← Divisions</a></p><h2>{html.escape(division)}</h2><p>No qualifying standings have been published yet.</p>")
    cut=int(setting["cut_size"] or 0) if setting else 0
    trs=[]; high_game=-1; high_game_names=[]; high3=-1; high3_names=[]
    import json as _json
    for r in rows:
        scores = _json.loads(r["scores_json"])
        valid=[int(x) for x in scores if x is not None]
        game_text = " / ".join("—" if x is None else str(x) for x in scores)
        avg = "—" if r["average"] is None else f"{r['average']:.2f}"
        boundary=" style='border-bottom:4px solid #1769d2'" if cut and int(r["rank"])==cut else ""
        name=f"{proper_name(r['first_name'])} {proper_name(r['last_name'])}"
        trs.append(f"<tr{boundary}><td class='rank'>{r['rank']}</td><td>{html.escape(name)}</td><td class='games'>{game_text}</td><td>{r['total']}</td><td>{avg}</td></tr>")
        if valid:
            hg=max(valid)
            if hg>high_game: high_game=hg; high_game_names=[name]
            elif hg==high_game: high_game_names.append(name)
        if len(scores)>=3 and all(x is not None for x in scores[:3]):
            h3=sum(int(x) for x in scores[:3])
            if h3>high3: high3=h3; high3_names=[name]
            elif h3==high3: high3_names.append(name)
    table = "<p>No bowlers in this division.</p>" if not trs else "<div class='tablewrap'><table><thead><tr><th>Rank</th><th>Bowler</th><th>Games 1–6</th><th>Total</th><th>Avg.</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>"
    stats=""
    if rows:
        hg_text="—" if high_game<0 else f"{high_game} — {', '.join(high_game_names)}"
        h3_text="—" if high3<0 else f"{high3} — {', '.join(high3_names)}"
        stats=f"<div class='grid stats'><div class='tile'><h2>High Game</h2><p>{html.escape(hg_text)}</p></div><div class='tile'><h2>High 3-Game Set</h2><p>{html.escape(h3_text)}</p></div></div>"
    cut_note=f"Cut line: top {cut}." if cut else "No cut line."
    body = f"<p><a href='/standings'>← Divisions</a></p><h2>{html.escape(division)}</h2><p><strong>{html.escape(t['name'])}</strong> <span class='status'>{html.escape(t['status'])}</span><br><span class='muted'>{html.escape(t['event_date'])} · {html.escape(cut_note)}</span></p>{table}{stats}"
    return _page(f"{division} Standings", body)


@app.get("/match-play", response_class=HTMLResponse)
def match_play_index():
    tiles="".join(f"<a class='tile' href='/match-play/{_division_slug(d)}'><h2>{html.escape(d)}</h2></a>" for d in DIVISIONS)
    return _page("Match Play",f"<p><a href='/current'>← Current Tournament</a></p><h2>Match Play</h2><div class='grid'>{tiles}</div>")


@app.get("/match-play/{division_slug}", response_class=HTMLResponse)
def match_play_division(division_slug: str):
    division=_division_from_slug(division_slug)
    if not division: raise HTTPException(status_code=404,detail="Division not found")
    with db() as conn:
        t=conn.execute("SELECT * FROM public_tournaments WHERE match_play_updated_at IS NOT NULL ORDER BY match_play_updated_at DESC LIMIT 1").fetchone()
        row=None if not t else conn.execute("SELECT bracket_json FROM public_match_play WHERE tournament_id=? AND division=?",(t["tournament_id"],division)).fetchone()
    if not t or not row:
        return _page(f"{division} Match Play",f"<p><a href='/match-play'>← Divisions</a></p><h2>{html.escape(division)} — Match Play</h2><p>No bracket has been published yet.</p>")
    import json as _json
    spec=_json.loads(row["bracket_json"]); rounds=spec.get("rounds") or []
    parts=[f"<p><a href='/match-play'>← Divisions</a></p><h2>{html.escape(division)} — Match Play</h2><p><strong>{html.escape(t['name'])}</strong><br><span class='muted'>{html.escape(t['event_date'])}</span></p>"]
    for rnd in rounds:
        matches=[]
        for m in rnd.get("matches") or []:
            p1=(m.get("p1") or {}).get("name") or "TBD"; p2=(m.get("p2") or {}).get("name") or "TBD"
            s1="—" if m.get("score1") is None else str(m.get("score1")); s2="—" if m.get("score2") is None else str(m.get("score2"))
            winner=m.get("winner")
            p1_class=" class='rank'" if winner and (m.get("p1") or {}).get("bowler_id")==winner else ""
            p2_class=" class='rank'" if winner and (m.get("p2") or {}).get("bowler_id")==winner else ""
            matches.append(f"<div class='tile'><div{p1_class}>{html.escape(p1)} <strong>{s1}</strong></div><div{p2_class}>{html.escape(p2)} <strong>{s2}</strong></div></div>")
        parts.append(f"<h3>{html.escape(rnd.get('name') or 'Round')}</h3><div class='grid'>{''.join(matches) or '<p>No matches.</p>'}</div>")
    return _page(f"{division} Match Play",''.join(parts))


@app.get("/bowler-of-the-year", response_class=HTMLResponse)
def boy_index():
    tiles = "".join(f"<a class='tile' href='/bowler-of-the-year/{_division_slug(d)}'><h2>{html.escape(d)}</h2></a>" for d in DIVISIONS)
    return _page("Bowler of the Year", f"<p><a href='/'>← Home</a></p><h2>Bowler of the Year</h2><div class='grid'>{tiles}</div>")


@app.get("/bowler-of-the-year/{division_slug}", response_class=HTMLResponse)
def boy_division(division_slug: str):
    division = _division_from_slug(division_slug)
    if not division:
        raise HTTPException(status_code=404, detail="Division not found")
    with db() as conn:
        rows = conn.execute("SELECT bowler_id,first_name,last_name,COUNT(*) tournaments,SUM(qualifying_total) pins,SUM(match_wins) wins,MAX(high_game) high_game,AVG(qualifying_average) avg,COALESCE(SUM(boy_points),0) points FROM tournament_performance WHERE division=? GROUP BY COALESCE(bowler_id,first_name||'|'||last_name),first_name,last_name ORDER BY points DESC,avg DESC,last_name,first_name", (division,)).fetchall()
    tr_parts = []
    for i, r in enumerate(rows, 1):
        avg_text = "—" if r["avg"] is None else f"{r['avg']:.2f}"
        points_text = str(int(r["points"] or 0))
        tr_parts.append(f"<tr><td>{i}</td><td>{html.escape(proper_name(r['first_name']))} {html.escape(proper_name(r['last_name']))}</td><td>{r['tournaments']}</td><td>{avg_text}</td><td>{r['high_game'] or '—'}</td><td>{r['wins']}</td><td>{points_text}</td></tr>")
    trs = "".join(tr_parts)
    table = "<p>No archived performances yet.</p>" if not rows else "<div class='tablewrap'><table><thead><tr><th>#</th><th>Bowler</th><th>Events</th><th>Qual. Avg.</th><th>High</th><th>Match Wins</th><th>BOY Points</th></tr></thead><tbody>"+trs+"</tbody></table></div>"
    return _page(f"{division} Bowler of the Year", f"<p><a href='/bowler-of-the-year'>← Divisions</a></p><h2>{html.escape(division)} — Bowler of the Year</h2>{table}")


def _jg_slug(name: str) -> str:
    return name.casefold().replace(" ","-")


def _jg_group_for_division(division: str, available_groups):
    """Resolve one of the seven public division buttons to its active JG group.

    Jr. Gold can merge Boys/Girls within an age group.  The public landing page
    deliberately remains a fixed seven-button layout; when an age group is
    merged, both its Boys and Girls buttons open the same Combined standings.
    """
    names = {g["group_name"]: g for g in available_groups}
    if division in names:
        return names[division]
    if division == "U12 Mixed":
        return names.get("U12 Mixed")
    parts = division.split(" ", 1)
    if len(parts) == 2 and parts[0] in {"U14", "U16", "U18"}:
        return names.get(f"{parts[0]} Combined")
    return None


@app.get("/jr-gold", response_class=HTMLResponse)
def jr_gold_index():
    with db() as conn:
        t=conn.execute("SELECT * FROM public_tournaments WHERE jr_gold_updated_at IS NOT NULL ORDER BY event_date DESC,jr_gold_updated_at DESC LIMIT 1").fetchone()
        groups=[] if not t else conn.execute("SELECT * FROM public_jr_gold_groups WHERE tournament_id=? ORDER BY group_name",(t["tournament_id"],)).fetchall()

    # Match the regular Qualifying and Bowler of the Year pages exactly: seven
    # permanent division buttons.  Merge settings affect the destination data,
    # not the shape of this navigation page.
    tiles="".join(
        f"<a class='tile' href='/jr-gold/{_division_slug(d)}'><h2>{html.escape(d)}</h2></a>"
        for d in DIVISIONS
    )
    status = "" if t else "<p>No Jr. Gold standings have been published yet.</p>"
    event = "" if not t else f"<p><strong>{html.escape(t['name'])}</strong><br>{html.escape(t['event_date'])}</p>"
    return _page("Jr. Gold Qualifying",f"<p><a href='/current'>← Current Tournament</a></p><h2>Jr. Gold Qualifying</h2>{event}{status}<div class='grid'>{tiles}</div>")


@app.get("/jr-gold/{division_slug}", response_class=HTMLResponse)
def jr_gold_group(division_slug: str):
    division = _division_from_slug(division_slug)
    if not division:
        raise HTTPException(status_code=404,detail="Jr. Gold division not found")
    with db() as conn:
        t=conn.execute("SELECT * FROM public_tournaments WHERE jr_gold_updated_at IS NOT NULL ORDER BY event_date DESC,jr_gold_updated_at DESC LIMIT 1").fetchone()
        if not t:
            return _page(division, f"<p><a href='/jr-gold'>← Jr. Gold Divisions</a></p><h2>{html.escape(division)} — Jr. Gold Qualifying</h2><p>No Jr. Gold standings have been published yet.</p>")
        groups=conn.execute("SELECT * FROM public_jr_gold_groups WHERE tournament_id=?",(t["tournament_id"],)).fetchall()
        g=_jg_group_for_division(division, groups)
        if not g:
            return _page(division, f"<p><a href='/jr-gold'>← Jr. Gold Divisions</a></p><h2>{html.escape(division)} — Jr. Gold Qualifying</h2><p>No Jr. Gold standings are available for this division.</p>")
        rows=conn.execute("SELECT * FROM public_jr_gold WHERE tournament_id=? AND group_name=? ORDER BY rank",(t["tournament_id"],g["group_name"])).fetchall()
    import json as _json
    trs=[]; cut=int(g["cut_size"] or 0)
    for r in rows:
        scores=_json.loads(r["scores_json"]); games=" / ".join("—" if x is None else str(x) for x in scores); avg="—" if r["average"] is None else f"{r['average']:.2f}"
        boundary=" style='border-bottom:4px solid #1769d2'" if cut and int(r["rank"])==cut else ""
        trs.append(f"<tr{boundary}><td class='rank'>{r['rank']}</td><td>{html.escape(proper_name(r['first_name']))} {html.escape(proper_name(r['last_name']))}</td><td>{html.escape(r['jr_gold_state'])}</td><td class='games'>{games}</td><td>{r['total']}</td><td>{avg}</td></tr>")
    table="<p>No eligible bowlers in this group.</p>" if not trs else "<div class='tablewrap'><table><thead><tr><th>Rank</th><th>Bowler</th><th>JG</th><th>Games 1–6</th><th>Total</th><th>Avg.</th></tr></thead><tbody>"+"".join(trs)+"</tbody></table></div>"
    note=f"Cut line after place {cut}." if cut else "No cut line set."
    return _page(f"{g['group_name']} Jr. Gold",f"<p><a href='/jr-gold'>← Jr. Gold Divisions</a></p><h2>{html.escape(g['group_name'])} — Jr. Gold Qualifying</h2><p><strong>{html.escape(t['name'])}</strong><br>{html.escape(t['event_date'])} · {html.escape(note)}</p>{table}")


@app.get("/archive", response_class=HTMLResponse)
def archive(q: str = ""):
    q = (q or "").strip()
    with db() as conn:
        tournaments = conn.execute("SELECT * FROM public_tournaments WHERE archived_at IS NOT NULL ORDER BY event_date DESC").fetchall()
        matches = []
        if q:
            like = f"%{q}%"
            permanent_ids = [r["bowler_id"] for r in conn.execute("SELECT bowler_id FROM permanent_bowlers WHERE (first_name||' '||last_name) LIKE ? COLLATE NOCASE", (like,)).fetchall()]
            if permanent_ids:
                qmarks = ",".join("?" for _ in permanent_ids)
                matches = conn.execute(f"SELECT p.*,t.name tournament_name,t.event_date FROM tournament_performance p JOIN public_tournaments t ON t.tournament_id=p.tournament_id WHERE (p.first_name||' '||p.last_name) LIKE ? COLLATE NOCASE OR p.bowler_id IN ({qmarks}) ORDER BY t.event_date DESC", (like, *permanent_ids)).fetchall()
            else:
                matches = conn.execute("SELECT p.*,t.name tournament_name,t.event_date FROM tournament_performance p JOIN public_tournaments t ON t.tournament_id=p.tournament_id WHERE (p.first_name||' '||p.last_name) LIKE ? COLLATE NOCASE ORDER BY t.event_date DESC", (like,)).fetchall()
    search = f"<form class='search'><input name='q' value='{html.escape(q)}' placeholder='Enter bowler name'><button>Search</button></form>"
    result_html = ""
    if q:
        tr_parts = []
        for r in matches:
            avg_text = "—" if r["qualifying_average"] is None else f"{r['qualifying_average']:.2f}"
            tr_parts.append(f"<tr><td>{html.escape(r['event_date'])}</td><td>{html.escape(r['tournament_name'])}</td><td>{html.escape(proper_name(r['first_name']))} {html.escape(proper_name(r['last_name']))}</td><td>{html.escape(r['division'])}</td><td>{r['qualifying_total']}</td><td>{avg_text}</td><td>{html.escape(r['finish_label'] or '—')}</td></tr>")
        trs = "".join(tr_parts)
        result_html = "<h3>Bowler Results</h3>" + ("<p>No matching performances found.</p>" if not matches else "<div class='tablewrap'><table><thead><tr><th>Date</th><th>Tournament</th><th>Bowler</th><th>Division</th><th>Qual. Total</th><th>Avg.</th><th>Match Play</th></tr></thead><tbody>"+trs+"</tbody></table></div>")
    tournament_tiles = "".join(f"<a class='tile' href='/archive/tournament/{html.escape(t['tournament_id'])}'><h2>{html.escape(t['name'])}</h2><p>{html.escape(t['event_date'])} · View final results</p></a>" for t in tournaments)
    return _page("Tournament Archive", f"<p><a href='/'>← Home</a></p><h2>Tournament Archive</h2><p class='muted'>Search a bowler by name to see all recorded past performances.</p>{search}{result_html}<h3>Past Tournaments</h3><div class='grid'>{tournament_tiles or '<p>No tournaments archived yet.</p>'}</div>")

@app.get("/archive/tournament/{tournament_id}", response_class=HTMLResponse)
def archive_tournament_page(tournament_id: str):
    with db() as conn:
        t = conn.execute("SELECT * FROM public_tournaments WHERE tournament_id=? AND archived_at IS NOT NULL", (tournament_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Archived tournament not found")
        rows = conn.execute("SELECT * FROM tournament_performance WHERE tournament_id=? ORDER BY division,qualifying_rank,last_name,first_name", (tournament_id,)).fetchall()
    body_parts=[f"<p><a href='/archive'>← Tournament Archive</a></p><h2>{html.escape(t['name'])}</h2><p class='muted'>{html.escape(t['event_date'])} · Final results</p>"]
    import json as _json
    for division in DIVISIONS:
        divrows=[r for r in rows if r['division']==division]
        if not divrows:
            continue
        tr_parts=[]
        for r in divrows:
            scores=_json.loads(r['scores_json'])
            games=" / ".join("—" if x is None else str(x) for x in scores)
            avg="—" if r['qualifying_average'] is None else f"{r['qualifying_average']:.2f}"
            tr_parts.append(f"<tr><td>{r['qualifying_rank'] or '—'}</td><td>{html.escape(proper_name(r['first_name']))} {html.escape(proper_name(r['last_name']))}</td><td>{games}</td><td>{r['qualifying_total']}</td><td>{avg}</td><td>{html.escape(r['finish_label'] or '—')}</td></tr>")
        body_parts.append(f"<h3>{html.escape(division)}</h3><div class='tablewrap'><table><thead><tr><th>Qual. Rank</th><th>Bowler</th><th>Games 1–6</th><th>Total</th><th>Avg.</th><th>Match Play</th></tr></thead><tbody>{''.join(tr_parts)}</tbody></table></div>")
    return _page(t['name'], ''.join(body_parts))
