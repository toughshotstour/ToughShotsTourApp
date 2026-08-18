#!/usr/bin/env python3
"""
Bowling Tournament Manager

Input:
    all_divisions.csv

Expected required columns:
    First_Name
    Last_Name
    Division

Optional useful columns:
    Gender
    Birthdate_Used

Features:
- Treats each unique Division as its own tournament.
- Stores a configurable number of qualifying games (default: 6).
- Auto-saves scores to SQLite.
- Ranks qualifying by total score.
- Lets the director choose the cut size separately for each division.
- Lets the director adjust seeding before creating match play.
- Creates a seeded single-elimination bracket automatically.
- For an 8-person cut, the bracket is exactly:
      1 vs 8
      4 vs 5
      2 vs 7
      3 vs 6
  which advances as:
      Winner(1v8) vs Winner(4v5)
      Winner(2v7) vs Winner(3v6)
      Final
- Supports non-power-of-two cut sizes by using automatic byes.
- Stores match-play scores and advances winners automatically.
- Exports qualifying, bracket, and summary CSVs.

Run:
    python bowling_tournament_manager.py all_divisions.csv

If no roster path is given, the program will ask you to choose one.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


APP_TITLE = "Bowling Tournament Manager"
DEFAULT_QUALIFYING_GAMES = 6
DEFAULT_CUT_SIZE = 8
MAX_QUALIFYING_GAMES = 20
MAX_BRACKET_SIZE = 64


# ============================================================
# Database / tournament model
# ============================================================

class TournamentDB:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        self.conn.close()

    def _init_schema(self):
        cur = self.conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bowlers (
                bowler_id TEXT PRIMARY KEY,
                source_row INTEGER NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                gender TEXT,
                birthdate TEXT,
                division TEXT NOT NULL,
                source_json TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                bowler_id TEXT NOT NULL,
                game_no INTEGER NOT NULL,
                score INTEGER NOT NULL,
                PRIMARY KEY (bowler_id, game_no),
                FOREIGN KEY (bowler_id) REFERENCES bowlers(bowler_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS division_settings (
                division TEXT PRIMARY KEY,
                cut_size INTEGER NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS brackets (
                division TEXT PRIMARY KEY,
                bracket_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        self.conn.commit()

        if self.get_meta("qualifying_games") is None:
            self.set_meta("qualifying_games", str(DEFAULT_QUALIFYING_GAMES))

    def get_meta(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.conn.commit()

    @property
    def qualifying_games(self):
        return int(self.get_meta("qualifying_games", DEFAULT_QUALIFYING_GAMES))

    @qualifying_games.setter
    def qualifying_games(self, value):
        value = int(value)
        if not 1 <= value <= MAX_QUALIFYING_GAMES:
            raise ValueError(
                f"Qualifying games must be between 1 and {MAX_QUALIFYING_GAMES}."
            )
        self.set_meta("qualifying_games", value)

    def roster_loaded(self):
        row = self.conn.execute("SELECT COUNT(*) AS n FROM bowlers").fetchone()
        return row["n"] > 0

    def import_roster(self, csv_path):
        csv_path = Path(csv_path)

        with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            required = {"First_Name", "Last_Name", "Division"}
            headers = set(reader.fieldnames or [])
            missing = required - headers

            if missing:
                raise ValueError(
                    "Roster is missing required columns: "
                    + ", ".join(sorted(missing))
                )

            rows = list(reader)

        if not rows:
            raise ValueError("The roster file has no participant rows.")

        cur = self.conn.cursor()
        cur.execute("DELETE FROM bowlers")
        cur.execute("DELETE FROM scores")
        cur.execute("DELETE FROM division_settings")
        cur.execute("DELETE FROM brackets")

        for idx, row in enumerate(rows, start=2):
            first = (row.get("First_Name") or "").strip()
            last = (row.get("Last_Name") or "").strip()
            division = (row.get("Division") or "").strip()
            gender = (row.get("Gender") or "").strip()
            birthdate = (row.get("Birthdate_Used") or "").strip()

            if not first or not last or not division:
                continue

            raw_key = f"{idx}|{first}|{last}|{birthdate}|{division}"
            bowler_id = hashlib.sha1(
                raw_key.encode("utf-8")
            ).hexdigest()[:16]

            cur.execute(
                """
                INSERT INTO bowlers(
                    bowler_id, source_row, first_name, last_name,
                    gender, birthdate, division, source_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bowler_id,
                    idx,
                    first,
                    last,
                    gender,
                    birthdate,
                    division,
                    json.dumps(row, ensure_ascii=False),
                ),
            )

        divisions = [
            row["division"]
            for row in cur.execute(
                "SELECT DISTINCT division FROM bowlers ORDER BY division"
            ).fetchall()
        ]

        for division in divisions:
            count = cur.execute(
                "SELECT COUNT(*) AS n FROM bowlers WHERE division = ?",
                (division,),
            ).fetchone()["n"]

            cut = min(DEFAULT_CUT_SIZE, count)
            if cut < 2 and count >= 2:
                cut = 2

            cur.execute(
                """
                INSERT INTO division_settings(division, cut_size)
                VALUES (?, ?)
                """,
                (division, cut),
            )

        self.set_meta("roster_path", str(csv_path.resolve()))
        self.set_meta("roster_imported_at", datetime.now().isoformat(timespec="seconds"))
        self.conn.commit()

    def divisions(self):
        return [
            row["division"]
            for row in self.conn.execute(
                "SELECT DISTINCT division FROM bowlers ORDER BY division"
            ).fetchall()
        ]

    def bowlers(self, division):
        return self.conn.execute(
            """
            SELECT *
            FROM bowlers
            WHERE division = ?
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
            """,
            (division,),
        ).fetchall()

    def bowler(self, bowler_id):
        return self.conn.execute(
            "SELECT * FROM bowlers WHERE bowler_id = ?",
            (bowler_id,),
        ).fetchone()

    def display_name(self, bowler_id):
        if not bowler_id:
            return "BYE"
        row = self.bowler(bowler_id)
        if not row:
            return "Unknown"
        return f"{row['first_name']} {row['last_name']}"

    def set_score(self, bowler_id, game_no, score):
        game_no = int(game_no)
        score = int(score)

        if not 0 <= score <= 300:
            raise ValueError("Qualifying scores must be between 0 and 300.")

        self.conn.execute(
            """
            INSERT INTO scores(bowler_id, game_no, score)
            VALUES (?, ?, ?)
            ON CONFLICT(bowler_id, game_no)
            DO UPDATE SET score = excluded.score
            """,
            (bowler_id, game_no, score),
        )
        self.conn.commit()

    def delete_score(self, bowler_id, game_no):
        self.conn.execute(
            "DELETE FROM scores WHERE bowler_id = ? AND game_no = ?",
            (bowler_id, int(game_no)),
        )
        self.conn.commit()

    def scores_for_bowler(self, bowler_id):
        return {
            int(row["game_no"]): int(row["score"])
            for row in self.conn.execute(
                """
                SELECT game_no, score
                FROM scores
                WHERE bowler_id = ?
                ORDER BY game_no
                """,
                (bowler_id,),
            ).fetchall()
        }

    def cut_size(self, division):
        row = self.conn.execute(
            "SELECT cut_size FROM division_settings WHERE division = ?",
            (division,),
        ).fetchone()

        if row:
            return int(row["cut_size"])

        count = len(self.bowlers(division))
        return min(DEFAULT_CUT_SIZE, count)

    def set_cut_size(self, division, cut_size):
        cut_size = int(cut_size)
        field_size = len(self.bowlers(division))

        if field_size < 2:
            raise ValueError("A division needs at least 2 bowlers for match play.")

        if cut_size < 2:
            raise ValueError("Cut size must be at least 2.")

        if cut_size > field_size:
            raise ValueError(
                f"Cut size cannot exceed the division field size ({field_size})."
            )

        if cut_size > MAX_BRACKET_SIZE:
            raise ValueError(
                f"Cut size cannot exceed {MAX_BRACKET_SIZE}."
            )

        self.conn.execute(
            """
            INSERT INTO division_settings(division, cut_size)
            VALUES (?, ?)
            ON CONFLICT(division)
            DO UPDATE SET cut_size = excluded.cut_size
            """,
            (division, cut_size),
        )
        self.conn.commit()

    def qualifying_rows(self, division):
        games = self.qualifying_games
        rows = []

        for bowler in self.bowlers(division):
            scores = self.scores_for_bowler(bowler["bowler_id"])
            game_values = [scores.get(i) for i in range(1, games + 1)]
            complete = all(v is not None for v in game_values)
            total = sum(v for v in game_values if v is not None)
            average = total / games if complete and games else None

            # Ranking tie-break:
            # total, then latest game backward, then name.
            reverse_games = tuple(
                (v if v is not None else -1)
                for v in reversed(game_values)
            )

            rows.append({
                "bowler_id": bowler["bowler_id"],
                "first_name": bowler["first_name"],
                "last_name": bowler["last_name"],
                "gender": bowler["gender"],
                "birthdate": bowler["birthdate"],
                "scores": game_values,
                "complete": complete,
                "total": total,
                "average": average,
                "_reverse_games": reverse_games,
            })

        rows.sort(
            key=lambda r: (
                -r["total"],
                tuple(-x for x in r["_reverse_games"]),
                r["last_name"].casefold(),
                r["first_name"].casefold(),
            )
        )

        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank

        return rows

    def division_scoring_complete(self, division):
        rows = self.qualifying_rows(division)
        return bool(rows) and all(row["complete"] for row in rows)

    def save_bracket(self, division, state):
        state = recompute_bracket(state)
        self.conn.execute(
            """
            INSERT INTO brackets(division, bracket_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(division)
            DO UPDATE SET
                bracket_json = excluded.bracket_json,
                updated_at = excluded.updated_at
            """,
            (
                division,
                json.dumps(state),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def load_bracket(self, division):
        row = self.conn.execute(
            "SELECT bracket_json FROM brackets WHERE division = ?",
            (division,),
        ).fetchone()

        if not row:
            return None

        state = json.loads(row["bracket_json"])
        return recompute_bracket(state)

    def delete_bracket(self, division):
        self.conn.execute(
            "DELETE FROM brackets WHERE division = ?",
            (division,),
        )
        self.conn.commit()

    def set_match_score(self, division, round_index, match_index, slot, value):
        state = self.load_bracket(division)
        if not state:
            raise ValueError("No bracket exists for this division.")

        match = state["rounds"][round_index][match_index]
        key = "score1" if slot == 1 else "score2"

        if value in ("", None):
            match[key] = None
        else:
            score = int(value)
            if score < 0 or score > 9999:
                raise ValueError("Match-play score must be between 0 and 9999.")
            match[key] = score

        match["manual_winner"] = None
        self.save_bracket(division, state)

    def set_manual_winner(self, division, round_index, match_index, bowler_id):
        state = self.load_bracket(division)
        if not state:
            return

        match = state["rounds"][round_index][match_index]
        if bowler_id not in {match.get("p1"), match.get("p2")}:
            raise ValueError("Selected bowler is not in this match.")

        match["manual_winner"] = bowler_id
        self.save_bracket(division, state)

    def export_results(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        games = self.qualifying_games

        # Qualifying results
        qualifying_path = folder / "qualifying_results.csv"
        with qualifying_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            header = [
                "Division", "Rank", "Seed", "First_Name", "Last_Name",
                "Gender", "Birthdate"
            ]
            header += [f"Game_{i}" for i in range(1, games + 1)]
            header += ["Total", "Average", "Qualifying_Complete", "Made_Cut"]
            writer.writerow(header)

            for division in self.divisions():
                bracket = self.load_bracket(division)
                seed_by_bowler = {}
                if bracket:
                    seed_by_bowler = {
                        bowler_id: int(seed)
                        for seed, bowler_id in bracket.get("seed_map", {}).items()
                        if bowler_id
                    }

                cut = self.cut_size(division)

                for row in self.qualifying_rows(division):
                    seed = seed_by_bowler.get(row["bowler_id"], "")
                    made_cut = (
                        "YES"
                        if row["complete"] and row["rank"] <= cut
                        else "NO"
                    )

                    out = [
                        division,
                        row["rank"],
                        seed,
                        row["first_name"],
                        row["last_name"],
                        row["gender"],
                        row["birthdate"],
                    ]
                    out += [
                        "" if score is None else score
                        for score in row["scores"]
                    ]
                    out += [
                        row["total"],
                        "" if row["average"] is None else f"{row['average']:.2f}",
                        "YES" if row["complete"] else "NO",
                        made_cut,
                    ]
                    writer.writerow(out)

        # Match play results
        match_path = folder / "match_play_results.csv"
        with match_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Division", "Round", "Match",
                "Participant_1", "Score_1",
                "Participant_2", "Score_2",
                "Winner"
            ])

            for division in self.divisions():
                state = self.load_bracket(division)
                if not state:
                    continue

                for r_idx, round_matches in enumerate(state["rounds"]):
                    round_name = bracket_round_name(len(round_matches))

                    for m_idx, match in enumerate(round_matches, start=1):
                        writer.writerow([
                            division,
                            round_name,
                            m_idx,
                            self.display_name(match.get("p1")) if match.get("p1") else "",
                            "" if match.get("score1") is None else match.get("score1"),
                            self.display_name(match.get("p2")) if match.get("p2") else "",
                            "" if match.get("score2") is None else match.get("score2"),
                            self.display_name(match.get("winner")) if match.get("winner") else "",
                        ])

        # Division summary
        summary_path = folder / "division_summary.csv"
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Division", "Field_Size", "Qualifying_Games", "Cut_Size",
                "Qualifying_Complete", "Bracket_Created", "Champion"
            ])

            for division in self.divisions():
                state = self.load_bracket(division)
                champion = ""

                if state and state["rounds"]:
                    final = state["rounds"][-1][0]
                    if final.get("winner"):
                        champion = self.display_name(final["winner"])

                writer.writerow([
                    division,
                    len(self.bowlers(division)),
                    games,
                    self.cut_size(division),
                    "YES" if self.division_scoring_complete(division) else "NO",
                    "YES" if state else "NO",
                    champion,
                ])

        return [qualifying_path, match_path, summary_path]


