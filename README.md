# Digital Daily Units

Estimated daily iTunes song sales for the US, Canada, UK, Australia and Brazil. Each country shows its full top 100 iTunes Store sales chart with cover art, an estimated sales number for each position, and Billboard-style movement: last position, peak, days on chart, and NEW and RE marks. A countdown at the top shows when the next chart update is due.

## Files

- `fetch_charts.py` downloads the five sales charts and writes `data.json` and `history.json`.
  - It asks Apple for the chart with `explicit=true`. Without that, Apple leaves explicit songs out and you get around 85 to 97 songs instead of 100.
  - If the sales chart fails for a country, it tries the version without explicit songs, then the streaming chart. If everything fails, the old copy stays.
- `history.json` is the site's memory: for each song, its peak, days on chart, recent daily positions and cover art. Don't delete it, or the history starts over.
- `index.html` is the site. It reads `data.json` and turns each chart position into an estimate with `4636 × rank^-0.68`, scaled per country.
- `.github/workflows/update.yml` runs the downloader every hour and saves both files.

## Cover art

Every song gets a picture, in this order:

1. The artwork in Apple's chart feed. The page asks Apple for a larger version so it looks sharp.
2. The artwork saved from an earlier run, if the feed leaves it out.
3. Apple's lookup service, asked by the song's iTunes id.
4. If an image still can't load, the page draws a colored cover with the artist's initials, so no row is ever blank.

## The countdown

The site updates at 7 minutes past every hour. The countdown counts down to the next one. When it reaches zero it shows "Updating…" and checks for the new chart every 30 seconds until it arrives, then starts counting again. GitHub sometimes starts the hourly run a few minutes late, so the new chart can take a little longer than the countdown suggests.

To change the minute, edit the `cron` line in `.github/workflows/update.yml` and `UPDATE_MINUTE` in `fetch_charts.py` so they match.

## How the movement columns work

- **Last** is the song's position on the previous day the chart was saved, so moves reflect day-to-day change.
- **Peak** is the best position the song has reached since the site started tracking.
- **Days** counts the days the song has appeared in the top 100. It goes up once per day, however many times the chart is downloaded.
- **NEW** marks a song on the chart for the first time. **RE** marks a song returning after dropping out.

History builds up from the day the site starts running. On day one every song is NEW; from day two the arrows and Last column fill in.

## Put it online for free (GitHub Pages)

1. Create a new public repository on GitHub (for example `digital-daily-units`) and upload all of these files, including the `.github` folder.
2. In the repository, open Settings, then Pages. Under "Build and deployment", choose "Deploy from a branch", pick `main` and `/ (root)`, and save.
3. Open the Actions tab and run "Update charts" once with "Run workflow". This creates `data.json` and `history.json`.
4. After a minute your site is live at `https://YOUR-NAME.github.io/digital-daily-units/`. It refreshes every hour from then on.

If the run fails with a permission error, go to Settings, Actions, General, choose "Read and write permissions" under Workflow permissions, and run it again.

## Try it on your own computer

    python3 fetch_charts.py
    python3 -m http.server 8000

Then open http://localhost:8000.

## Changing the estimates

Open `index.html` and edit the `MARKETS` list near the top of the script. `scale` multiplies the US estimate for that country.
