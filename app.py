"""
Coastal Monitoring IoT Dashboard — Flask Backend
Generates dummy sensor data, stores in SQLite, serves REST API.
"""

import sqlite3
import os
import io
import csv
import random
import time
import math
import threading
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request, render_template, g, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "iot_data.db")

app = Flask(__name__)

# Dummy‑mode state (shared between threads)
dummy_mode_enabled = True
dummy_mode_lock = threading.Lock()

# Track previous values for realistic fluctuation
_prev = {"sea_level": 130.0, "wind_speed": 10.0}

# Runtime settings (shared)
settings = {
    "data_interval": 3,       # seconds between dummy records
    "chart_points": 50,       # max points shown on live charts
    "prediction_days": 7,     # how many days to forecast
}
settings_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Open a per‑request database connection stored on Flask's `g`."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the sensor_data table if it doesn't exist."""
    conn = sqlite3.connect(DATABASE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sensor_data (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sea_level   REAL    NOT NULL,
            wind_speed  REAL    NOT NULL,
            timestamp   TEXT    NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Sensor‑value helpers
# ---------------------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def generate_sensor_values():
    """Return a dict with realistic fluctuating sea_level & wind_speed."""
    delta_sea = random.uniform(-8, 8)
    delta_wind = random.uniform(-3, 3)

    new_sea = _clamp(_prev["sea_level"] + delta_sea, 50, 250)
    new_wind = _clamp(_prev["wind_speed"] + delta_wind, 1, 25)

    _prev["sea_level"] = new_sea
    _prev["wind_speed"] = new_wind

    return {
        "sea_level": round(new_sea, 2),
        "wind_speed": round(new_wind, 2),
    }


def compute_status(sea_level, wind_speed):
    """Derive per‑metric and overall status strings."""
    if sea_level < 120:
        sea_status = "Normal"
    elif sea_level <= 180:
        sea_status = "Warning"
    else:
        sea_status = "Danger"

    if wind_speed < 10:
        wind_status = "Normal"
    elif wind_speed <= 18:
        wind_status = "Warning"
    else:
        wind_status = "Danger"

    priority = {"Normal": 0, "Warning": 1, "Danger": 2}
    overall = max(sea_status, wind_status, key=lambda s: priority[s])

    return sea_status, wind_status, overall


def insert_record(sea_level, wind_speed):
    """Insert a sensor record into the database (thread‑safe)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DATABASE)
    conn.execute(
        "INSERT INTO sensor_data (sea_level, wind_speed, timestamp) VALUES (?, ?, ?)",
        (sea_level, wind_speed, ts),
    )
    conn.commit()
    conn.close()
    return ts


def row_to_dict(row):
    """Convert a sqlite3.Row (or dict) to a plain dict with status fields."""
    d = dict(row)
    sea_status, wind_status, overall = compute_status(d["sea_level"], d["wind_speed"])
    d["sea_status"] = sea_status
    d["wind_status"] = wind_status
    d["overall_status"] = overall
    return d


# ---------------------------------------------------------------------------
# Background dummy‑data generator
# ---------------------------------------------------------------------------

def _dummy_loop():
    """Runs in a daemon thread; inserts data every N s while enabled."""
    while True:
        with dummy_mode_lock:
            enabled = dummy_mode_enabled
        if enabled:
            vals = generate_sensor_values()
            insert_record(vals["sea_level"], vals["wind_speed"])
        with settings_lock:
            interval = settings["data_interval"]
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/sensor-data", methods=["POST"])
def post_sensor_data():
    """Accept external sensor data (for real IoT devices)."""
    data = request.get_json(force=True)
    sea = data.get("sea_level")
    wind = data.get("wind_speed")
    if sea is None or wind is None:
        return jsonify({"error": "sea_level and wind_speed required"}), 400
    ts = insert_record(float(sea), float(wind))
    return jsonify({"message": "ok", "timestamp": ts}), 201