# ============================================================
# Bracket engine
# ============================================================

def next_power_of_two(n):
    value = 1
    while value < n:
        value *= 2
    return value


def bracket_seed_order(size):
    """
    Standard seeded bracket slot order.

    8 returns:
        [1, 8, 4, 5, 2, 7, 3, 6]

    Pairing adjacent values gives:
        1v8
        4v5
        2v7
        3v6

    The next round pairs adjacent match winners, producing:
        Winner(1v8) vs Winner(4v5)
        Winner(2v7) vs Winner(3v6)
    """
    if size < 2 or size & (size - 1):
        raise ValueError("Bracket size must be a power of 2.")

    order = [1, 2]
    current = 2

    while current < size:
        current *= 2
        expanded = []
        for seed in order:
            expanded.extend([seed, current + 1 - seed])
        order = expanded

    return order


def bracket_round_name(matches_in_round):
    if matches_in_round == 1:
        return "Final"
    if matches_in_round == 2:
        return "Semifinal"
    if matches_in_round == 4:
        return "Quarterfinal"
    return f"Round of {matches_in_round * 2}"


def create_bracket_state(division, ordered_bowler_ids):
    cut_size = len(ordered_bowler_ids)

    if cut_size < 2:
        raise ValueError("At least 2 qualifiers are required for match play.")

    bracket_size = next_power_of_two(cut_size)

    if bracket_size > MAX_BRACKET_SIZE:
        raise ValueError(
            f"Bracket exceeds supported size of {MAX_BRACKET_SIZE}."
        )

    seed_map = {
        str(seed): bowler_id
        for seed, bowler_id in enumerate(ordered_bowler_ids, start=1)
    }

    order = bracket_seed_order(bracket_size)
    first_round = []

    for i in range(0, len(order), 2):
        seed1 = order[i]
        seed2 = order[i + 1]

        first_round.append({
            "p1": seed_map.get(str(seed1)),
            "p2": seed_map.get(str(seed2)),
            "seed1": seed1 if seed1 <= cut_size else None,
            "seed2": seed2 if seed2 <= cut_size else None,
            "score1": None,
            "score2": None,
            "manual_winner": None,
            "winner": None,
        })

    rounds = [first_round]
    match_count = len(first_round)

    while match_count > 1:
        match_count //= 2
        rounds.append([
            {
                "p1": None,
                "p2": None,
                "seed1": None,
                "seed2": None,
                "score1": None,
                "score2": None,
                "manual_winner": None,
                "winner": None,
            }
            for _ in range(match_count)
        ])

    state = {
        "division": division,
        "cut_size": cut_size,
        "bracket_size": bracket_size,
        "seed_map": seed_map,
        "rounds": rounds,
    }

    return recompute_bracket(state)


