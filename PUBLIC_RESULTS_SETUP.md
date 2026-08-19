# Tough Shots Permanent Bowlers + Public Results

This build extends the existing Render mobile-scoring service into the public Tough Shots results site while keeping the permanent bowler database and scorer/lane administration private.

## Deploy the update

1. Replace the files in your GitHub repository with this build (keep your `.gitignore`; do not upload `TournamentWorkspace`, CSV exports, databases, or secrets).
2. Commit/push the changes to the same branch connected to Render.
3. Let Render redeploy the existing service. **Do not create a second Render service.** The same persistent disk/database is reused and the new tables are created automatically.
4. Keep the existing `TOUGHSHOTS_ADMIN_KEY` unchanged unless you intentionally want to rotate it.
5. Open your normal Render URL. The `/` route is now the Tough Shots public landing page.

Existing scorer PINs, lane data, and score audit information remain in the existing cloud SQLite database because this update adds tables rather than replacing the database file.

## Permanent bowler database

Open **6 Bowlers + Results** in the desktop application.

First update the reusable local demographic database in **2 Demographics** whenever you receive a newer demographic export. On **6 Bowlers + Results**, click **Sync Permanent Bowlers from Local DB** to push that private local demographic snapshot to the permanent cloud bowler database.

The importer looks for:
- Bowlers First Name (or a common First Name variation)
- Bowlers Last Name (or a common Last Name variation)
- Date of birth
- Gender
- a column with `USBC` in its heading

The cloud record stores First Name, Last Name, Gender, Birthdate, Division, Bowler ID, and Jr. Gold state. The full bowler list is available only through the admin-key API and the desktop manager; there is no public permanent-bowler directory.

### Bowler ID rule

The numeric USBC ID is padded **on the right** with zeros until it is 10 digits.

Example: `12345` -> `1234500000`

If that 10-digit Bowler ID is already assigned to a different bowler, the last digit advances for the next record:
- first: `1234500000`
- second: `1234500001`
- third: `1234500002`

Re-importing the same bowler (same normalized name + birthdate) updates their demographic fields without assigning a new Bowler ID.

## Jr. Gold status

Click **Manage Bowler JG / Q Status**. Select a bowler and choose:
- **Set Blank** — not trying to qualify for Jr. Gold
- **Set JG** — trying to qualify for Jr. Gold
- **Set Q** — already qualified for Jr. Gold

This is manual and is preserved when later demographic forms are imported.

## Public qualifying standings

After scores are current in the local Tournament Manager, click **Push Current Qualifying Standings**.

That one click publishes the current qualifying ranks, all six games, total, and average for every division. The public site provides:
- `/standings`
- U12 Mixed
- U14 Boys
- U14 Girls
- U16 Boys
- U16 Girls
- U18 Boys
- U18 Girls

Re-click the button any time you want to refresh the public standings.

## Tournament archive + Bowler of the Year data

At the end of the tournament, make sure qualifying and match-play results are final locally, then click **Archive Tournament + Update BOY Data**.

This performs two actions in one operation:
1. publishes the latest qualifying standings;
2. stores qualifying performance and match-play performance as a permanent FINAL tournament record.

The archive page lists past tournaments, allows visitors to open a tournament's final results, and has a name search so a bowler can see all archived performances matching their name.

The Bowler-of-the-Year pages already aggregate event count, qualifying average, high game, and match wins by division. **BOY points intentionally display `TBD` until the official points key is provided.** The raw tournament performance is already stored, so the formula can be added later without re-entering old results.

## Event date

On **6 Bowlers + Results**, set Event date as `YYYY-MM-DD` before publishing/archiving, for example `2026-08-18`.

## Privacy

Public:
- Tough Shots landing page
- qualifying standings
- Bowler-of-the-Year performance pages
- archived tournament results and name-based history search

Private/admin-key protected:
- permanent bowler database
- Birthdate/Gender/Bowler-ID database management
- Jr. Gold status management
- scorer PIN administration
- score publishing/archive APIs

Lane-pair QR pages remain unlisted tokenized URLs and still require an authorized scorer PIN before scores can be edited.

## Bowler of the Year points

The Bowler-of-the-Year formula is now built in and is calculated when a tournament is archived:

- Qualifying: last place in each regular division earns 1 point, next-to-last earns 2, continuing by one point per place through first.
- Match play: 5 points for every match won.
- Runner-up: 10 additional bonus points, separate from match-win points.
- Champion: 20 additional bonus points, separate from match-win points.

Older archived performances are backfilled with this formula when the updated cloud service starts.

## Jr. Gold qualifying

Jr. Gold qualifying is separate from regular qualifying/match-play cuts.

1. In the private Permanent Bowlers manager, mark eligible bowlers `JG` or `Q`.
2. Open Tournament Manager and click **Jr. Gold Settings** on the Qualifying tab.
3. Set separate Jr. Gold cut sizes.
4. Optionally merge Boys + Girls for U14, U16, and/or U18. These merges affect only Jr. Gold standings. U12 remains Mixed.
5. Back in the main Tough Shots app, click **Push Jr. Gold Standings**.

The public Render homepage includes **Jr. Gold Qualifying**. Its pages show only JG/Q bowlers, current six-game scores, rank, and the configured Jr. Gold cut line. The private permanent-bowler database is never exposed publicly.

## Score entry by lane

Tournament Manager now keeps its existing division/list score-entry grid and adds **Enter by Lane**. The lane-entry window uses the `lane_manifest.json` created by **Prepare Lane Scoring**. Enter a lane number to load only the bowlers assigned to that lane, edit Games 1-6, and save. Both entry modes write to the same tournament database.


## Reset after archiving

After you have verified and archived a completed tournament, click **Reset for Next Tournament** on **6 Bowlers + Results**. Tournament-specific local outputs are moved into `TournamentWorkspace/completed_tournaments/<timestamp>_<event>/` rather than deleted. The reusable local demographic database, imported-file archive, permanent cloud bowlers, Jr. Gold states, scorer PINs, and Render archive remain intact.
