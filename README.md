# Mastersportal scraper app

A small web app for collecting Mastersportal's Master's programme and university listings, behind a login.

- **Dashboard:** start, stop and resume scrapes; progress bar; live log; counts by country; job history.
- **Browse:** programmes and universities, with search and a country filter.
- **CSV export:** everything, or a single country.
- **Users:** Admin (runs scrapes, manages users) and Viewer (browse and download only). Passwords are hashed, the login locks after 5 failed tries, and every form is CSRF-protected.
- **Safety brakes:** one scrape at a time, a minimum 1-second delay between requests, and the job stops itself with the status **blocked** if the site keeps refusing requests. Progress is saved after every page.

The stack is Python 3, Flask, Waitress and SQLite, in one process using about 60 MB of RAM. A 512 MB–1 GB Ubuntu VPS is enough.

## Install on the VPS (Ubuntu 22.04 or 24.04)

1. **Optional:** point a subdomain at the VPS IP, for example an `A` record for `scraper.educationvibes.in`. With a domain, the installer sets up HTTPS for free.
2. From your PC (PowerShell, in the folder holding the zip):
   ```
   scp mpscraper.zip root@YOUR_VPS_IP:/root/
   ```
   To keep the data you've already scraped, also copy your existing database:
   ```
   scp D:\py\mastersportal.sqlite root@YOUR_VPS_IP:/root/
   ```
3. On the VPS:
   ```
   ssh root@YOUR_VPS_IP
   apt-get install -y unzip && unzip -o mpscraper.zip && cd mpscraper
   cp /root/mastersportal.sqlite . 2>/dev/null || true      # only if you uploaded it
   sudo bash deploy/install.sh scraper.educationvibes.in      # or with no argument for http://IP
   sudo mpscraper-manage create-user you@example.com --role admin
   ```
4. Open `https://scraper.educationvibes.in` (or `http://YOUR_VPS_IP`) and log in.
5. Edit the contact email that the scraper sends to the site:
   ```
   sudo nano /etc/mpscraper.env        # change you@example.com in MP_USER_AGENT
   sudo systemctl restart mpscraper
   ```

Without a domain, the login runs over plain HTTP, so your password travels unencrypted. Use a domain if you can.

## Using it

- **Start / resume** skips sections that are already finished, so after a stop, a block or a server restart it carries on where it left off.
- **Fresh run** fetches everything again to refresh the data. Existing records are updated, never deleted, and the CSVs show `first_seen` and `last_seen` dates.
- **Delay:** 4 seconds is a safe default. If a job ends **blocked**, wait 30 minutes or more and resume with 6–10 seconds.
- Don't use proxies or IP rotation to get around a block.

## Day-to-day commands (on the VPS)

| What | Command |
|---|---|
| App status / logs | `systemctl status mpscraper` · `journalctl -u mpscraper -f` |
| Restart | `sudo systemctl restart mpscraper` |
| Add user / reset password | `sudo mpscraper-manage create-user EMAIL --role viewer` · `sudo mpscraper-manage reset-password EMAIL` |
| Back up the data | `sudo sqlite3 /var/lib/mpscraper/mastersportal.sqlite ".backup /root/mp-backup.sqlite"` (install with `apt install sqlite3`) |
| Update the app | upload the new zip, unzip, `sudo bash deploy/install.sh scraper.educationvibes.in` (keeps your data and settings) |

## Files

`app.py` (web app), `scraper.py` (background scrape engine), `db.py` (SQLite schema; opens the CLI script's `mastersportal.sqlite` as is), `exports.py` (CSV columns), `manage.py` (command-line user admin), `wsgi.py` (production server), `deploy/` (installer, systemd unit), `tests/` (run with `pip install pytest && pytest -q`; uses a fake API, no network).

Data lives in `/var/lib/mpscraper/mastersportal.sqlite`. Secrets and settings live in `/etc/mpscraper.env`, readable by root only.