def recompute_bracket(state):
    rounds = state["rounds"]

    for r_idx, round_matches in enumerate(rounds):
        for m_idx, match in enumerate(round_matches):
            if r_idx > 0:
                source_a = rounds[r_idx - 1][m_idx * 2]
                source_b = rounds[r_idx - 1][m_idx * 2 + 1]
                new_p1 = source_a.get("winner")
                new_p2 = source_b.get("winner")

                if match.get("p1") != new_p1 or match.get("p2") != new_p2:
                    match["p1"] = new_p1
                    match["p2"] = new_p2
                    match["score1"] = None
                    match["score2"] = None
                    match["manual_winner"] = None
                    match["winner"] = None

            p1 = match.get("p1")
            p2 = match.get("p2")

            if p1 and not p2:
                match["winner"] = p1
                continue

            if p2 and not p1:
                match["winner"] = p2
                continue

            if not p1 and not p2:
                match["winner"] = None
                continue

            manual = match.get("manual_winner")
            if manual in {p1, p2}:
                match["winner"] = manual
                continue

            s1 = match.get("score1")
            s2 = match.get("score2")

            if s1 is not None and s2 is not None and s1 != s2:
                match["winner"] = p1 if s1 > s2 else p2
            else:
                match["winner"] = None

    return state


