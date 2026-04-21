import asyncio
import base64
import json
import os
import sqlite3
import struct
import zlib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pywebpush import WebPushException, webpush

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "mood.db"

# ---------------------------------------------------------------------------
# VAPID keys  (generated once, persisted in DATA_DIR)
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_or_create_vapid() -> tuple[str, str]:
    """Return (private_key_pem_path, public_key_b64url)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_path = DATA_DIR / "vapid_private.pem"

    if key_path.exists():
        private_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return str(key_path), _b64url(pub_bytes)


VAPID_PRIVATE_KEY_PATH, VAPID_PUBLIC_KEY = _load_or_create_vapid()
VAPID_CLAIMS = {"sub": "mailto:mood@localhost"}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL,
            anxiety  INTEGER NOT NULL,
            disgust  INTEGER NOT NULL,
            note     TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT    NOT NULL UNIQUE,
            p256dh   TEXT    NOT NULL,
            auth     TEXT    NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("reminder_times", "[]"),
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Icon generation  (minimal solid-colour PNGs, no Pillow needed)
# ---------------------------------------------------------------------------

def _make_png(size: int, r: int, g: int, b: int) -> bytes:
    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([r, g, b, 255]) * size

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ensure_icons() -> None:
    static = Path("static")
    for size in (192, 512):
        p = static / f"icon-{size}.png"
        if not p.exists():
            p.write_bytes(_make_png(size, 124, 107, 255))  # purple

# ---------------------------------------------------------------------------
# Push sender
# ---------------------------------------------------------------------------

def send_push_to_all(message: str) -> None:
    conn = get_db()
    subs = conn.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions"
    ).fetchall()
    dead: list[str] = []
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json.dumps(
                    {"title": "Mood Tracker", "body": message}
                ),
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims=VAPID_CLAIMS,
            )
        except WebPushException as exc:
            if exc.response is not None and exc.response.status_code in (404, 410):
                dead.append(sub["endpoint"])
        except Exception:
            pass
    for ep in dead:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (ep,)
        )
    if dead:
        conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Background scheduler  (checks every 30 s)
# ---------------------------------------------------------------------------

async def notification_scheduler() -> None:
    last_sent: str | None = None
    while True:
        await asyncio.sleep(30)
        now = datetime.now()
        hm = now.strftime("%H:%M")
        if hm == last_sent:
            continue
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'reminder_times'"
        ).fetchone()
        conn.close()
        if row:
            times = json.loads(row["value"])
            if hm in times:
                last_sent = hm
                send_push_to_all("Время записать настроение")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_icons()
    task = asyncio.create_task(notification_scheduler())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EntryCreate(BaseModel):
    anxiety: int
    disgust: int
    note: str = ""

class ReminderTimesUpdate(BaseModel):
    times: list[str]

class PushSubscriptionIn(BaseModel):
    endpoint: str
    keys: dict

# ---------------------------------------------------------------------------
# Pages & static
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("tracker.html").read_text(encoding="utf-8")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")

# ---------------------------------------------------------------------------
# API – VAPID public key
# ---------------------------------------------------------------------------

@app.get("/api/vapid-key")
async def vapid_key():
    return {"publicKey": VAPID_PUBLIC_KEY}

# ---------------------------------------------------------------------------
# API – Entries
# ---------------------------------------------------------------------------

@app.get("/api/entries")
async def list_entries():
    conn = get_db()
    rows = conn.execute("SELECT * FROM entries ORDER BY ts DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/entries")
async def create_entry(entry: EntryCreate):
    conn = get_db()
    ts = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO entries (ts, anxiety, disgust, note) VALUES (?, ?, ?, ?)",
        (ts, entry.anxiety, entry.disgust, entry.note),
    )
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return {"id": eid, "ts": ts}


@app.delete("/api/entries/{entry_id}")
async def delete_entry(entry_id: int):
    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------------------------------------------------------------------
# API – Reminder settings
# ---------------------------------------------------------------------------

@app.get("/api/settings/reminders")
async def get_reminders():
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'reminder_times'"
    ).fetchone()
    conn.close()
    return {"times": json.loads(row["value"]) if row else []}


@app.put("/api/settings/reminders")
async def set_reminders(body: ReminderTimesUpdate):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("reminder_times", json.dumps(sorted(body.times))),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------------------------------------------------------------------------
# API – Push subscriptions
# ---------------------------------------------------------------------------

@app.post("/api/push/subscribe")
async def push_subscribe(sub: PushSubscriptionIn):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth) VALUES (?, ?, ?)",
        (sub.endpoint, sub.keys["p256dh"], sub.keys["auth"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/push/subscribe")
async def push_unsubscribe(sub: PushSubscriptionIn):
    conn = get_db()
    conn.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ?", (sub.endpoint,)
    )
    conn.commit()
    conn.close()
    return {"ok": True}
