#!/usr/bin/env python3
"""
MapCord backend: serves the website and the API it talks to.

Run:   python app.py              (http://localhost:5000)
Pi:    see README.md for running it as a service and putting it online.
"""

import csv
import io
import os
import re
import secrets
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import extract_coords_from_urls as extractor
from db import get_db, init_db, now
from worker import Worker

BASE_DIR = Path(__file__).resolve().parent
MAX_ROWS_PER_UPLOAD = 2000


def load_secret_key():
    """Keeps users logged in across restarts. Stored in secret_key.txt (not committed)."""
    if os.environ.get('MAPCORD_SECRET'):
        return os.environ['MAPCORD_SECRET']
    key_file = BASE_DIR / 'secret_key.txt'
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    return key_file.read_text().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,   # stay logged in 30 days
)
init_db()
worker = Worker()


# ===== HELPERS =====
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify(error='Not logged in'), 401
        return fn(*args, **kwargs)
    return wrapper


def user_list(db, list_id):
    """Returns the list row if it belongs to the logged-in user, else None."""
    return db.execute(
        "SELECT * FROM lists WHERE id=? AND user_id=?", (list_id, session['user_id'])
    ).fetchone()


def user_payload(db, user_id):
    u = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        return None
    stats = db.execute(
        "SELECT COUNT(DISTINCT l.id) AS lists, COUNT(loc.id) AS locations "
        "FROM lists l LEFT JOIN locations loc ON loc.list_id = l.id WHERE l.user_id=?", (user_id,)
    ).fetchone()
    return {
        'email': u['email'],
        'first_name': u['first_name'],
        'last_name': u['last_name'],
        'created_at': u['created_at'],
        'total_lists': stats['lists'],
        'total_locations': stats['locations'],
    }


def clean_rows(raw_rows):
    """[{title, url}] from the browser -> cleaned, valid Google Maps rows only."""
    rows = []
    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue
        url = extractor.clean_text(r.get('url'))
        title = extractor.clean_text(r.get('title')) or '(no name)'
        if re.match(r'^https?://', url) and ('google.' in url or 'goo.gl' in url):
            rows.append({'title': title[:300], 'url': url[:2000]})
    return rows


# ===== WEBSITE =====
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'MapCord.html')


# ===== AUTH =====
@app.route('/api/check-auth')
def check_auth():
    if 'user_id' not in session:
        return jsonify(authenticated=False)
    db = get_db()
    payload = user_payload(db, session['user_id'])
    db.close()
    if not payload:
        session.clear()
        return jsonify(authenticated=False)
    return jsonify(authenticated=True, **payload)


@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify(error='Please enter a valid email'), 400
    if len(password) < 6:
        return jsonify(error='Password must be at least 6 characters'), 400

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        db.close()
        return jsonify(error='An account with that email already exists'), 400
    cur = db.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
        (email, generate_password_hash(password), now())
    )
    db.commit()
    session.clear()
    session.permanent = True
    session['user_id'] = cur.lastrowid
    payload = user_payload(db, cur.lastrowid)
    db.close()
    return jsonify(**payload)


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_password_hash(u['password_hash'], password):
        db.close()
        return jsonify(error='Incorrect email or password'), 401
    session.clear()
    session.permanent = True
    session['user_id'] = u['id']
    payload = user_payload(db, u['id'])
    db.close()
    return jsonify(**payload)


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify(ok=True)


@app.route('/api/account', methods=['PUT'])
@login_required
def update_account():
    data = request.get_json(silent=True) or {}
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()

    new_pw = data.get('new_password') or ''
    if new_pw:
        if not check_password_hash(u['password_hash'], data.get('current_password') or ''):
            db.close()
            return jsonify(error='Current password is incorrect'), 400
        if len(new_pw) < 6:
            db.close()
            return jsonify(error='New password must be at least 6 characters'), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(new_pw), u['id']))

    db.execute("UPDATE users SET first_name=?, last_name=? WHERE id=?",
               ((data.get('first_name') or '').strip()[:100],
                (data.get('last_name') or '').strip()[:100], u['id']))
    db.commit()
    payload = user_payload(db, u['id'])
    db.close()
    return jsonify(**payload)