# ============================================================
# GUI helpers
# ============================================================

class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)

        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vscroll = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview
        )
        self.hscroll = ttk.Scrollbar(
            self, orient="horizontal", command=self.canvas.xview
        )

        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )

        self.canvas.configure(
            yscrollcommand=self.vscroll.set,
            xscrollcommand=self.hscroll.set,
        )

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        self.hscroll.grid(row=1, column=0, sticky="ew")

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.inner.bind("<Configure>", self._update_scrollregion)

        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _update_scrollregion(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass


class SeedDialog(tk.Toplevel):
    def __init__(self, parent, db, division, cut_size, on_create):
        super().__init__(parent)
        self.db = db
        self.division = division
        self.cut_size = cut_size
        self.on_create = on_create

        self.title(f"Seed Match Play — {division}")
        self.geometry("650x500")
        self.transient(parent)
        self.grab_set()

        ttk.Label(
            self,
            text=f"Top {cut_size} qualifiers — adjust order if needed",
            font=("", 12, "bold"),
        ).pack(pady=(12, 6))

        ttk.Label(
            self,
            text=(
                "Default order is qualifying total, then latest game backward. "
                "Use Up/Down to resolve ties or make a director adjustment."
            ),
            wraplength=600,
        ).pack(padx=12, pady=(0, 8))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        self.listbox = tk.Listbox(
            body,
            activestyle="dotbox",
            font=("TkFixedFont", 10),
        )
        self.listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(
            body, orient="vertical", command=self.listbox.yview
        )
        scrollbar.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)

        ranking = self.db.qualifying_rows(division)

        self.rows = ranking[:cut_size]

        for idx, row in enumerate(self.rows, start=1):
            score_text = f"{row['total']:4d}"
            self.listbox.insert(
                "end",
                f"{idx:>2}. {row['first_name']} {row['last_name']} — {score_text}"
            )

        buttons = ttk.Frame(body)
        buttons.pack(side="left", fill="y", padx=(10, 0))

        ttk.Button(buttons, text="Move Up", command=self.move_up).pack(
            fill="x", pady=3
        )
        ttk.Button(buttons, text="Move Down", command=self.move_down).pack(
            fill="x", pady=3
        )

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=12)

        ttk.Button(
            bottom,
            text="Create Bracket",
            command=self.create,
        ).pack(side="right")

        ttk.Button(
            bottom,
            text="Cancel",
            command=self.destroy,
        ).pack(side="right", padx=(0, 8))

        if self.rows:
            self.listbox.selection_set(0)

    def refresh_labels(self):
        selected = self.listbox.curselection()
        selected_idx = selected[0] if selected else 0

        self.listbox.delete(0, "end")

        for idx, row in enumerate(self.rows, start=1):
            self.listbox.insert(
                "end",
                f"{idx:>2}. {row['first_name']} {row['last_name']} — {row['total']:4d}"
            )

        if self.rows:
            selected_idx = max(0, min(selected_idx, len(self.rows) - 1))
            self.listbox.selection_set(selected_idx)
            self.listbox.see(selected_idx)

    def move_up(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        i = selected[0]
        if i <= 0:
            return
        self.rows[i - 1], self.rows[i] = self.rows[i], self.rows[i - 1]
        self.refresh_labels()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(i - 1)

    def move_down(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        i = selected[0]
        if i >= len(self.rows) - 1:
            return
        self.rows[i + 1], self.rows[i] = self.rows[i], self.rows[i + 1]
        self.refresh_labels()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(i + 1)

    def create(self):
        ids = [row["bowler_id"] for row in self.rows]
        self.on_create(ids)
        self.destroy()


# ============================================================
# Main application
# ============================================================

class TournamentApp(tk.Tk):
    def __init__(self, db, roster_path):
        super().__init__()

        self.db = db
        self.roster_path = Path(roster_path)

        self.title(APP_TITLE)
        self.geometry("1380x850")
        self.minsize(1050, 650)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.qualifying_entries = {}
        self.qualifying_labels = {}
        self.match_score_vars = []

        self._build_ui()

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")

        ttk.Label(
            toolbar,
            text=APP_TITLE,
            font=("", 14, "bold"),
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Tournament Settings",
            command=self.open_settings,
        ).pack(side="right", padx=4)

        ttk.Button(
            toolbar,
            text="Export Results",
            command=self.export_results,
        ).pack(side="right", padx=4)

        ttk.Button(
            toolbar,
            text="Refresh",
            command=self.refresh_all,
        ).pack(side="right", padx=4)

        info = ttk.Label(
            self,
            text=(
                f"Roster: {self.roster_path.name}    "
                f"Database: {self.db.db_path.name}"
            ),
            padding=(8, 0),
        )
        info.pack(fill="x")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.qualifying_tab = ttk.Frame(self.notebook)
        self.match_tab = ttk.Frame(self.notebook)
        self.summary_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.qualifying_tab, text="Qualifying")
        self.notebook.add(self.match_tab, text="Match Play")
        self.notebook.add(self.summary_tab, text="Summary")

        self._build_qualifying_tab()
        self._build_match_tab()
        self._build_summary_tab()

    # --------------------------------------------------------
    # Qualifying
    # --------------------------------------------------------

    def _build_qualifying_tab(self):
        controls = ttk.Frame(self.qualifying_tab, padding=8)
        controls.pack(fill="x")

        ttk.Label(controls, text="Division:").pack(side="left")

        self.qual_division = tk.StringVar()
        self.qual_division_combo = ttk.Combobox(
            controls,
            textvariable=self.qual_division,
            values=self.db.divisions(),
            state="readonly",
            width=22,
        )
        self.qual_division_combo.pack(side="left", padx=(4, 12))
        self.qual_division_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.refresh_qualifying()
        )

        ttk.Label(controls, text="Cut size:").pack(side="left")

        self.cut_var = tk.StringVar()
        self.cut_spin = ttk.Spinbox(
            controls,
            textvariable=self.cut_var,
            from_=2,
            to=MAX_BRACKET_SIZE,
            width=6,
        )
        self.cut_spin.pack(side="left", padx=(4, 6))

        ttk.Button(
            controls,
            text="Save Cut Size",
            command=self.save_cut_size,
        ).pack(side="left", padx=(0, 10))

        ttk.Button(
            controls,
            text="Save Scores",
            command=self.save_all_qualifying_scores,
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Refresh Mobile Scores",
            command=self.refresh_qualifying,
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Create / Re-seed Match Play",
            command=self.open_seed_dialog,
        ).pack(side="left", padx=4)

        self.qual_status = ttk.Label(controls, text="")
        self.qual_status.pack(side="right")

        self.qual_grid_container = ttk.Frame(self.qualifying_tab)
        self.qual_grid_container.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        divisions = self.db.divisions()
        if divisions:
            self.qual_division.set(divisions[0])
            self.refresh_qualifying()

    def refresh_qualifying(self):
        division = self.qual_division.get()
        if not division:
            return

        for child in self.qual_grid_container.winfo_children():
            child.destroy()

        self.qualifying_entries = {}
        self.qualifying_labels = {}

        games = self.db.qualifying_games
        rows = self.db.qualifying_rows(division)
        cut = self.db.cut_size(division)

        self.cut_var.set(str(cut))

        scroll = ScrollableFrame(self.qual_grid_container)
        scroll.pack(fill="both", expand=True)
        grid = scroll.inner

        headers = ["Rank", "Bowler"]
        headers += [f"G{i}" for i in range(1, games + 1)]
        headers += ["Total", "Avg", "Cut"]

        for col, title in enumerate(headers):
            ttk.Label(
                grid,
                text=title,
                font=("", 10, "bold"),
                anchor="center",
                padding=(4, 4),
            ).grid(row=0, column=col, sticky="nsew", padx=1, pady=1)

        entry_order = []

        for r_idx, row in enumerate(rows, start=1):
            ttk.Label(
                grid,
                text=str(row["rank"]),
                width=5,
                anchor="center",
            ).grid(row=r_idx, column=0, sticky="nsew", padx=1, pady=1)

            name = f"{row['first_name']} {row['last_name']}"
            ttk.Label(
                grid,
                text=name,
                width=25,
                anchor="w",
            ).grid(row=r_idx, column=1, sticky="nsew", padx=1, pady=1)

            for game_no in range(1, games + 1):
                value = row["scores"][game_no - 1]
                var = tk.StringVar(value="" if value is None else str(value))

                entry = ttk.Entry(
                    grid,
                    textvariable=var,
                    width=6,
                    justify="center",
                )
                entry.grid(
                    row=r_idx,
                    column=1 + game_no,
                    sticky="nsew",
                    padx=1,
                    pady=1,
                )

                key = (row["bowler_id"], game_no)
                self.qualifying_entries[key] = (var, entry)
                entry_order.append(key)

            total_col = 2 + games
            avg_col = 3 + games
            cut_col = 4 + games

            total_label = ttk.Label(
                grid,
                text=str(row["total"]),
                width=8,
                anchor="center",
            )
            total_label.grid(
                row=r_idx, column=total_col, sticky="nsew", padx=1, pady=1
            )

            avg_text = (
                f"{row['average']:.2f}"
                if row["average"] is not None
                else ""
            )

            avg_label = ttk.Label(
                grid,
                text=avg_text,
                width=8,
                anchor="center",
            )
            avg_label.grid(
                row=r_idx, column=avg_col, sticky="nsew", padx=1, pady=1
            )

            cut_text = "CUT" if row["complete"] and row["rank"] <= cut else ""
            cut_label = ttk.Label(
                grid,
                text=cut_text,
                width=7,
                anchor="center",
            )
            cut_label.grid(
                row=r_idx, column=cut_col, sticky="nsew", padx=1, pady=1
            )

            self.qualifying_labels[row["bowler_id"]] = (
                total_label, avg_label, cut_label
            )

        # Easy keyboard entry:
        # Enter advances to the next score cell.
        for idx, key in enumerate(entry_order):
            var, entry = self.qualifying_entries[key]

            def make_return_handler(index):
                def handler(event):
                    try:
                        self._save_single_qualifying_cell(entry_order[index])
                    except ValueError as exc:
                        messagebox.showerror("Invalid Score", str(exc), parent=self)
                        return "break"

                    next_index = index + 1
                    if next_index < len(entry_order):
                        self.qualifying_entries[
                            entry_order[next_index]
                        ][1].focus_set()
                        self.qualifying_entries[
                            entry_order[next_index]
                        ][1].selection_range(0, "end")
                    else:
                        self.refresh_qualifying()
                    return "break"
                return handler

            entry.bind("<Return>", make_return_handler(idx))

        complete_count = sum(1 for row in rows if row["complete"])
        self.qual_status.configure(
            text=(
                f"{complete_count}/{len(rows)} complete | "
                f"{games} qualifying games"
            )
        )

    def _save_single_qualifying_cell(self, key):
        bowler_id, game_no = key
        var, entry = self.qualifying_entries[key]
        raw = var.get().strip()

        if raw == "":
            self.db.delete_score(bowler_id, game_no)
            return

        try:
            score = int(raw)
        except ValueError:
            raise ValueError("Score must be a whole number from 0 to 300.")

        self.db.set_score(bowler_id, game_no, score)

    def save_all_qualifying_scores(self):
        errors = []

        for key in list(self.qualifying_entries):
            try:
                self._save_single_qualifying_cell(key)
            except ValueError as exc:
                bowler_id, game_no = key
                errors.append(
                    f"{self.db.display_name(bowler_id)}, Game {game_no}: {exc}"
                )

        if errors:
            messagebox.showerror(
                "Score Errors",
                "\n".join(errors[:12]),
                parent=self,
            )
            return False

        self.refresh_qualifying()
        self.refresh_summary()
        return True

    def save_cut_size(self):
        division = self.qual_division.get()
        if not division:
            return

        try:
            self.db.set_cut_size(division, int(self.cut_var.get()))
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Invalid Cut Size", str(exc), parent=self)
            return

        self.refresh_qualifying()
        self.refresh_summary()

    def open_seed_dialog(self):
        if not self.save_all_qualifying_scores():
            return

        division = self.qual_division.get()

        if not self.db.division_scoring_complete(division):
            incomplete = [
                f"{row['first_name']} {row['last_name']}"
                for row in self.db.qualifying_rows(division)
                if not row["complete"]
            ]

            messagebox.showwarning(
                "Qualifying Not Complete",
                (
                    "Every bowler must have a score entered for every "
                    "qualifying game before the cut can be made.\n\n"
                    "Missing: " + ", ".join(incomplete[:15])
                ),
                parent=self,
            )
            return

        try:
            cut_size = int(self.cut_var.get())
            self.db.set_cut_size(division, cut_size)
        except ValueError as exc:
            messagebox.showerror("Invalid Cut Size", str(exc), parent=self)
            return

        existing = self.db.load_bracket(division)
        if existing:
            ok = messagebox.askyesno(
                "Replace Existing Bracket?",
                (
                    "This division already has a match-play bracket. "
                    "Re-seeding will erase its match scores.\n\n"
                    "Continue?"
                ),
                parent=self,
            )
            if not ok:
                return

        SeedDialog(
            self,
            self.db,
            division,
            cut_size,
            lambda ordered_ids: self.create_match_play(
                division, ordered_ids
            ),
        )

    def create_match_play(self, division, ordered_ids):
        state = create_bracket_state(division, ordered_ids)
        self.db.save_bracket(division, state)

        self.match_division.set(division)
        self.refresh_match_play()
        self.refresh_summary()
        self.notebook.select(self.match_tab)

    # --------------------------------------------------------
    # Match play
    # --------------------------------------------------------

    def _build_match_tab(self):
        controls = ttk.Frame(self.match_tab, padding=8)
        controls.pack(fill="x")

        ttk.Label(controls, text="Division:").pack(side="left")

        self.match_division = tk.StringVar()
        self.match_division_combo = ttk.Combobox(
            controls,
            textvariable=self.match_division,
            values=self.db.divisions(),
            state="readonly",
            width=22,
        )
        self.match_division_combo.pack(side="left", padx=(4, 12))
        self.match_division_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.refresh_match_play()
        )

        ttk.Button(
            controls,
            text="Reset / Re-seed From Qualifying",
            command=self.match_reseed,
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Refresh Bracket",
            command=self.refresh_match_play,
        ).pack(side="left", padx=4)

        self.match_status = ttk.Label(controls, text="")
        self.match_status.pack(side="right")

        self.match_container = ttk.Frame(self.match_tab)
        self.match_container.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        divisions = self.db.divisions()
        if divisions:
            self.match_division.set(divisions[0])
            self.refresh_match_play()

    def match_reseed(self):
        division = self.match_division.get()
        if not division:
            return
        self.qual_division.set(division)
        self.refresh_qualifying()
        self.notebook.select(self.qualifying_tab)
        self.open_seed_dialog()

    def refresh_match_play(self):
        division = self.match_division.get()
        if not division:
            return

        for child in self.match_container.winfo_children():
            child.destroy()

        self.match_score_vars = []

        state = self.db.load_bracket(division)

        if not state:
            ttk.Label(
                self.match_container,
                text=(
                    "No match-play bracket has been created for this division.\n"
                    "Complete qualifying, set the cut size, then use "
                    "'Create / Re-seed Match Play' on the Qualifying tab."
                ),
                justify="center",
                padding=30,
            ).pack(expand=True)

            self.match_status.configure(text="No bracket")
            return

        # Save recomputed state in case byes auto-advanced.
        self.db.save_bracket(division, state)
        state = self.db.load_bracket(division)

        scroll = ScrollableFrame(self.match_container)
        scroll.pack(fill="both", expand=True)
        bracket_frame = scroll.inner

        seed_by_bowler = {
            bowler_id: int(seed)
            for seed, bowler_id in state.get("seed_map", {}).items()
            if bowler_id
        }

        for r_idx, round_matches in enumerate(state["rounds"]):
            column = ttk.Frame(bracket_frame, padding=(8, 8))
            column.grid(
                row=0,
                column=r_idx,
                sticky="n",
                padx=10,
                pady=6,
            )

            ttk.Label(
                column,
                text=bracket_round_name(len(round_matches)),
                font=("", 12, "bold"),
            ).pack(pady=(0, 10))

            spacer_multiplier = 2 ** r_idx

            for m_idx, match in enumerate(round_matches):
                if m_idx > 0:
                    ttk.Frame(
                        column,
                        height=20 * spacer_multiplier,
                    ).pack()

                self._render_match_box(
                    column,
                    division,
                    state,
                    r_idx,
                    m_idx,
                    match,
                    seed_by_bowler,
                )

        champion = state["rounds"][-1][0].get("winner")
        if champion:
            self.match_status.configure(
                text=f"Champion: {self.db.display_name(champion)}"
            )
        else:
            self.match_status.configure(
                text=f"Cut: {state['cut_size']} | Bracket: {state['bracket_size']}"
            )

    def _render_match_box(
        self,
        parent,
        division,
        state,
        round_index,
        match_index,
        match,
        seed_by_bowler,
    ):
        box = ttk.LabelFrame(
            parent,
            text=f"Match {match_index + 1}",
            padding=8,
        )
        box.pack(fill="x", pady=4)

        p1 = match.get("p1")
        p2 = match.get("p2")

        self._render_match_participant(
            box,
            division,
            round_index,
            match_index,
            slot=1,
            bowler_id=p1,
            seed=seed_by_bowler.get(p1),
            score=match.get("score1"),
        )

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=4)

        self._render_match_participant(
            box,
            division,
            round_index,
            match_index,
            slot=2,
            bowler_id=p2,
            seed=seed_by_bowler.get(p2),
            score=match.get("score2"),
        )

        winner = match.get("winner")

        if winner:
            ttk.Label(
                box,
                text=f"Winner: {self.db.display_name(winner)}",
                font=("", 9, "bold"),
            ).pack(anchor="w", pady=(6, 0))
        elif (
            p1 and p2
            and match.get("score1") is not None
            and match.get("score2") is not None
            and match.get("score1") == match.get("score2")
        ):
            ttk.Label(
                box,
                text="Tie — choose the tiebreak winner:",
            ).pack(anchor="w", pady=(6, 2))

            tie_buttons = ttk.Frame(box)
            tie_buttons.pack(fill="x")

            ttk.Button(
                tie_buttons,
                text=self.db.display_name(p1),
                command=lambda: self.advance_tie_winner(
                    division, round_index, match_index, p1
                ),
            ).pack(side="left", padx=(0, 4))

            ttk.Button(
                tie_buttons,
                text=self.db.display_name(p2),
                command=lambda: self.advance_tie_winner(
                    division, round_index, match_index, p2
                ),
            ).pack(side="left")

    def _render_match_participant(
        self,
        parent,
        division,
        round_index,
        match_index,
        slot,
        bowler_id,
        seed,
        score,
    ):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)

        if bowler_id:
            seed_text = f"#{seed} " if seed else ""
            name = seed_text + self.db.display_name(bowler_id)
        else:
            name = "Waiting / BYE"

        ttk.Label(
            row,
            text=name,
            width=28,
            anchor="w",
        ).pack(side="left")

        var = tk.StringVar(value="" if score is None else str(score))
        entry = ttk.Entry(
            row,
            textvariable=var,
            width=7,
            justify="center",
            state="normal" if bowler_id else "disabled",
        )
        entry.pack(side="right")

        self.match_score_vars.append(var)

        def save_match_score(event=None):
            try:
                self.db.set_match_score(
                    division,
                    round_index,
                    match_index,
                    slot,
                    var.get().strip(),
                )
            except ValueError as exc:
                messagebox.showerror(
                    "Invalid Match Score", str(exc), parent=self
                )
                return "break"

            self.refresh_match_play()
            self.refresh_summary()
            return "break"

        entry.bind("<Return>", save_match_score)
        entry.bind("<FocusOut>", lambda e: None)

    def advance_tie_winner(
        self, division, round_index, match_index, bowler_id
    ):
        self.db.set_manual_winner(
            division, round_index, match_index, bowler_id
        )
        self.refresh_match_play()
        self.refresh_summary()

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    def _build_summary_tab(self):
        controls = ttk.Frame(self.summary_tab, padding=8)
        controls.pack(fill="x")

        ttk.Button(
            controls,
            text="Refresh Summary",
            command=self.refresh_summary,
        ).pack(side="left")

        self.summary_tree = ttk.Treeview(
            self.summary_tab,
            columns=(
                "division", "field", "games", "complete",
                "cut", "bracket", "champion"
            ),
            show="headings",
            height=12,
        )

        headings = {
            "division": "Division",
            "field": "Field",
            "games": "Games",
            "complete": "Qualifying",
            "cut": "Cut",
            "bracket": "Bracket",
            "champion": "Champion",
        }

        widths = {
            "division": 160,
            "field": 70,
            "games": 70,
            "complete": 110,
            "cut": 70,
            "bracket": 100,
            "champion": 200,
        }

        for col in self.summary_tree["columns"]:
            self.summary_tree.heading(col, text=headings[col])
            self.summary_tree.column(col, width=widths[col], anchor="center")

        self.summary_tree.pack(
            fill="both", expand=True, padx=8, pady=(0, 8)
        )

        self.refresh_summary()

    def refresh_summary(self):
        if not hasattr(self, "summary_tree"):
            return

        for item in self.summary_tree.get_children():
            self.summary_tree.delete(item)

        games = self.db.qualifying_games

        for division in self.db.divisions():
            field_size = len(self.db.bowlers(division))
            complete = self.db.division_scoring_complete(division)
            cut = self.db.cut_size(division)
            state = self.db.load_bracket(division)

            champion = ""
            bracket_text = "Not created"

            if state:
                bracket_text = f"{state['bracket_size']}-slot"
                final = state["rounds"][-1][0]
                if final.get("winner"):
                    champion = self.db.display_name(final["winner"])

            self.summary_tree.insert(
                "",
                "end",
                values=(
                    division,
                    field_size,
                    games,
                    "Complete" if complete else "In progress",
                    cut,
                    bracket_text,
                    champion,
                ),
            )

    # --------------------------------------------------------
    # Settings / export
    # --------------------------------------------------------

    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Tournament Settings")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="Qualifying games:",
        ).grid(row=0, column=0, sticky="w", pady=6)

        games_var = tk.StringVar(value=str(self.db.qualifying_games))
        games_spin = ttk.Spinbox(
            body,
            textvariable=games_var,
            from_=1,
            to=MAX_QUALIFYING_GAMES,
            width=8,
        )
        games_spin.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=6)

        ttk.Label(
            body,
            text=(
                "Changing the number of games does not erase stored scores. "
                "Games above the current setting are simply ignored until "
                "you increase it again."
            ),
            wraplength=430,
        ).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        def save():
            try:
                self.db.qualifying_games = int(games_var.get())
            except ValueError as exc:
                messagebox.showerror("Invalid Setting", str(exc), parent=win)
                return

            win.destroy()
            self.refresh_all()

        ttk.Button(
            body,
            text="Save",
            command=save,
        ).grid(row=2, column=1, sticky="e", pady=(8, 0))

        ttk.Button(
            body,
            text="Cancel",
            command=win.destroy,
        ).grid(row=2, column=0, sticky="e", pady=(8, 0))

    def export_results(self):
        folder = filedialog.askdirectory(
            parent=self,
            title="Choose folder for tournament exports",
        )

        if not folder:
            return

        paths = self.db.export_results(folder)

        messagebox.showinfo(
            "Export Complete",
            "Created:\n\n" + "\n".join(str(p) for p in paths),
            parent=self,
        )

    def refresh_all(self):
        self.refresh_qualifying()
        self.refresh_match_play()
        self.refresh_summary()

    def on_close(self):
        self.db.close()
        self.destroy()


