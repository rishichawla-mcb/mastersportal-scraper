# Mastersportal scraper app

A small web app for collecting Mastersportal's Master's programme and university listings, behind a login.

- **Dashboard:** start, stop and resume scrapes; progress bar; live log; counts by country; job history.
- **Coverage:** API-listed vs stored, per country and per section, with every short section named and sized.
- **Changes:** what actually changed between runs (tuition, duration, degree, location, ranking…), viewable and downloadable.
- **Browse:** programmes and universities, with search and a country filter.
- **CSV export:** everything or a single country, plus the change log.
- **Users:** Admin (runs scrapes, manages users) and Viewer (browse and download only). Passwords are hashed, the login locks after 5 failed tries, and every form is CSRF-protected.

The stack is Python 3, Flask, Waitress and SQLite in one process. A 512 MB–1 GB Ubuntu VPS is enough (see "Measured figures" below).

## How a scrape works

1. **Plan:** one section per country, using the API's own country counts.
2. **Split:** a section listing more than 400 programmes is split on the next filter that provably covers all of it: delivery mode, then full/part-time, then degree type, then discipline. A filter with a blank bucket (e.g. programmes with no degree type set) is never used, because the blank bucket can't be fetched and those records would be lost.
3. **Fetch:** page by page, 20 records each. Progress is saved after **every page**, so a stop, a block, a restart or a deploy costs at most one page.
4. **Check:** when a section ends, unique records are compared with the API's count. A section that came back short (the site's sort order shifts between requests) is marked **short** and read again, up to 3 passes. If it's still short after that, it's finished but the gap is recorded and shown on the Coverage page. It is never silently "done".
5. **Store:** existing records are merged, never blanked: an empty incoming value never erases a stored one. A change is logged only when a content field really differs. A record's first sighting, ratings, review counts and "featured" flags are not changes.

**Non-answers are named, not stored:** HTTP errors, a "200 OK" that isn't a usable result (challenge page, missing results), and an empty page before the end are all retried. None of them is ever recorded as "this country has nothing".

**Brakes:** one job at a time, a 1-second minimum delay, 6 retries with growing back-off, and a circuit breaker that stops the run if more than 20% of the last 50 requests failed, even if they were scattered.

**Auto-resume** (on by default): when the brakes stop a run, it pauses 45 minutes (status **cooling**), then carries on 1.5× slower, up to 4 times. It never speeds up, rotates IPs or disguises itself; if the site still refuses, the job ends **blocked**.

## Install on the VPS (Ubuntu 22.04 or 24.04)

1. **Optional:** point a subdomain at the VPS IP, for example an `A` record for `scraper.educationvibes.in`. With a domain, the installer sets up HTTPS for free.
2. On the VPS:
   ```
   git clone https://github.com/rishichawla-mcb/mastersportal-scraper.git
   cd mastersportal-scraper
   ```
   (private repo: use your GitHub username and a personal access token as the password)
3. To keep the data you've already scraped, copy it in from your PC first (PowerShell):
   ```
   scp D:\py\mastersportal.sqlite root@YOUR_VPS_IP:/root/mastersportal-scraper/
   ```
4. Install and create your login:
   ```
   sudo bash deploy/install.sh scraper.educationvibes.in      # or with no argument for http://IP
   sudo mpscraper-manage create-user you@example.com --role admin
   ```
5. Open `https://scraper.educationvibes.in` (or `http://YOUR_VPS_IP`) and log in.
6. Edit the contact email that the scraper sends to the site:
   ```
   sudo nano /etc/mpscraper.env        # change you@example.com in MP_USER_AGENT
   sudo systemctl restart mpscraper
   ```

Without a domain, the login runs over plain HTTP, so your password travels unencrypted. Use a domain if you can.

## Using it

- **Start / resume** continues from the last saved page. Finished sections are skipped.
- **Fresh run** re-plans and re-reads everything to refresh the data. Records are updated, never deleted; the CSVs show `first_seen`, `last_seen` and `last_changed`.
- **Delay:** 4 seconds is a safe default.
- **Coverage page:** check it after a run. A sum of "short" gaps tells you exactly how much the site's shifting sort order cost.
- **Same server, always:** the site converts tuition into the visitor's currency, so scraping from a different country makes every fee look "changed".
- **Don't deploy or restart during a run** if you can avoid it. Nothing is lost, but the current page is fetched again.

## Measured figures

Measured on a test database of 95,000 realistic programme records (19 Nov 2026), not estimated:

| what | measured |
|---|---|
| app memory, idle | 40 MB |
| app memory after browsing and a full 95,000-row CSV export | 95 MB |
| database size for 95,000 programmes | 150 MB (1.54 KB per programme) |
| full programme CSV export | 2.1 s, 26 MB |
| programme list page / search | 0.15 s / 0.45 s |

**Inferred, not yet measured:**
- **Total run time.** About 95,000 ÷ 20 = 4,750 pages × (4 s delay + response time), roughly 6–8 hours. Splitting overlaps and re-reads of short sections add to that by an unknown amount. Settle it with the dashboard's request count after the first full run.
- **How much the sort order loses per pass.** One measured data point so far: the first US university section returned 1,004 unique of 1,095 listed (92%). The Coverage page measures it for every section.
- **Size of the change log on refresh runs.**

## Day-to-day commands (on the VPS)

| What | Command |
|---|---|
| App status / logs | `systemctl status mpscraper` · `journalctl -u mpscraper -f` |
| Restart | `sudo systemctl restart mpscraper` |
| Add user / reset password | `sudo mpscraper-manage create-user EMAIL --role viewer` · `sudo mpscraper-manage reset-password EMAIL` |
| Back up the data | `sudo sqlite3 /var/lib/mpscraper/mastersportal.sqlite ".backup /root/mp-backup.sqlite"` (install with `apt install sqlite3`) |
| Update the app | `cd mastersportal-scraper && git pull && sudo bash deploy/install.sh scraper.educationvibes.in` (keeps your data and settings) |

## Settings (`/etc/mpscraper.env`)

All optional. Restart the service after changing them.

`MP_SPLIT_ABOVE` (400) · `MP_MAX_PASSES` (3) · `MP_COOLDOWN` seconds (2700) · `MP_MAX_AUTO_RESUMES` (4) · `MP_BREAKER_WINDOW` (50) · `MP_USER_AGENT`

## Files

`app.py` (web app), `scraper.py` (scrape engine; the design rules are listed at its top), `db.py` (SQLite schema with additive migrations; opens the CLI script's `mastersportal.sqlite` as is and treats its finished sections as done), `exports.py` (CSV columns), `manage.py` (command-line user admin), `wsgi.py` (production server), `deploy/` (installer, systemd unit), `tests/` (24 tests; run with `pip install pytest && pytest -q`; they drive the real runner through a fake API, no network).

Data lives in `/var/lib/mpscraper/mastersportal.sqlite`. Secrets and settings live in `/etc/mpscraper.env`, readable by root only.
