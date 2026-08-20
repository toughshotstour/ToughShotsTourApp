# Tough Shots Tournament Suite

A guided desktop application for tournament preparation, division creation, bowling qualifying, match play, lane assignment, printable score sheets, and cloud/mobile QR score entry.

## Main workflow

1. **Payment Check** - reconcile registrations against completed Square payments.
2. **Local Master Bowler Database** - demographic exports are used only to create/update this database; all tournament workflows read demographic information from the database itself.
3. **Division Builder** - create the tournament division rosters.
4. **Tournament Manager** - six-game qualifying, cuts, seeding, match play, brackets, autosave, and exports.
5. **Lanes + Mobile** - randomly balance all bowlers across available lanes, publish lane-pair mobile scoring pages, manage individual scorer PINs, generate QR-coded paper score sheets, and sync phone-entered qualifying scores back to the Tournament Manager database.

The original preparation scripts remain available and the existing Tournament Manager is still the source of truth for tournament scoring/brackets.

## Start the application

### Windows

Double-click `run_app.bat`, or run:

```bash
python app.py
```

### macOS / Linux

Run:

```bash
./run_app.command
```

or:

```bash
python3 app.py
```

## Install desktop dependencies

```bash
python -m pip install -r requirements.txt
```

Tkinter is included with standard Python installations on Windows and macOS. Some Linux distributions package Tkinter separately.

## Cloud/mobile scoring

See **[docs/CLOUD_MOBILE_SETUP.md](docs/CLOUD_MOBILE_SETUP.md)** for the deployment and event-day instructions.

The included cloud service is a FastAPI application in `cloud/`, and `render.yaml` is provided to make deployment as small as possible.

## Lane assignment behavior

Lane assignment balances the **lane-pair scorecards first**, so pair totals differ by no more than one bowler. It then keeps bowlers from the same division together as much as possible. Divisions may share a pair when needed to preserve even scorecard sizes, but bowlers from the same division stay grouped together instead of being interleaved.

Preparing lane scoring creates:

```text
TournamentWorkspace/
└── lane_scoring/
    ├── lane_assignments.csv
    ├── lane_manifest.json
    └── lane_scoresheets.pdf
```

The PDF has one landscape page per lane pair, names pre-filled in a compact bowling-sheet grid, six game columns, a total column, and one shared QR code that opens both lanes on the mobile scoring page. Position letters run sequentially across the pair: the odd lane receives the first half rounded up and the even lane receives the remaining letters. The public results URL is printed immediately below the competitor table. Scorers authenticate with individual six-digit PINs; scores remain editable and changes are audit-attributed.



## Resume a saved tournament

Tournament Manager scores, cuts, Jr. Gold settings, seeds, brackets, and match-play state are stored in the active workspace SQLite database. If you accidentally close Tournament Manager, reopen the desktop suite and go to **4 Tournament → Reload Tournament from Workspace**. The suite detects the active `all_divisions.csv`, its saved tournament database, lane manifest, lane score sheets, event name, and lane count, then opens the manager directly in resume mode.

The reload button resumes only the **active** workspace. Tournaments moved by **Reset for Next Tournament** remain preserved under `TournamentWorkspace/completed_tournaments/`.

## Project layout

The source tree is organized by responsibility:

```text
ToughShotsApp/
├── app.py                    # desktop launcher
├── desktop/                  # desktop suite UI
├── core/                     # lane scoring, demographics, archive, cloud publishing helpers
├── processors/               # payment, demographic matching, and division scripts
├── tournament/               # Tournament Manager and local SQLite scoring logic
├── cloud/                    # Render/FastAPI service
├── docs/                     # setup and workflow documentation
├── render.yaml               # Render Blueprint
├── requirements.txt
├── run_app.bat
└── run_app.command
```

## Automatic copies of imported files

Whenever a desktop workflow consumes a file, the app first preserves an untouched copy in the selected tournament workspace. This applies to payment checking, demographic matching, division building, lane assignment, and opening a roster in the Tournament Manager. A full prep run archives the registration, Square export, and the local demographic snapshot that was actually used for matching.

Each run creates a timestamped folder such as:

```text
TournamentWorkspace/
└── imported_files/
    └── 2026-08-17_213945_full_prep_pipeline/
        ├── tournament_registration__registrations.csv
        ├── square_transactions__transactions.csv
        ├── demographic_form__demographics.csv
        └── import_manifest.json
```

`import_manifest.json` records the original path, file size, and SHA-256 hash of every preserved input. The app stops the requested operation if it cannot make the archive copy, so a file is never processed silently without the backup being created. Use **Open Imported Files** on the Tournament Prep page to open this archive quickly.

## Existing prep workspace

A full preparation run still creates files similar to:

```text
TournamentWorkspace/
├── payment_status.csv
├── duplicate_review.csv
├── paid_demographic_check.csv
└── tournament_divisions/
    ├── all_divisions.csv
    ├── U12_Mixed.csv
    ├── U14_Boys.csv
    ├── U14_Girls.csv
    ├── U16_Boys.csv
    ├── U16_Girls.csv
    ├── U18_Boys.csv
    ├── U18_Girls.csv
    └── needs_review.csv
```

The Tournament Manager stores its local SQLite database next to `all_divisions.csv` by default. Mobile score synchronization writes into that same database.


## Permanent bowlers and public results

The Render service now also hosts a public Tough Shots landing page with qualifying standings for all seven divisions, Bowler-of-the-Year pages, and a historical tournament archive. The desktop application's **6 Bowlers + Results** page manages the private permanent bowler database, Jr. Gold status, one-click qualifying publication, and end-of-tournament archive/season uploads. See `docs/PUBLIC_RESULTS_SETUP.md` for the workflow.

### Added results features
- Automatic Bowler-of-the-Year points: reverse qualifying placement points + 5 per match win + champion/runner-up bonuses.
- Separate Jr. Gold qualifying standings for bowlers marked JG or Q, including independent cut lines and optional U14/U16/U18 boys+girls merges.
- Qualifying score entry by lane in Tournament Manager, alongside the existing division list.


## Reusable local demographics and tournament reset

The demographic form is only an import/update source. In **2 Bowler Database**, import a new demographic export only when needed. The authoritative source is `local_demographics.sqlite3`; tournament entry checks, roster enrichment, division placement, Jr. Gold information, and permanent-bowler sync read directly from that database. `demographic_master.csv` is retained only as a convenient human-readable export.

After a tournament has been published to the cloud archive, use **6 Bowlers + Results → Reset for Next Tournament**. The button moves the current payment, division, Tournament Manager database, and lane-scoring files into a timestamped `completed_tournaments` folder and clears the active tournament selections. It deliberately preserves `local_demographics.sqlite3`, `demographic_master.csv`, `imported_files`, cloud permanent bowlers/Jr. Gold states, scorer PINs, and the public archive.
