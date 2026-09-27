# Project Structure

```text
ToughShotsApp/
├── app.py                    # desktop launcher
├── desktop/
│   └── app.py                # six-page desktop workflow
├── core/
│   ├── import_archive.py
│   ├── lane_scoring.py
│   ├── local_demographics.py
│   ├── printing.py
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
├── render.yaml
├── requirements.txt
├── run_app.bat
└── run_app.command
```

## Runtime workspace

Tournament data remains separate from source code:

```text
TournamentWorkspace/
├── local_demographics.sqlite3
├── demographic_master.csv
├── tournament_inputs/
│   ├── tournament_registration.csv
│   └── square_transactions.csv
├── imported_files/
├── payment_status.csv
├── duplicate_review.csv
├── paid_demographic_check.csv
├── tournament_divisions/
│   ├── all_divisions.csv
│   ├── *_tournament.sqlite3
│   └── needs_review.csv
├── lane_scoring/
└── completed_tournaments/
```

`tournament_inputs/` contains the authoritative registration and Square copies for the active event. After files are imported, downstream tournament processing does not use the original source paths.

## Six-page desktop layout

1. Connect to Website
2. Bowler Database
3. Tournament Files
4. Tournament Setup
5. Tournament Manager
6. Public Site

## Active-tournament resume

**Open Tournament Manager** automatically resumes the saved SQLite tournament database when one exists. This retains qualifying scores, cuts, Jr. Gold settings, seeding, brackets, and match-play results after the Tournament Manager window is closed.

## Reset behavior

After the event is archived, **Reset for Next Tournament** moves active tournament-specific files—including `tournament_inputs/`, division outputs, Tournament Manager database, and lane-scoring files—under `completed_tournaments/`. The reusable master bowler database and imported-source archive remain in place.

## Desktop themes
The desktop suite includes a persistent Theme selector in the sidebar with four choices: Light, Slate Dark, Charcoal + Red, and Midnight Blue. The selection is stored as a user-interface preference and is also read by Tournament Manager when it opens.
