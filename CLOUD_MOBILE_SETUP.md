# Tough Shots Cloud + QR Mobile Scoring Setup

This version keeps the desktop application as the tournament control center and adds a small cloud scoring service for phones.

## What you need

- Python on the tournament computer.
- A GitHub or GitLab repository containing this `ToughShotsApp` folder.
- A Render account.
- Internet access on the scoring phones and the tournament computer.

The included `render.yaml` is configured for one small Render web service with a 1 GB persistent disk. The persistent disk is intentional: the mobile service stores scores in SQLite, and only files written beneath the disk mount survive service restarts/redeploys. The database path is `/var/data/toughshots_cloud.sqlite3`.

## 1. Install the desktop dependencies

From the `ToughShotsApp` folder:

```bash
python -m pip install -r requirements.txt
```

This installs pandas plus the PDF/QR libraries used for score sheets.

## 2. Put the project in a Git repository

Create a repository on GitHub or GitLab and upload/commit this `ToughShotsApp` folder so that `render.yaml` is at the repository root.

The desktop files do not run on Render; the Blueprint uses `rootDir: cloud`, so only the `cloud` folder is built for the web service.

## 3. Deploy the mobile scoring service on Render

1. Sign in to Render.
2. Create a new **Blueprint** and connect the repository containing this project.
3. Render will read `render.yaml`.
4. During the initial Blueprint setup, Render will ask for `TOUGHSHOTS_ADMIN_KEY` because it is marked `sync: false`.
5. Enter a long private value you can copy into the desktop app, for example a password-manager-generated random string. Do not print this key on score sheets or share it with scorers.
6. Create/apply the Blueprint and wait for the service to become healthy.
7. Copy the public HTTPS address Render gives the service, such as `https://your-service-name.onrender.com`.

Useful official references:

- Render FastAPI deployment: https://render.com/docs/deploy-fastapi
- Render Blueprint format: https://render.com/docs/blueprint-spec
- Render persistent disks: https://render.com/docs/disks

## 4. Prepare the tournament as usual

Run the normal Tough Shots preparation pipeline and create `all_divisions.csv`.

On **5  Lanes + Mobile**:

1. Confirm the tournament roster.
2. Enter a tournament name.
3. Enter the number of bowling lanes available.
4. Paste the Render HTTPS URL.
5. Paste the same admin key you entered during Render setup.
6. Click **Prepare Lane Scoring**.

The application will:

- randomly shuffle all bowlers without considering division, age, or gender;
- distribute them across the available lanes as evenly as mathematically possible;
- publish one secure mobile scoring page per lane;
- create `lane_assignments.csv`;
- create `lane_manifest.json`;
- create `lane_scoresheets.pdf` with one sheet per lane and six games per bowler.

For example, 65 bowlers on 5 lanes results in exactly 13 bowlers per lane. If the division is not exact, lane sizes differ by at most one bowler.

## 5. Print and use the lane score sheets

Open `lane_scoresheets.pdf` from the desktop app and print it.

Each page contains:

- the lane number;
- the bowlers assigned to that lane;
- Game 1 through Game 6 boxes;
- a total column;
- a lane-specific QR code.

A scorer scans the QR code with a phone camera. No app installation is required. The browser page shows only that lane's bowlers and six score fields per bowler.

The QR code contains a long random lane token, not the cloud admin key. Treat the printed sheet as permission to score that lane.

## 6. Multiple phones

Different lanes can be scored at the same time.

If two phones open the same lane, the server uses a lane revision number. After one phone saves, an older copy cannot silently overwrite it. The older phone receives a message telling the scorer to review the newest values and submit again.

Every changed score is also written to a cloud audit table with the old score, new score, lane, bowler, game, and submission time.

## 7. Bring mobile scores into the Tournament Manager

On **5  Lanes + Mobile**, click **Sync Mobile Scores**, or enable:

**Auto-sync cloud scores into the Tournament Manager database every 15 seconds**

The desktop app writes the received six-game qualifying scores into the same SQLite database used by the existing Tournament Manager.

If Tournament Manager is already open, go to its **Qualifying** tab and click **Refresh Mobile Scores** to redraw the screen from the database.

## 8. Reassigning lanes

Running **Prepare Lane Scoring** again creates a fresh random lane draw. The desktop app warns before doing this.

A new draw:

- preserves the tournament's internal ID;
- generates new lane tokens;
- invalidates the old QR sheets;
- replaces the cloud lane assignment;
- clears existing cloud qualifying scores for that tournament.

Print the newly generated PDF after reassigning lanes.

## Files created locally

Inside the selected tournament workspace:

```text
lane_scoring/
├── lane_assignments.csv
├── lane_manifest.json
└── lane_scoresheets.pdf
```

The cloud service stores its own database at `/var/data/toughshots_cloud.sqlite3` on the Render persistent disk.

## Security notes

- Keep `TOUGHSHOTS_ADMIN_KEY` private. It is used only by the desktop app for publishing assignments and downloading scores.
- The QR code token grants scoring access only to its lane. Do not post score-sheet QR codes publicly.
- Use the HTTPS Render URL for real events. HTTP is accepted only to make local testing easier.
- The included Render configuration uses a persistent disk because the cloud service uses SQLite. Do not remove the disk unless you also move the cloud data layer to a persistent database service.