# ============================================================
# Startup
# ============================================================

def choose_roster_file(root):
    return filedialog.askopenfilename(
        parent=root,
        title="Choose all_divisions.csv",
        filetypes=[
            ("CSV files", "*.csv"),
            ("All files", "*.*"),
        ],
    )


def resolve_database_path(roster_path, explicit_db=None):
    if explicit_db:
        return Path(explicit_db)

    roster_path = Path(roster_path)
    return roster_path.with_name(
        roster_path.stem + "_tournament.sqlite3"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roster",
        nargs="?",
        help="Path to all_divisions.csv",
    )
    parser.add_argument(
        "--db",
        help="Optional path for tournament SQLite database",
    )
    args = parser.parse_args()

    hidden_root = tk.Tk()
    hidden_root.withdraw()

    roster_path = args.roster

    if not roster_path:
        roster_path = choose_roster_file(hidden_root)

    if not roster_path:
        hidden_root.destroy()
        return

    roster_path = Path(roster_path)

    if not roster_path.exists():
        messagebox.showerror(
            "Roster Not Found",
            f"Could not find:\n{roster_path}",
            parent=hidden_root,
        )
        hidden_root.destroy()
        return

    db_path = resolve_database_path(roster_path, args.db)
    db_exists = db_path.exists()

    db = TournamentDB(db_path)

    try:
        if db_exists and db.roster_loaded():
            old_roster = db.get_meta("roster_path", "")

            resume = messagebox.askyesnocancel(
                "Existing Tournament Found",
                (
                    f"A saved tournament database already exists:\n\n"
                    f"{db_path}\n\n"
                    "Yes = Resume it\n"
                    "No = Start over using the current roster\n"
                    "Cancel = Exit"
                ),
                parent=hidden_root,
            )

            if resume is None:
                db.close()
                hidden_root.destroy()
                return

            if resume is False:
                db.import_roster(roster_path)

        else:
            db.import_roster(roster_path)

    except Exception as exc:
        db.close()
        messagebox.showerror(
            "Could Not Load Tournament",
            str(exc),
            parent=hidden_root,
        )
        hidden_root.destroy()
        return

    hidden_root.destroy()

    app = TournamentApp(db, roster_path)
    app.mainloop()


if __name__ == "__main__":
    main()