@app.route('/api/account', methods=['DELETE'])
@login_required
def delete_account():
    db = get_db()
    for r in db.execute("SELECT id FROM lists WHERE user_id=?", (session['user_id'],)).fetchall():
        worker.remove(r['id'])
    db.execute("DELETE FROM users WHERE id=?", (session['user_id'],))
    db.commit()
    db.close()
    session.clear()
    return jsonify(ok=True)


# ===== LISTS =====
@app.route('/api/lists')
@login_required
def get_lists():
    db = get_db()
    rows = db.execute(
        "SELECT l.id, l.name, l.updated_at, COUNT(loc.id) AS location_count, "
        "SUM(CASE WHEN loc.status IN ('pending','processing') THEN 1 ELSE 0 END) AS pending_count "
        "FROM lists l LEFT JOIN locations loc ON loc.list_id = l.id "
        "WHERE l.user_id=? GROUP BY l.id ORDER BY l.updated_at DESC",
        (session['user_id'],)
    ).fetchall()
    db.close()
    return jsonify(lists=[dict(r) for r in rows])


@app.route('/api/lists/<int:list_id>', methods=['PUT'])
@login_required
def rename_list(list_id):
    name = ((request.get_json(silent=True) or {}).get('name') or '').strip()[:200]
    if not name:
        return jsonify(error='Name is required'), 400
    db = get_db()
    if not user_list(db, list_id):
        db.close()
        return jsonify(error='List not found'), 404
    db.execute("UPDATE lists SET name=?, updated_at=? WHERE id=?", (name, now(), list_id))
    db.commit()
    db.close()
    return jsonify(ok=True)


@app.route('/api/lists/<int:list_id>', methods=['DELETE'])
@login_required
def delete_list(list_id):
    db = get_db()
    if not user_list(db, list_id):
        db.close()
        return jsonify(error='List not found'), 404
    worker.remove(list_id)
    db.execute("DELETE FROM lists WHERE id=?", (list_id,))
    db.commit()
    db.close()
    return jsonify(ok=True)


@app.route('/api/lists/<int:list_id>/progress')
@login_required
def list_progress(list_id):
    """Polled by the website every couple of seconds while a list is processing."""
    db = get_db()
    lst = user_list(db, list_id)
    if not lst:
        db.close()
        return jsonify(error='List not found'), 404
    rows = db.execute(
        "SELECT position, title, url, lat, lng, address, status, method FROM locations "
        "WHERE list_id=? ORDER BY position", (list_id,)
    ).fetchall()
    db.close()

    counts = {'pending': 0, 'processing': 0, 'done': 0, 'error': 0}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    return jsonify(
        id=lst['id'],
        name=lst['name'],
        total=len(rows),
        counts=counts,
        finished=counts['pending'] == 0 and counts['processing'] == 0,
        queue_position=worker.queue_position(list_id),
        note=worker.status_note,
        rows=[dict(r) for r in rows],
    )


@app.route('/api/lists/<int:list_id>/retry', methods=['POST'])
@login_required
def retry_failed(list_id):
    """Puts rows that failed back in the queue."""
    db = get_db()
    if not user_list(db, list_id):
        db.close()
        return jsonify(error='List not found'), 404
    n = db.execute("UPDATE locations SET status='pending' WHERE list_id=? AND status='error'",
                   (list_id,)).rowcount
    db.commit()
    db.close()
    if n:
        worker.enqueue(list_id)
    return jsonify(requeued=n)


