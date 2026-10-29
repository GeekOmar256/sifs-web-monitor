"""LAN dashboard API backed directly by the Pi controller and local SQLite history."""

import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DATA_DIR = Path(os.getenv("SIFS_DATA_DIR", str(BASE / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "history.sqlite3"
SETTINGS_FILE = DATA_DIR / "settings.json"
ALLOWED_SETTINGS = {"pumpMode", "lightMode", "manualPump", "manualLight",
                    "co2Threshold", "photoIntervalMinutes"}


def validate_setting(key, value):
    if key not in ALLOWED_SETTINGS:
        raise ValueError("unknown setting")
    if key in ("pumpMode", "lightMode") and value in ("auto", "manual"):
        return value
    if key in ("manualPump", "manualLight") and type(value) is bool:
        return value
    if key == "co2Threshold" and type(value) in (int, float) and 400 <= value <= 5000:
        return int(value)
    if key == "photoIntervalMinutes" and type(value) in (int, float) and 1 <= value <= 60:
        return int(value)
    raise ValueError("invalid setting value")


class LocalState:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
        self.telemetry = {}
        self.photo = None
        self.capture_request = None
        self.remote_capture_request = None
        self.camera_error = None
        self.cloud_online = False
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS telemetry (timestamp INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, image TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS synced_analyses (cloud_key TEXT PRIMARY KEY);
            """)

    def connect(self):
        return sqlite3.connect(DB_FILE, timeout=5)

    def snapshot(self):
        with self.lock:
            return {"telemetry": self.telemetry, "settings": self.settings,
                    "photo": self.photo, "cloudOnline": self.cloud_online,
                    "cameraError": self.camera_error}

    def set_setting(self, key, value, timestamp=None, queue_cloud=True):
        value = validate_setting(key, value)
        timestamp = timestamp or int(time.time() * 1000)
        with self.lock:
            self.settings[key] = value
            self.settings["updatedAt"] = timestamp
            temp = SETTINGS_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings))
            temp.replace(SETTINGS_FILE)
        with self.connect() as conn:
            conn.execute("INSERT INTO settings(timestamp,key,value) VALUES(?,?,?)",
                         (timestamp, key, json.dumps(value)))
            if queue_cloud:
                conn.execute("INSERT INTO outbox(path,payload) VALUES(?,?)",
                             ("history/settings", json.dumps({"timestamp": timestamp, "key": key, "value": value})))
        return timestamp

    def apply_remote_settings(self, values):
        stamp = values.get("updatedAt") or int(time.time() * 1000)
        changed = False
        for key in ALLOWED_SETTINGS:
            if key in values and values[key] != self.settings.get(key):
                try:
                    self.set_setting(key, values[key], stamp, queue_cloud=False)
                    changed = True
                except ValueError:
                    pass
        if not changed:
            with self.lock:
                self.settings["updatedAt"] = stamp
                temp = SETTINGS_FILE.with_suffix(".tmp")
                temp.write_text(json.dumps(self.settings))
                temp.replace(SETTINGS_FILE)

    def record_telemetry(self, telemetry, settings):
        stamp = int(time.time() * 1000)
        row = {"timestamp": stamp, **telemetry, "settings": settings}
        with self.lock:
            self.telemetry = telemetry
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO telemetry(timestamp,data) VALUES(?,?)",
                         (stamp, json.dumps(row)))
            conn.execute("INSERT INTO outbox(path,payload) VALUES(?,?)",
                         ("history/telemetry", json.dumps(row)))
        return row

    def set_photo(self, image, captured_at):
        with self.lock:
            self.photo = {"image": image, "capturedAt": captured_at}
        stamp = int(time.time() * 1000)
        with self.connect() as conn:
            conn.execute("INSERT INTO photos(timestamp,image) VALUES(?,?)", (stamp, image))
            conn.execute("INSERT INTO outbox(path,payload) VALUES(?,?)",
                         ("history/photos", json.dumps({"timestamp": stamp, "image": image,
                                                          "capturedAt": captured_at})))
            conn.execute("DELETE FROM photos WHERE id NOT IN (SELECT id FROM photos ORDER BY id DESC LIMIT 250)")
        return stamp

    def record_analysis(self, result):
        stamp = int(time.time() * 1000)
        with self.connect() as conn:
            conn.execute("INSERT INTO analyses(timestamp,result) VALUES(?,?)", (stamp, json.dumps(result)))
            conn.execute("INSERT INTO outbox(path,payload) VALUES(?,?)",
                         ("history/analyses", json.dumps({"timestamp": stamp, **result})))
        return stamp

    def pending(self):
        with self.connect() as conn:
            return conn.execute("SELECT id,path,payload FROM outbox ORDER BY id LIMIT 20").fetchall()

    def mark_sent(self, row_id, cloud_key=None):
        with self.connect() as conn:
            conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            if cloud_key:
                conn.execute("INSERT OR IGNORE INTO synced_analyses(cloud_key) VALUES(?)", (cloud_key,))

    def import_analyses(self, cloud_rows):
        with self.connect() as conn:
            for key, value in cloud_rows.items():
                exists = conn.execute("SELECT 1 FROM synced_analyses WHERE cloud_key=?", (key,)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO analyses(timestamp,result) VALUES(?,?)",
                                 (value.get("timestamp") or int(time.time() * 1000), json.dumps(value)))
                    conn.execute("INSERT INTO synced_analyses(cloud_key) VALUES(?)", (key,))

    def history(self, kind, limit=100):
        if kind not in ("telemetry", "settings", "photos", "analyses"):
            raise ValueError("unknown history kind")
        limit = min(max(int(limit), 1), 500)
        column = {"telemetry": "data", "settings": "value", "photos": "image", "analyses": "result"}[kind]
        with self.connect() as conn:
            if kind == "settings":
                rows = conn.execute("SELECT timestamp,key,value FROM settings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [{"timestamp": stamp, "key": key, "value": json.loads(value)} for stamp, key, value in rows]
            rows = conn.execute(f"SELECT timestamp,{column} FROM {kind} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        if kind == "telemetry":
            return [json.loads(value) for _, value in rows]
        if kind == "photos":
            return [{"timestamp": stamp, "image": value} for stamp, value in rows]
        return [{"timestamp": stamp, **json.loads(value)} for stamp, value in rows]


def create_app(state):
    app = Flask(__name__, static_folder=None)
    pin = os.environ.get("SIFS_LOCAL_PIN", "")
    if len(pin) < 6:
        raise RuntimeError("Set SIFS_LOCAL_PIN to at least 6 characters before starting the LAN dashboard")

    @app.before_request
    def require_pin():
        if request.path.startswith("/api/local/"):
            supplied = request.headers.get("X-SIFS-PIN", "")
            if not hmac.compare_digest(supplied, pin):
                abort(401)

    @app.get("/")
    def index():
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = html.replace("</head>", "<script>window.SIFS_LOCAL = true;</script></head>")
        return Response(html, mimetype="text/html")

    @app.get("/<path:name>")
    def asset(name):
        return send_from_directory(WEB, name)

    @app.get("/api/local/state")
    def get_state():
        return jsonify(state.snapshot())

    @app.put("/api/local/settings")
    def put_setting():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            stamp = state.set_setting(payload.get("key"), payload.get("value"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"timestamp": stamp})

    @app.post("/api/local/capture")
    def capture():
        with state.lock:
            state.capture_request = str(time.time_ns())
        return jsonify({"queued": True})

    @app.post("/api/local/analysis")
    def analysis():
        payload = request.get_json(force=True, silent=True) or {}
        if not isinstance(payload.get("result"), dict):
            return jsonify({"error": "invalid analysis"}), 400
        stamp = state.record_analysis(payload["result"])
        return jsonify({"timestamp": stamp})

    @app.get("/api/local/history/<kind>")
    def history(kind):
        try:
            return jsonify(state.history(kind, request.args.get("limit", 100)))
        except (ValueError, TypeError):
            return jsonify({"error": "invalid history request"}), 400

    return app
