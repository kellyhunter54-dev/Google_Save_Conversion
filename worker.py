"""
Background worker for MapCord.

One Chrome browser processes uploaded places one at a time, in the order lists were
submitted. Every result is written to the database as soon as it's found, so:
  - the website can show live progress by polling
  - nothing is lost if the Pi restarts (unfinished lists resume on startup)
  - a URL that anyone has looked up before is answered instantly from the cache
"""

import random
import threading
import time
from collections import deque

import extract_coords_from_urls as extractor
from db import get_db, now

RESTART_BROWSER_EVERY = 75     # fresh Chrome every N pages keeps memory low on a Pi
PAUSE_BETWEEN_PAGES = (0.5, 1.5)  # small random gap so Google doesn't flag us
BLOCKED_WAIT_SECONDS = 300     # if Google shows a CAPTCHA page, wait this long and retry
MAX_BLOCKED_RETRIES = 3


class Worker:
    def __init__(self):
        self._queue = deque()          # list_ids waiting to be processed
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.current_list = None
        self.status_note = ''          # e.g. "Paused: Google rate limit"
        self._driver = None
        self._pages_on_driver = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name='mapcord-worker')

    # ----- public -----
    def start(self):
        # Resume anything left unfinished from before a restart
        db = get_db()
        db.execute("UPDATE locations SET status='pending' WHERE status='processing'")
        db.commit()
        rows = db.execute(
            "SELECT DISTINCT list_id FROM locations WHERE status='pending' ORDER BY list_id"
        ).fetchall()
        db.close()
        for r in rows:
            self.enqueue(r['list_id'])
        self._thread.start()

    def enqueue(self, list_id):
        with self._lock:
            if list_id not in self._queue and list_id != self.current_list:
                self._queue.append(list_id)
        self._wake.set()

    def remove(self, list_id):
        with self._lock:
            if list_id in self._queue:
                self._queue.remove(list_id)

    def queue_position(self, list_id):
        """0 = being processed now, 1 = next, ... None = not queued."""
        with self._lock:
            if list_id == self.current_list:
                return 0
            if list_id in self._queue:
                return list(self._queue).index(list_id) + 1
        return None

    # ----- internals -----
    def _next_list(self):
        with self._lock:
            if self._queue:
                self.current_list = self._queue.popleft()
                return self.current_list
            self.current_list = None
            return None

    def _get_driver(self):
        if self._driver is not None and self._pages_on_driver >= RESTART_BROWSER_EVERY:
            self._close_driver()
        if self._driver is None:
            self._driver = extractor.setup_driver()
            self._pages_on_driver = 0
        return self._driver

    def _close_driver(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
        self._driver = None

    def _run(self):
        while True:
            list_id = self._next_list()
            if list_id is None:
                self._close_driver()   # free memory while idle
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            try:
                self._process_list(list_id)
            except Exception as e:
                print(f"[worker] list {list_id} failed: {e}")
            finally:
                with self._lock:
                    self.current_list = None
                # Rows added to this list while it was finishing up: run it again
                db = get_db()
                more = db.execute(
                    "SELECT 1 FROM locations WHERE list_id=? AND status='pending' LIMIT 1", (list_id,)
                ).fetchone()
                db.close()
                if more:
                    self.enqueue(list_id)

    def _process_list(self, list_id):
        db = get_db()
        try:
            while True:
                # list may have been deleted mid-run
                if not db.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone():
                    return
                row = db.execute(
                    "SELECT id, url FROM locations WHERE list_id=? AND status='pending' "
                    "ORDER BY position LIMIT 1", (list_id,)
                ).fetchone()
                if row is None:
                    return
                db.execute("UPDATE locations SET status='processing' WHERE id=?", (row['id'],))
                db.commit()

                result = self._lookup(db, row['url'])
                if result is None:
                    db.execute("UPDATE locations SET status='error', method='failed' WHERE id=?", (row['id'],))
                else:
                    lat, lng, address, final_url, method = result
                    ok = lat is not None
                    db.execute(
                        "UPDATE locations SET lat=?, lng=?, address=?, final_url=?, method=?, status=? WHERE id=?",
                        (lat, lng, address, final_url, method, 'done' if ok else 'error', row['id'])
                    )
                db.execute("UPDATE lists SET updated_at=? WHERE id=?", (now(), list_id))
                db.commit()
        finally:
            db.close()

    def _lookup(self, db, url):
        """Returns (lat, lng, address, final_url, method) or None."""
        # 1. Someone already looked this exact URL up
        hit = db.execute("SELECT * FROM place_cache WHERE url=?", (url,)).fetchone()
        if hit and hit['lat'] is not None and hit['address']:
            return hit['lat'], hit['lng'], hit['address'], hit['final_url'], 'cache'

        # 2. Coordinates already in the link (still need the browser for the address)
        url_lat, url_lng = extractor.coords_from_url(url)

        # 3. Browser
        for attempt in range(MAX_BLOCKED_RETRIES + 1):
            try:
                driver = self._get_driver()
                self._pages_on_driver += 1
                lat, lng, address, final_url = extractor.fetch_via_selenium(driver, url)
            except Exception as e:
                print(f"[worker] browser error on {url[:80]}: {e}")
                self._close_driver()   # Chrome probably crashed; start fresh next time
                if url_lat is not None:
                    return url_lat, url_lng, None, url, 'url'
                return None

            if '/sorry/' in (final_url or ''):
                self.status_note = 'Paused: Google asked for a CAPTCHA, retrying in a few minutes'
                print(f"[worker] blocked by Google, waiting {BLOCKED_WAIT_SECONDS}s")
                self._close_driver()
                time.sleep(BLOCKED_WAIT_SECONDS)
                self.status_note = ''
                continue
            break
        else:
            return None

        time.sleep(random.uniform(*PAUSE_BETWEEN_PAGES))

        if lat is None and url_lat is not None:
            lat, lng = url_lat, url_lng
        if lat is not None:
            db.execute(
                "INSERT INTO place_cache (url, lat, lng, address, final_url, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, "
                "address=COALESCE(excluded.address, place_cache.address), final_url=excluded.final_url, "
                "updated_at=excluded.updated_at",
                (url, lat, lng, address, final_url, now())
            )
        return lat, lng, address, final_url, 'browser'
