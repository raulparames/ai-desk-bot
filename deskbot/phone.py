"""Phone access to the dashboard over the home network.

The phone opens http://<PC on the Wi-Fi>:8899/m#t=<token> once (from the QR code on the PC's
dashboard). The page keeps the token in the phone's browser storage and sends it with every
request; any request that does not come from this PC needs it. The token lives in
phone_token.txt (not in git) and can be replaced from the PC's dashboard, which cuts off
every phone that had the old one.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import socket
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT

TOKEN_FILE = ROOT / "phone_token.txt"
STATIC = Path(__file__).parent / "dashboard_static"


def token() -> str:
    if not TOKEN_FILE.exists():
        rotate()
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def rotate() -> str:
    value = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(value, encoding="utf-8")
    return value


def lan_ip() -> str | None:
    """The PC's address on the local network (a UDP connect sends no packet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def link(port: int) -> str | None:
    ip = lan_ip()
    return f"http://{ip}:{port}/m#t={token()}" if ip else None


def qr_svg(text: str) -> str:
    import segno
    return segno.make_qr(text, error="m").svg_inline(scale=5, border=2, dark="#000000", light="#ffffff")


# ------------------------------------------------------------------ app icon (PNG, no image library)

def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _icon(size: int) -> bytes:
    """Black tile with a green rising line: the home-screen icon."""
    pts = [(0.18, 0.70), (0.38, 0.52), (0.52, 0.62), (0.82, 0.30)]
    pts = [(x * size, y * size) for x, y in pts]
    thick = size * 0.055
    dot = (pts[-1], size * 0.075)
    bg, fg = (0, 0, 0), (0, 211, 90)

    def seg_dist(px, py, a, b):
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            d = min(seg_dist(px, py, pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            cover = max(0.0, min(1.0, thick / 2 - d + 0.5))
            dd = math.hypot(px - dot[0][0], py - dot[0][1])
            cover = max(cover, max(0.0, min(1.0, dot[1] - dd + 0.5)))
            row += bytes(round(b + (f - b) * cover) for b, f in zip(bg, fg))
        rows.append(bytes(row))
    return _png(size, size, rows)


def ensure_icons() -> None:
    for size in (180, 512):
        path = STATIC / f"icon-{size}.png"
        if not path.exists():
            path.write_bytes(_icon(size))


# ------------------------------------------------------------------ password (for the home-screen app)
# On iPhone, an app added to the home screen does not share storage with Safari, so it never sees
# the key from the QR link. It can log in with a password instead: the password is checked here and
# the answer is the same phone key. Only a salted PBKDF2 hash is stored.

PASSWORD_FILE = ROOT / "phone_password.json"
MIN_PASSWORD = 6
_ITERATIONS = 200_000


def _hash(pw: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)


def password_info() -> dict:
    try:
        raw = json.loads(PASSWORD_FILE.read_text(encoding="utf-8"))
        return {"set": True, "set_at": raw.get("set_at")}
    except (OSError, ValueError):
        return {"set": False, "set_at": None}


def set_password(pw: str) -> None:
    pw = str(pw or "")
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"a password tem de ter pelo menos {MIN_PASSWORD} caracteres")
    if len(pw) > 128:
        raise ValueError("a password é demasiado comprida")
    salt = secrets.token_bytes(16)
    PASSWORD_FILE.write_text(json.dumps({
        "salt": salt.hex(), "iterations": _ITERATIONS, "hash": _hash(pw, salt, _ITERATIONS).hex(),
        "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")


def check_password(pw: str) -> bool:
    try:
        raw = json.loads(PASSWORD_FILE.read_text(encoding="utf-8"))
        got = _hash(str(pw or ""), bytes.fromhex(raw["salt"]), int(raw["iterations"]))
        return secrets.compare_digest(got, bytes.fromhex(raw["hash"]))
    except (OSError, ValueError, KeyError):
        return False


class LoginGuard:
    """5 wrong passwords from one address lock it out for 5 minutes; 30 wrong ones in an hour from
    anywhere lock the login for everyone for an hour (on the internet, guesses come from many places)."""

    def __init__(self, max_fails: int = 5, lock_s: int = 300, global_fails: int = 30):
        self.max_fails, self.lock_s, self.global_fails = max_fails, lock_s, global_fails
        self._fails: dict[str, tuple[int, float]] = {}
        self._recent: list[float] = []
        self._lock = threading.Lock()

    def wait(self, ip: str) -> int:
        with self._lock:
            self._recent = [t for t in self._recent if t > time.time() - 3600]
            if len(self._recent) >= self.global_fails:
                return int(self._recent[0] + 3600 - time.time()) + 1
            n, until = self._fails.get(ip, (0, 0.0))
            return max(0, int(until - time.time())) if n >= self.max_fails else 0

    def failed(self, ip: str) -> None:
        with self._lock:
            self._recent.append(time.time())
            n, _ = self._fails.get(ip, (0, 0.0))
            n = 0 if n >= self.max_fails else n  # a finished lock-out starts a fresh count
            n += 1
            self._fails[ip] = (n, time.time() + self.lock_s if n >= self.max_fails else 0.0)

    def ok(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
