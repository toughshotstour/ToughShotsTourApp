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