@app.route('/api/lists/<int:list_id>/download')
@login_required
def download_list(list_id):
    db = get_db()
    lst = user_list(db, list_id)
    if not lst:
        db.close()
        return jsonify(error='List not found'), 404
    rows = db.execute(
        "SELECT title, url, lng, lat, address, final_url, status FROM locations "
        "WHERE list_id=? ORDER BY position", (list_id,)
    ).fetchall()
    db.close()

    buf = io.StringIO()
    buf.write('﻿')   # BOM so Excel shows Thai and other non-English text correctly
    w = csv.writer(buf)
    w.writerow(['Place Name', 'URL', 'Longitude', 'Latitude', 'Full Address', 'Final URL', 'Status'])
    for r in rows:
        w.writerow([r['title'], r['url'],
                    '' if r['lng'] is None else r['lng'],
                    '' if r['lat'] is None else r['lat'],
                    r['address'] or '', r['final_url'] or '', r['status']])
    safe_name = re.sub(r'[^\w\- ]+', '', lst['name']).strip() or 'locations'
    return Response(
        buf.getvalue(), mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}_with_coords.csv"'}
    )


# ===== UPLOAD FLOW =====
@app.route('/api/match', methods=['POST'])
@login_required
def match_upload():
    """Before extraction: how many of these are already known, and which lists already contain them."""
    rows = clean_rows((request.get_json(silent=True) or {}).get('rows'))
    urls = list(dict.fromkeys(r['url'] for r in rows))
    if not urls:
        return jsonify(error='No Google Maps links found in this file'), 400

    db = get_db()
    known = 0
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        q = ','.join('?' * len(chunk))
        known += db.execute(
            f"SELECT COUNT(*) FROM place_cache WHERE address IS NOT NULL AND url IN ({q})", chunk
        ).fetchone()[0]

    lists = {}
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        q = ','.join('?' * len(chunk))
        for r in db.execute(
            f"SELECT l.id, l.name, COUNT(DISTINCT loc.url) AS matches FROM locations loc "
            f"JOIN lists l ON l.id = loc.list_id WHERE l.user_id=? AND loc.url IN ({q}) GROUP BY l.id",
            [session['user_id'], *chunk]
        ).fetchall():
            entry = lists.setdefault(r['id'], {'id': r['id'], 'name': r['name'], 'matches': 0})
            entry['matches'] += r['matches']
    db.close()

    return jsonify(
        total=len(rows),
        unique=len(urls),
        already_known=known,
        new_to_process=len(urls) - known,
        matching_lists=sorted(lists.values(), key=lambda x: -x['matches']),
    )


@app.route('/api/extract', methods=['POST'])
@login_required
def start_extraction():
    """Creates a new list (list_name) or adds to an existing one (list_id), then queues it."""
    data = request.get_json(silent=True) or {}
    rows = clean_rows(data.get('rows'))
    if not rows:
        return jsonify(error='No Google Maps links found in this file'), 400
    if len(rows) > MAX_ROWS_PER_UPLOAD:
        return jsonify(error=f'Please upload {MAX_ROWS_PER_UPLOAD} rows or fewer at a time'), 400

    db = get_db()
    if data.get('list_id'):
        lst = user_list(db, int(data['list_id']))
        if not lst:
            db.close()
            return jsonify(error='List not found'), 404
        list_id = lst['id']
    else:
        name = (data.get('list_name') or '').strip()[:200] or 'Untitled list'
        cur = db.execute("INSERT INTO lists (user_id, name, created_at, updated_at) VALUES (?,?,?,?)",
                         (session['user_id'], name, now(), now()))
        list_id = cur.lastrowid

    # Skip links this list already has; keep upload order for the rest
    existing = {r['url'] for r in db.execute("SELECT url FROM locations WHERE list_id=?", (list_id,))}
    start = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM locations WHERE list_id=?",
                       (list_id,)).fetchone()[0]
    added = 0
    for r in rows:
        if r['url'] in existing:
            continue
        db.execute("INSERT INTO locations (list_id, position, title, url) VALUES (?,?,?,?)",
                   (list_id, start + added, r['title'], r['url']))
        added += 1
    db.execute("UPDATE lists SET updated_at=? WHERE id=?", (now(), list_id))
    db.commit()
    db.close()

    if added:
        worker.enqueue(list_id)
    return jsonify(list_id=list_id, added=added, skipped=len(rows) - added)


worker.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    try:
        from waitress import serve
        print(f"MapCord running on http://0.0.0.0:{port}")
        serve(app, host='0.0.0.0', port=port, threads=8)
    except ImportError:
        app.run(host='0.0.0.0', port=port, threaded=True)
