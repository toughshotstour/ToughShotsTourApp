# Project Structure

The project is organized so local tournament operations, reusable processing logic, the Tournament Manager, and the Render service are easy to find without changing the normal launch workflow.

```text
ToughShotsApp/
├── app.py
├── desktop/
│   └── app.py
├── core/
│   ├── import_archive.py
│   ├── lane_scoring.py
│   ├── local_demographics.py
│   └── results_portal.py
├── processors/
│   ├── payment_check.py
│   ├── compare_paid_demographics.py
│   └── make_tournament_divisions.py
├── tournament/
│   └── bowling_tournament_manager.py
├── cloud/
│   ├── main.py
│   └── requirements.txt
├── docs/
│   ├── CLOUD_MOBILE_SETUP.md
│   ├── PUBLIC_RESULTS_SETUP.md
│   └── PROJECT_STRUCTURE.md
├── render.yaml
├── requirements.txt
├── run_app.bat
└── run_app.command
```

## Runtime data

Tournament data is not source code and stays under `TournamentWorkspace/` at runtime. The active Tournament Manager database remains beside `tournament_divisions/all_divisions.csv`. Completed tournaments moved by the reset function are retained under `TournamentWorkspace/completed_tournaments/`.

## Reloading an active tournament

Use **4 Tournament → Reload Tournament from Workspace** after reopening the desktop suite. It resumes the SQLite tournament database directly, so qualifying scores, cuts, Jr. Gold settings, seeding, bracket state, and match-play results are retained.

## Managing the local bowler database

Open **2 — Bowler Database** and choose **Manage Local Bowlers** to search, add, edit, or remove local records. Changes are written to both `local_demographics.sqlite3` and `demographic_master.csv`. The generated 10-digit Bowler ID is intentionally read-only so archived results remain linked to the same person. Jr. Gold state and manual division overrides can be edited here; the permanent-bowler sync carries those values to the private cloud database.

## Printing

The desktop suite has a shared **Print title** setting on the Lane Assignment + Mobile Scoring page. The value is saved with the active workspace and passed into Tournament Manager. It appears at the top of lane score sheets, qualifying score pages, and match-play bracket pages.

Tournament Manager includes:
- **Print Qualifying Page** on the Qualifying tab for the selected division.
- **Print Current Round - All Divisions** on Match Play. It finds the largest unfinished round anywhere in the tournament and prints only divisions currently in that same round. For example, while any divisions are still in the Round of 16, Round-of-8 divisions are skipped. Each selected division uses four matches (8 bowlers) per page, with 4- or 2-bowler sheets as later rounds shrink.

Generated forms are saved beside the tournament database under `printed_forms/` before being sent to the default printer. If direct printing is unavailable, the PDF is opened/saved for manual printing.
