#!/usr/bin/env python3
"""
app.py — self-bootstrapping venv + Flask-SocketIO signaling server + LocalTunnel,
plus new elevation-forwarder over WebSockets.
"""

import os
import sys

# ── I) STD-LIB SHADOW AVOIDANCE ───────────────────────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
for p in (script_dir, ""):
    if p in sys.path:
        sys.path.remove(p)

import subprocess
import shutil

# ── II) VENV BOOTSTRAP ─────────────────────────────────────────────────────────
venv_dir = os.path.join(script_dir, "venv")
venv_py  = os.path.join(venv_dir, "bin", "python")

def in_venv():
    return os.path.abspath(sys.executable) == os.path.abspath(venv_py)

DEPS = ["flask", "flask-cors", "flask-socketio", "eventlet"]

if not in_venv():
    if not os.path.isdir(venv_dir):
        print("→ Creating virtualenv…")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
    print("→ Installing dependencies in venv…")
    subprocess.check_call([venv_py, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([venv_py, "-m", "pip", "install"] + DEPS)
    os.execv(venv_py, [venv_py] + sys.argv)

# ── III) EVENTLET MONKEY PATCH ─────────────────────────────────────────────────
import eventlet
eventlet.monkey_patch()

# ── IV) STANDARD IMPORTS ──────────────────────────────────────────────────────
import json
import random
import argparse
import re
import select
import fcntl
import threading
import time
import urllib.request
from urllib.parse import urlparse, quote_plus
from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# ── V) LOAD OR INIT CONFIG ─────────────────────────────────────────────────────
cfg_path = os.path.join(script_dir, "config.json")
try:
    with open(cfg_path) as f:
        cfg = json.load(f)
    print("→ Loaded config:", cfg)
except FileNotFoundError:
    cfg = {}

# On first run: CORS origins
if "cors_origins" not in cfg:
    raw = input(
        "Enter comma-separated allowed CORS origins (URLs or domains)\n"
        "Leave blank for wildcard (*):\n> "
    ).strip()
    domains = []
    for part in raw.split(","):
        u = part.strip()
        if not u:
            continue
        p = urlparse(u if re.match(r"^https?://", u) else "http://" + u)
        if p.netloc:
            domains.append(p.netloc)
    cfg["cors_origins"] = domains or ["*"]

# On first run: LocalTunnel subdomain
if not cfg.get("subdomain"):
    cfg["subdomain"] = input("Enter desired LocalTunnel subdomain prefix:\n> ").strip()

# ── VI) CLI ARGS ───────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Signaling server + LocalTunnel")
parser.add_argument("--port", "-p", type=int, default=None,
                    help="Port (overrides config.json)")
parser.add_argument("--subdomain", "-s", type=str, default=None,
                    help="LocalTunnel subdomain (overrides config.json)")
args = parser.parse_args()

PORT      = args.port if args.port is not None else cfg.get("port", 3000)
SUBDOMAIN = args.subdomain if args.subdomain is not None else cfg["subdomain"]
print(f"→ Using port={PORT} subdomain={SUBDOMAIN}")

# ── VIb) PREPARE LOCALTUNNEL COMMAND ───────────────────────────────────────────
LT_PATH = shutil.which("lt")
if LT_PATH:
    launcher = [LT_PATH]
else:
    NPX = shutil.which("npx")
    if NPX:
        launcher = [NPX, "--yes", "localtunnel"]
    else:
        print("‼️ Neither 'lt' nor 'npx' found. Please install Node.js (with npm & npx).")
        sys.exit(1)

# ── VII) LAUNCH TUNNEL WITH BACKOFF ────────────────────────────────────────────
def try_tunnel(port, subdomain):
    cmd = launcher + ["--port", str(port), "--subdomain", subdomain]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    fd = proc.stderr.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    while True:
        ret = proc.poll()
        if ret is not None:
            err = proc.stderr.read().strip()
            return None, err or f"exit code {ret}"
        rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
        if proc.stdout in rlist:
            return proc, proc.stdout.readline().strip()

def launch_tunnel_with_backoff(port, subdomain):
    delay = 5
    while True:
        print(f"→ Trying LocalTunnel subdomain '{subdomain}'…")
        proc, res = try_tunnel(port, subdomain)
        if proc:
            return proc, res, subdomain
        print(f"‼️  Error launching tunnel: {res}\n→ Retrying in {delay}s…")
        time.sleep(delay)
        delay = min(delay * 2, 300)

proc, public_url, final_sub = launch_tunnel_with_backoff(PORT, SUBDOMAIN)
print(f"→ Public URL: {public_url}")

# ── VIII) PARSE PUBLIC URL ─────────────────────────────────────────────────────
m = re.search(r'https?://\S+', public_url)
if not m:
    print(f"‼️  Could not parse a valid URL out of '{public_url}'")
    sys.exit(1)
check_url = m.group(0)
print(f"→ Heartbeat will check: {check_url}")

# ── IX) SAVE UPDATED CONFIG ───────────────────────────────────────────────────
parsed = urlparse(check_url)
host = parsed.netloc
actual_sub = host.split('.')[0]

cfg.update({
    "port": PORT,
    "subdomain": actual_sub,
    "cors_origins": cfg["cors_origins"],
    "localtunnel_domain": host
})
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=4)

# ── X) SET UP CORS ORIGINS ────────────────────────────────────────────────────
if cfg["cors_origins"] == ["*"]:
    allowed_origins = "*"
else:
    allowed_origins = [f"https://{d}" for d in cfg["cors_origins"]]
    allowed_origins.append(f"https://{cfg['localtunnel_domain']}")

print("→ Allowed CORS origins:", allowed_origins)

# ── XI) FLASK + SOCKET.IO SETUP ───────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": allowed_origins}})
socketio = SocketIO(app, cors_allowed_origins=allowed_origins, async_mode="eventlet")

clients = {}
def gen_uuid():
    return "-".join("".join(random.choice("0123456789abcdef") for _ in range(4))
                    for _ in range(4))

@app.after_request
def add_headers(resp):
    if allowed_origins == "*":
        resp.headers["Access-Control-Allow-Origin"] = "*"
    else:
        origin = request.headers.get("Origin")
        if origin in allowed_origins:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Upstash-Forward-bypass-tunnel-reminder"] = "yes"
    return resp

@app.route("/")
def index():
    return "Signaling Server is up and running."

# ── XII) EXISTING SIGNALING HANDLERS ──────────────────────────────────────────
def on_connect():
    sid  = request.sid
    uuid = request.args.get("uuid") or gen_uuid()
    print(f"[+] Connected: sid={sid} uuid={uuid}")
    clients[sid] = {"uuid": uuid, "hasPassword": False}
    emit("new-peer", {"id": sid, "uuid": uuid, "hasPassword": False},
         broadcast=True, include_self=False)
    peers = [{"id": pid, "uuid": d["uuid"], "hasPassword": d["hasPassword"]}
             for pid, d in clients.items() if pid != sid]
    emit("existing-peers", peers)

def on_set_password(data):
    sid = request.sid
    clients[sid]["hasPassword"] = bool(data.get("password"))
    emit("peer-password-status",
         {"peerId": sid, "hasPassword": clients[sid]["hasPassword"]},
         broadcast=True)

def on_broadcast(data):
    sid = request.sid
    emit("peer-message",
         {"peerId": sid, "message": data.get("message")},
         broadcast=True, include_self=False)

def on_peer_message(data):
    sid, target = request.sid, data.get("target")
    if target in clients:
        socketio.emit("peer-message",
                      {"peerId": sid, "message": data.get("message")}, room=target)
    else:
        emit("error-message", {"message": f"Peer {target} does not exist."})

def on_disconnect():
    sid = request.sid
    print(f"[-] Disconnected: sid={sid}")
    clients.pop(sid, None)
    emit("peer-disconnect", sid, broadcast=True)

for evt, fn in [
    ("connect", on_connect),
    ("set-password", on_set_password),
    ("broadcast-message", on_broadcast),
    ("peer-message", on_peer_message),
    ("disconnect", on_disconnect),
]:
    socketio.on_event(evt, fn)

# ── XIII) NEW ELEVATION REQUEST HANDLER ───────────────────────────────────────
@socketio.on('elevation-request')
def handle_elevation_request(data):
    """
    Expects data: {
      dataset: "test-dataset",          # optional, default below
      location: { lat: 56.35, lng: 123 },   # OR
      locations: [ {lat,lng}, … ]
    }
    Emits back "elevation-response" with the full JSON payload.
    """
    sid = request.sid

    dataset = data.get("dataset", "test-dataset")
    pts = data.get("locations")
    if not pts and data.get("location"):
        pts = [data["location"]]
    if not pts or not isinstance(pts, list):
        emit("elevation-response",
             {"error": "Invalid request: must supply 'location' or 'locations' array."},
             room=sid)
        return

    loc_str = "|".join(f"{p['lat']},{p['lng']}" for p in pts)
    url = f"http://localhost:5000/v1/{dataset}?locations={quote_plus(loc_str)}"
    try:
        with urllib.request.urlopen(url) as resp:
            result = json.load(resp)
        emit("elevation-response", {"dataset": dataset, **result}, room=sid)
    except Exception as e:
        print("Elevation proxy error:", e)
        emit("elevation-response",
             {"error": "Failed to fetch elevation data."},
             room=sid)

# ── XIV) HEARTBEAT MONITOR ────────────────────────────────────────────────────
def heartbeat_monitor():
    failures = 0
    while True:
        try:
            resp = urllib.request.urlopen(check_url, timeout=5)
            status = getattr(resp, 'status', None) or resp.getcode()
            if status == 200:
                failures = 0
                print("✅ Heartbeat OK")
            else:
                failures += 1
                print(f"⚠️ Heartbeat HTTP {status} ({failures}/5)")
        except Exception as e:
            failures += 1
            print(f"⚠️ Heartbeat failed ({failures}/5): {e}")

        if failures >= 5:
            print("‼️ LocalTunnel unreachable 5×; restarting entire script.")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        time.sleep(60)

# ── XV) RUN ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=heartbeat_monitor, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=PORT)
