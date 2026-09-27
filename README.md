# Tough Shots Tournament Suite

A desktop tournament-operations application plus a Render-hosted public/mobile scoring service for Tough Shots bowling events.

## Desktop workflow

The main application is organized into six pages:

1. **Connect to Website** — enter the Render website URL and private admin key. Use **Test Website Connection** to verify the credentials.
2. **Bowler Database** — import a demographic form to update the reusable local master database, download/merge the private website database, manage local bowlers, sync permanent bowlers to Render, and manage Jr. Gold status.
3. **Tournament Files** — choose the tournament registration and Square transaction exports and click **Import Tournament Files to Workspace**. The app preserves the originals and makes fixed local copies under `TournamentWorkspace/tournament_inputs/`.
4. **Tournament Setup** — run the full setup, review payment/demographic/division errors, edit the local registration copy, then create lane groups, build/edit the lane assignment, and generate QR score sheets.
5. **Tournament Manager** — open/resume Tournament Manager, manage the active tournament roster, edit Jr. Gold settings, manage/sync mobile scoring, and use the consolidated Print Center.
6. **Public Site** — publish qualifying, Jr. Gold, and match-play data; open the public site; archive the finished event; clear the live Current Tournament section; and reset the local workspace for the next event.

The local master bowler database is the demographic source of truth. The demographic CSV is only an import/update source.

## Tournament file safety

After **3 Tournament Files → Import Tournament Files to Workspace**, all tournament setup uses these files:

```text
TournamentWorkspace/
└── tournament_inputs/
    ├── tournament_registration.csv
    └── square_transactions.csv
```

The original downloaded file paths are no longer used by payment checking, demographic matching, or division creation. This means moving or deleting the original downloads after import will not break the active tournament.

Every imported source is also preserved under `TournamentWorkspace/imported_files/` with a timestamped manifest and SHA-256 hashes.

## Setup review files

**4 Tournament Setup** provides direct access to the main review outputs:

```text
TournamentWorkspace/
├── payment_status.csv
├── duplicate_review.csv
├── paid_demographic_check.csv
└── tournament_divisions/
    └── needs_review.csv
```

The local registration editor can search by bowler name, sort by first or last name, and pull rows associated with current review/error outputs to the top. Edits are made to the workspace copy, not to the original downloaded registration file. Rerun **Setup Tournament** after correcting the registration.

## Resume a tournament

Tournament Manager saves qualifying, Jr. Gold settings, seeding, brackets, and match play to SQLite. If Tournament Manager is closed accidentally, reopen the Tough Shots suite and click **Open Tournament Manager**. If the active tournament database already exists, the app automatically resumes it instead of rebuilding it.

## Lane assignment and mobile scoring

Lane-pair assignment balances scorecards as evenly as practical while keeping divisions together by default. Optional Lane Groups can force selected bowlers onto the same pair. Once an assignment exists, **Build / Edit Lane Assignment** reopens that saved assignment instead of resetting it.

Generating score sheets creates/publishes the same lane assignment used by mobile scoring and the public lane-assignment page.

```text
TournamentWorkspace/
└── lane_scoring/
    ├── lane_assignments.csv
    ├── lane_manifest.json
    └── lane_scoresheets.pdf
```

## Printing

The Print Center is on **5 Tournament Manager** and contains:

- Qualifying — all divisions
- Jr. Gold qualifying
- Current match-play round

The Tournament Name is used automatically as the title on printed standings, bracket forms, and lane score sheets. Printed qualifying/Jr. Gold standings follow the same table structure as the website, including the blank cut separator and high-game/high-3 summary.

## Public website

The Render site includes Current Tournament, Bowler of the Year, and Tournament Archive sections. Current Tournament contains Qualifying, Jr. Gold Qualifying, Match Play, and Lane Assignments.

See:

- `docs/CLOUD_MOBILE_SETUP.md`
- `docs/PUBLIC_RESULTS_SETUP.md`

## Project layout

```text
ToughShotsApp/
├── app.py
├── desktop/
├── core/
├── processors/
├── tournament/
├── cloud/
├── docs/
├── render.yaml
├── requirements.txt
├── run_app.bat
└── run_app.command
```

## Start the application

### Windows

Double-click `run_app.bat`, or run:

```bash
python app.py
```

### macOS / Linux

```bash
./run_app.command
```

## Install dependencies

```bash
python -m pip install -r requirements.txt
```