@app.route("/api/latest")
def api_latest():
    """Return the most recent sensor record."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM sensor_data ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return jsonify({"message": "no data yet"}), 200
    return jsonify(row_to_dict(row))


@app.route("/api/history")
def api_history():
    """Return the last N records (oldest‑first for charting)."""
    with settings_lock:
        limit = settings["chart_points"]
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sensor_data ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    data = [row_to_dict(r) for r in reversed(rows)]
    return jsonify(data)


@app.route("/api/generate-dummy")
def api_generate_dummy():
    """Manually trigger a single dummy record (for testing)."""
    vals = generate_sensor_values()
    ts = insert_record(vals["sea_level"], vals["wind_speed"])
    record = {**vals, "timestamp": ts}
    sea_s, wind_s, overall = compute_status(vals["sea_level"], vals["wind_speed"])
    record.update(sea_status=sea_s, wind_status=wind_s, overall_status=overall)
    return jsonify(record)


@app.route("/api/toggle-dummy", methods=["POST"])
def api_toggle_dummy():
    """Enable or disable the background dummy‑data generator."""
    global dummy_mode_enabled
    with dummy_mode_lock:
        dummy_mode_enabled = not dummy_mode_enabled
        state = dummy_mode_enabled
    return jsonify({"dummy_mode": state})


@app.route("/api/dummy-status")
def api_dummy_status():
    with dummy_mode_lock:
        state = dummy_mode_enabled
    return jsonify({"dummy_mode": state})


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

@app.route("/api/export-csv")
def api_export_csv():
    """Download all sensor data as a CSV file."""
    db = get_db()
    rows = db.execute("SELECT * FROM sensor_data ORDER BY id ASC").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "sea_level", "wind_speed", "timestamp",
                     "sea_status", "wind_status", "overall_status"])
    for r in rows:
        d = row_to_dict(r)
        writer.writerow([d["id"], d["sea_level"], d["wind_speed"],
                         d["timestamp"], d["sea_status"],
                         d["wind_status"], d["overall_status"]])

    csv_bytes = output.getvalue()
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=coastal_sensor_data.csv"},
    )


# ---------------------------------------------------------------------------
# 7‑Day Prediction (simple linear‑regression forecast)
# ---------------------------------------------------------------------------

def _linear_regression(ys):
    """Return (slope, intercept) for y values indexed 0..n-1."""
    n = len(ys)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    sx = sum(range(n))
    sy = sum(ys)
    sxx = sum(i * i for i in range(n))
    sxy = sum(i * y for i, y in enumerate(ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


@app.route("/api/prediction")
def api_prediction():
    """Return 7‑day forecast based on recent data trend."""
    with settings_lock:
        days = settings["prediction_days"]

    db = get_db()
    rows = db.execute(
        "SELECT sea_level, wind_speed, timestamp FROM sensor_data ORDER BY id DESC LIMIT 200"
    ).fetchall()
    if len(rows) < 2:
        return jsonify({"error": "not enough data for prediction"}), 200

    rows = list(reversed(rows))
    sea_vals = [r["sea_level"] for r in rows]
    wind_vals = [r["wind_speed"] for r in rows]

    sea_slope, sea_int = _linear_regression(sea_vals)
    wind_slope, wind_int = _linear_regression(wind_vals)

    n = len(sea_vals)
    # Each index step ≈ 3 seconds; compute steps per day
    steps_per_day = (24 * 3600) / 3

    predictions = []
    now = datetime.now(timezone.utc)
    for day in range(1, days + 1):
        future_idx = n + day * steps_per_day
        pred_sea = sea_int + sea_slope * future_idx
        pred_wind = wind_int + wind_slope * future_idx

        # Add sinusoidal natural variation
        pred_sea += 15 * math.sin(day * 0.9)
        pred_wind += 3 * math.sin(day * 1.1 + 0.5)

        # Clamp to realistic bounds
        pred_sea = max(50, min(250, round(pred_sea, 2)))
        pred_wind = max(1, min(25, round(pred_wind, 2)))

        sea_s, wind_s, overall = compute_status(pred_sea, pred_wind)
        predictions.append({
            "day": day,
            "date": (now + timedelta(days=day)).strftime("%Y-%m-%d"),
            "sea_level": pred_sea,
            "wind_speed": pred_wind,
            "sea_status": sea_s,
            "wind_status": wind_s,
            "overall_status": overall,
        })

    return jsonify(predictions)


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    with settings_lock:
        return jsonify(dict(settings))


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    global settings
    data = request.get_json(force=True)
    with settings_lock:
        if "data_interval" in data:
            settings["data_interval"] = max(1, min(60, int(data["data_interval"])))
        if "chart_points" in data:
            settings["chart_points"] = max(10, min(500, int(data["chart_points"])))
        if "prediction_days" in data:
            settings["prediction_days"] = max(1, min(30, int(data["prediction_days"])))
        return jsonify(dict(settings))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    # Start the background generator daemon
    t = threading.Thread(target=_dummy_loop, daemon=True)
    t.start()
    app.run(debug=True, use_reloader=False, port=5000)
