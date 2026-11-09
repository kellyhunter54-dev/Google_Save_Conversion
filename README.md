# MapCord: Google Maps Coordinate Extractor

Upload a Google Maps saved places export (the CSV from Google Takeout) and get back the exact
latitude, longitude and full address for every place.

Takeout links don't contain coordinates, so each place is opened in a headless Chrome browser
to read its exact pin location. That takes about 3 to 5 seconds per place (around 15 minutes
for 200 places). Users can close the page and come back; their list keeps processing.

## What's in this repo

| File | What it does |
|---|---|
| `index.html` | The website (accounts, upload, live progress, dashboard, CSV download). Hosted on GitHub Pages |
| `app.py` | The server on the Pi: logins, processing, results (it can also serve the website) |
| `worker.py` | Background worker: one Chrome browser that processes lists in order |
| `db.py` | SQLite database setup (`mapcord.db` is created automatically) |
| `extract_coords_from_urls.py` | The lookup logic. Also works on its own as a desktop script |
| `deploy/mapcord.service` | Keeps the app running on the Pi and starts it on boot |

### Things worth knowing

- **Shared cache:** once any user has looked up a Google Maps link, it's saved. The next time
  anyone uploads that link, it's answered instantly instead of opening Chrome again.
- **Nothing is lost on restart:** results are saved after every place. If the Pi reboots,
  unfinished lists pick up where they left off.
- **One list at a time:** a Pi can only run one Chrome comfortably, so lists are processed in
  the order they were submitted. Users see their place in line.
- **Google rate limits:** if Google shows a CAPTCHA page, the worker pauses 5 minutes and
  retries. Failed rows can be retried from the website with the "Retry Failed" button.

### How the pieces connect

```
GitHub Pages (index.html)  --->  Pi (app.py, via Cloudflare Tunnel HTTPS address)
       the website                  logins, processing, results, CSV downloads
```

The website is a static page on GitHub Pages. It talks to the Pi through the Pi's public
address, which is set in one place: `PI_URL` near the top of the script in `index.html`.
If the Pi is off, the site still loads but shows that it can't reach the server.

---

## Try it on your own computer first (optional)

Needs Python 3.10+ and Google Chrome.

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000, create an account and upload your CSV. (When the Pi or your
computer serves the page itself, it doesn't need `PI_URL`.)

---

## Raspberry Pi setup

Recommended: Raspberry Pi 4 or 5 with 4 GB of RAM or more, running Raspberry Pi OS (64-bit).

### 1. Install system packages

```
sudo apt update
sudo apt install -y git python3-venv chromium chromium-driver
```

If `chromium` isn't found (older Pi OS), use `chromium-browser` instead.

### 2. Get the code

```
cd ~
git clone https://github.com/kellyhunter54-dev/Google_Save_Conversion.git
cd Google_Save_Conversion
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Test it

```
venv/bin/python app.py
```

From another computer on the same network, open `http://<pi-ip-address>:5000`
(find the Pi's address with `hostname -I`). Upload a small file to confirm Chrome works,
then press Ctrl+C to stop it.

### 4. Run it automatically on boot

If your Pi username isn't `pi`, edit `deploy/mapcord.service` first and replace every `pi`
with your username.

```
sudo cp deploy/mapcord.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mapcord
```

Useful commands:

```
sudo systemctl status mapcord     # is it running?
journalctl -u mapcord -f          # live log (Ctrl+C to exit)
sudo systemctl restart mapcord    # restart after changes
```

### 5. Put it on the internet with Cloudflare Tunnel

This makes the site reachable from anywhere over HTTPS without opening ports on your router.

**Quick test (no account, temporary address):**

```
cloudflared tunnel --url http://localhost:5000
```

It prints a `https://something.trycloudflare.com` address. That address changes every time,
so it's only for testing.

**Permanent (needs a free Cloudflare account and a domain on Cloudflare):**

1. In the Cloudflare dashboard, go to Zero Trust, then Networks, then Tunnels, and create a tunnel.
2. Choose Debian and arm64. Cloudflare shows one command to run on the Pi; it installs
   `cloudflared` and connects it.
3. Add a public hostname, for example `maps.yourdomain.com`, pointing to `http://localhost:5000`.

### 6. Connect the GitHub Pages site to the Pi

1. Open `index.html` in GitHub (click the pencil icon to edit).
2. Near the top of the script, change
   `const PI_URL = 'https://maps.yourdomain.com';`
   to your tunnel address from step 5, for example `https://maps.kellysdomain.com`.
3. Commit. GitHub Pages updates within a minute or two.

The Pi only accepts requests from `https://kellyhunter54-dev.github.io`. If you ever move the
site to a different address (like a custom domain), add it to the service file:

```
Environment=MAPCORD_ALLOWED_ORIGINS=https://kellyhunter54-dev.github.io,https://maps.kellysdomain.com
```

then run `sudo systemctl daemon-reload && sudo systemctl restart mapcord`.

### 7. Updating later

```
cd ~/Google_Save_Conversion
git pull
venv/bin/pip install -r requirements.txt
sudo systemctl restart mapcord
```

### Backups

Everything (users, logins, lists, results, cache) is in `mapcord.db`. Copy that one file to
back it up.

---

## Using the script on its own (no website)

```
pip install selenium openpyxl
python extract_coords_from_urls.py
```

It opens a file picker, then saves a `_with_coords.csv` next to your file. You can also pass the
files directly: `python extract_coords_from_urls.py Thailand.csv Thailand_with_coords.csv`
