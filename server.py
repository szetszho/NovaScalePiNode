#!/usr/bin/env python3
"""Scale Hub: a small self-hosted store and dashboard for smart-scale readings.

ble-scale-sync reads the scale over Bluetooth and POSTs each weigh-in to
/api/ingest. This server keeps every reading in SQLite, works out which
household member stepped on the scale, lets people claim readings from the
dashboard (no login), and serves the web page. Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("scale-hub")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Body composition that depends on who is on the scale (height, age, sex).
PERSONAL_FIELDS = [
    "bmi",
    "bodyFatPercent",
    "waterPercent",
    "boneMass",
    "muscleMass",
    "visceralFat",
    "physiqueRating",
    "bmr",
    "metabolicAge",
    "skeletalMusclePercent",
]
# Raw measurements that belong to the weigh-in whoever it is assigned to.
SEGMENT_FIELDS = [
    f"impedance{seg}{freq}"
    for seg in ("Trunk", "LeftArm", "RightArm", "LeftLeg", "RightLeg")
    for freq in ("20kHz", "100kHz")
]
METRIC_FIELDS = ["weight", "impedance", *PERSONAL_FIELDS, *SEGMENT_FIELDS]

# The same weight within this window is the same weigh-in delivered twice
# (webhook retry, JSONL re-import). One scale, so this ignores the person.
DUPLICATE_WINDOW_SECONDS = 120
MAX_BODY_BYTES = 64 * 1024

# Auto-assignment tuning.
HISTORY_READINGS = 5          # readings nearest in time used as a person's reference weight
HISTORY_MIN_CONFIDENCE = 0.5  # auto readings below this don't feed a person's reference
BASE_TOLERANCE_KG = 2.0       # day-to-day swing that still counts as a match
TOLERANCE_PCT = 0.03
DRIFT_KG_PER_DAY = 0.1        # extra tolerance per day since the person last weighed in
MAX_DRIFT_KG = 6.0
MIN_CONFIDENCE = 0.35         # below this the reading stays unclaimed

PALETTE = ["#0f766e", "#7c3aed", "#c2410c", "#2563eb", "#be185d", "#4d7c0f", "#b45309", "#0e7490"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_slug TEXT NOT NULL DEFAULT 'default',   -- the user ble-scale-sync matched (computed body comp for)
    user_name TEXT,
    {", ".join(f'"{f}" REAL' for f in METRIC_FIELDS)},
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(ts);
CREATE TABLE IF NOT EXISTS profiles (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    height_cm REAL,
    birth_date TEXT,
    sex TEXT,
    athlete INTEGER NOT NULL DEFAULT 0,
    start_weight REAL,
    created TEXT NOT NULL
);
"""

# Columns added after v1. Assignment state lives on the measurement:
#   profile       who the reading belongs to (NULL = unclaimed or guest)
#   assign_method 'auto' (estimated), 'manual' (someone claimed it), 'none' (no confident match)
#   auto_profile  the estimator's best guess, kept even when it was not confident
ASSIGN_COLUMNS = {
    "profile": "TEXT",
    "assign_method": "TEXT",
    "auto_profile": "TEXT",
    "confidence": "REAL",
    "claimed_at": "TEXT",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "person"


def age_on(birth: str | None, when: datetime) -> int | None:
    try:
        b = date.fromisoformat(birth) if birth else None
    except ValueError:
        return None
    if not b:
        return None
    d = when.date()
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def estimate_composition(weight: float, p: dict, when: datetime) -> dict:
    """Body composition from weight and the person's profile.

    Same formulas ble-scale-sync uses when a scale reports no body fat:
    Deurenberg 1991 body fat, Mifflin-St Jeor BMR, lean-mass ratios for water,
    bone and muscle. BMI needs only height; the rest needs height, age and sex.
    """
    out: dict = {}
    h = p.get("height_cm")
    if not h:
        return out
    bmi = weight / (h / 100) ** 2
    out["bmi"] = round(bmi, 2)
    age, sex, athlete = age_on(p.get("birth_date"), when), p.get("sex"), bool(p.get("athlete"))
    if age is None or sex not in ("male", "female"):
        return out
    male = 1 if sex == "male" else 0
    bf = (1.51 * bmi - 0.7 * age - 3.6 * male + 1.4) if age <= 15 else (1.2 * bmi + 0.23 * age - 10.8 * male - 5.4)
    if athlete:
        bf *= 0.85
    bf = max(3.0, min(bf, 60.0))
    lbm = weight * (1 - bf / 100)
    bone = lbm * 0.042
    sex_const = 5 if male else -161
    bmr = 10 * weight + 6.25 * h - 5 * age + sex_const
    if athlete:
        bmr *= 1.05
    ideal = 10 * weight + 6.25 * h - 5 * 25 + sex_const
    meta_age = max(12, age + int((ideal - bmr) / 15))
    if athlete and meta_age > age:
        meta_age = age - 5
    out.update(
        bodyFatPercent=round(bf, 1),
        waterPercent=round(lbm * (0.74 if athlete else 0.73) / weight * 100, 1),
        boneMass=round(bone, 2),
        muscleMass=round(lbm - bone, 2),
        visceralFat=max(1, min(int(bf * 0.55 - 4 + age * 0.08), 59)) if bf > 10 else 1,
        bmr=int(bmr),
        metabolicAge=meta_age,
    )
    return out


class ClaimConflict(Exception):
    def __init__(self, row):
        super().__init__("already claimed")
        self.row = row


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(measurements)")}
        for field in METRIC_FIELDS:
            if field not in existing:
                self.conn.execute(f'ALTER TABLE measurements ADD COLUMN "{field}" REAL')
        upgrading = "assign_method" not in existing
        for col, ctype in ASSIGN_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE measurements ADD COLUMN {col} {ctype}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_measurements_profile ON measurements(profile, ts)")
        if upgrading:
            # v1 databases: turn each ble-scale-sync user into a profile and keep its readings.
            rows = self.conn.execute(
                "SELECT user_slug, MAX(user_name) AS name, MIN(ts) AS first FROM measurements GROUP BY user_slug"
            ).fetchall()
            for i, r in enumerate(rows):
                self.conn.execute(
                    "INSERT OR IGNORE INTO profiles (slug, name, color, created) VALUES (?, ?, ?, ?)",
                    (r["user_slug"], r["name"] or r["user_slug"].title(), PALETTE[i % len(PALETTE)], r["first"]),
                )
            self.conn.execute(
                "UPDATE measurements SET profile=user_slug, assign_method='auto', auto_profile=user_slug, confidence=1 "
                "WHERE assign_method IS NULL"
            )
        self.conn.commit()

    # --- profiles ----------------------------------------------------------
    def profiles(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT p.*, COUNT(m.id) AS count, MAX(m.ts) AS last FROM profiles p "
                "LEFT JOIN measurements m ON m.profile = p.slug GROUP BY p.slug ORDER BY p.created, p.name"
            ).fetchall()
        return [dict(r) for r in rows]

    def _profile_map(self) -> dict[str, dict]:
        return {r["slug"]: dict(r) for r in self.conn.execute("SELECT * FROM profiles")}

    def save_profile(self, data: dict, slug: str | None = None) -> dict:
        name = str(data.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("name is required")

        def num(key, lo, hi):
            v = data.get(key)
            if v in (None, ""):
                return None
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            return v

        height = num("height_cm", 50, 250)
        start_weight = num("start_weight", 2, 400)
        birth = data.get("birth_date") or None
        if birth:
            try:
                if date.fromisoformat(birth) > date.today():
                    raise ValueError
            except ValueError:
                raise ValueError("birth_date must be a past date (YYYY-MM-DD)")
        sex = data.get("sex") if data.get("sex") in ("male", "female") else None
        athlete = 1 if data.get("athlete") else 0
        color = data.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(data.get("color") or "")) else None

        with self.lock:
            if slug is None:
                base = slugify(name)
                slug, n = base, 2
                while self.conn.execute("SELECT 1 FROM profiles WHERE slug=?", (slug,)).fetchone():
                    slug, n = f"{base}-{n}", n + 1
                count = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
                self.conn.execute(
                    "INSERT INTO profiles (slug, name, color, height_cm, birth_date, sex, athlete, start_weight, created) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, name, color or PALETTE[count % len(PALETTE)], height, birth, sex, athlete, start_weight,
                     to_iso(utc_now())),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE profiles SET name=?, color=COALESCE(?, color), height_cm=?, birth_date=?, sex=?, athlete=?, "
                    "start_weight=? WHERE slug=?",
                    (name, color, height, birth, sex, athlete, start_weight, slug),
                )
                if cur.rowcount == 0:
                    raise KeyError(slug)
            self.conn.commit()
            # A new or changed reference weight can resolve readings nobody claimed.
            self.reassign_auto()
            return dict(self.conn.execute("SELECT * FROM profiles WHERE slug=?", (slug,)).fetchone())

    def delete_profile(self, slug: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM profiles WHERE slug=?", (slug,))
            if cur.rowcount == 0:
                return False
            # Its readings become unclaimed again; manual claims by others are untouched.
            self.conn.execute(
                "UPDATE measurements SET profile=NULL, assign_method='none', auto_profile=NULL, confidence=0, "
                "claimed_at=NULL WHERE profile=? OR (auto_profile=? AND assign_method!='manual')",
                (slug, slug),
            )
            self.conn.commit()
            self.reassign_auto()
            return True

    # --- auto-assignment ---------------------------------------------------
    # Each person's reference weight is the median of their trusted readings nearest in
    # time to the weigh-in being judged: manual claims (before or after it) and confident
    # automatic matches before it. The tolerance grows with the time gap, since weight
    # drifts. Everything runs in memory: a household's history is a few thousand rows.

    def _trusted_refs(self) -> dict[str, list[tuple[float, float]]]:
        refs: dict[str, list[tuple[float, float]]] = {}
        for r in self.conn.execute(
            "SELECT profile, ts, weight FROM measurements WHERE profile IS NOT NULL AND "
            "(assign_method='manual' OR (assign_method='auto' AND confidence>=?)) ORDER BY ts",
            (HISTORY_MIN_CONFIDENCE,),
        ):
            refs.setdefault(r["profile"], []).append((parse_ts(r["ts"]).timestamp(), r["weight"]))
        return refs

    @staticmethod
    def _estimate_with(weight: float, t: float, source_slug, profiles: dict, refs: dict):
        """Return (profile or None, best guess or None, confidence 0..1)."""
        if not profiles:
            return None, None, 0.0
        cands = []
        for slug, p in profiles.items():
            hist = refs.get(slug) or []
            if hist:
                i = bisect.bisect_left(hist, (t, -1.0))
                lo, hi, near = i - 1, i, []
                while len(near) < HISTORY_READINGS and (lo >= 0 or hi < len(hist)):
                    if hi >= len(hist) or (lo >= 0 and t - hist[lo][0] <= hist[hi][0] - t):
                        near.append(hist[lo]); lo -= 1
                    else:
                        near.append(hist[hi]); hi += 1
                ref = statistics.median(w for _, w in near)
                days = abs(t - near[0][0]) / 86400
            elif p.get("start_weight"):
                ref, days = p["start_weight"], 30
            else:
                continue
            tol = max(BASE_TOLERANCE_KG, TOLERANCE_PCT * ref) + min(MAX_DRIFT_KG, DRIFT_KG_PER_DAY * days)
            d = abs(weight - ref)
            cands.append((d / tol, d, slug))
        if not cands:
            # Nobody has a weight yet: fall back to the only person, or ble-scale-sync's match.
            if len(profiles) == 1:
                only = next(iter(profiles))
                return only, only, 0.5
            if source_slug in profiles:
                return source_slug, source_slug, 0.5
            return None, None, 0.0
        cands.sort()
        score, dist, best = cands[0]
        if score > 1:
            return None, best, 0.0
        margin = next((c[1] - dist for c in cands[1:] if c[0] <= 1.5), None)
        conf = round((1 - 0.5 * score) * (1.0 if margin is None else min(1.0, margin / 1.5)), 2)
        return (best if conf >= MIN_CONFIDENCE else None), best, conf

    def estimate(self, weight: float, ts: datetime, source_slug: str | None):
        return self._estimate_with(weight, ts.timestamp(), source_slug, self._profile_map(), self._trusted_refs())

    def reassign_auto(self):
        """Re-run the estimator over every reading nobody claimed by hand, oldest first.
        Confident matches feed later readings in the same pass."""
        with self.lock:
            profiles = self._profile_map()
            refs: dict[str, list[tuple[float, float]]] = {}
            for r in self.conn.execute(
                "SELECT profile, ts, weight FROM measurements WHERE assign_method='manual' AND profile IS NOT NULL ORDER BY ts"
            ):
                refs.setdefault(r["profile"], []).append((parse_ts(r["ts"]).timestamp(), r["weight"]))
            rows = self.conn.execute(
                "SELECT id, ts, weight, user_slug, profile, auto_profile, confidence, assign_method "
                "FROM measurements WHERE assign_method != 'manual' ORDER BY ts"
            ).fetchall()
            updates = []
            for r in rows:
                t = parse_ts(r["ts"]).timestamp()
                prof, guess, conf = self._estimate_with(r["weight"], t, r["user_slug"], profiles, refs)
                if prof and conf >= HISTORY_MIN_CONFIDENCE:
                    bisect.insort(refs.setdefault(prof, []), (t, r["weight"]))
                method = "auto" if prof else "none"
                if (prof, guess, conf, method) != (r["profile"], r["auto_profile"], r["confidence"], r["assign_method"]):
                    updates.append((prof, guess, conf, method, r["id"]))
            self.conn.executemany(
                "UPDATE measurements SET profile=?, auto_profile=?, confidence=?, assign_method=? WHERE id=?", updates
            )
            self.conn.commit()

    # --- measurements ------------------------------------------------------
    def insert(self, payload: dict, assign: bool = True) -> tuple[int, str, str | None]:
        """Store one reading. Returns (id, 'created'|'duplicate', assigned profile).
        assign=False skips the estimate (bulk import runs reassign_auto once at the end)."""
        weight = payload.get("weight")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not (1 <= weight <= 400):
            raise ValueError("payload needs a numeric 'weight' in kg between 1 and 400")

        ts = parse_ts(payload.get("timestamp")) or utc_now()
        user_name = payload.get("user_name") or payload.get("userName")
        user_slug = payload.get("user_slug") or (slugify(user_name) if user_name else "default")
        values = {
            f: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
            for f in METRIC_FIELDS
            for v in [payload.get(f)]
        }

        with self.lock:
            lo = to_iso(ts - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
            hi = to_iso(ts + timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
            dup = self.conn.execute(
                "SELECT id, profile FROM measurements WHERE ts BETWEEN ? AND ? AND ABS(weight-?) < 0.05",
                (lo, hi, float(weight)),
            ).fetchone()
            if dup:
                return dup["id"], "duplicate", dup["profile"]

            prof, guess, conf = self.estimate(float(weight), ts, user_slug) if assign else (None, None, 0.0)
            cols = ["ts", "user_slug", "user_name", *METRIC_FIELDS, "raw",
                    "profile", "assign_method", "auto_profile", "confidence"]
            row = [to_iso(ts), user_slug, user_name, *(values[f] for f in METRIC_FIELDS), json.dumps(payload),
                   prof, "auto" if prof else "none", guess, conf]
            cur = self.conn.execute(
                f"INSERT INTO measurements ({', '.join(chr(34) + c + chr(34) for c in cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                row,
            )
            self.conn.commit()
            return cur.lastrowid, "created", prof

    def claim(self, mid: int, profile: str | None, confirm: bool) -> dict:
        """Assign a reading by hand. profile None = guest (belongs to nobody).
        Taking a reading someone already claimed by hand needs confirm=True."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM measurements WHERE id=?", (mid,)).fetchone()
            if not row:
                raise KeyError(mid)
            if profile is not None and not self.conn.execute(
                "SELECT 1 FROM profiles WHERE slug=?", (profile,)
            ).fetchone():
                raise ValueError("unknown profile")
            if row["assign_method"] == "manual" and row["profile"] != profile and not confirm:
                raise ClaimConflict(row)
            self.conn.execute(
                "UPDATE measurements SET profile=?, assign_method='manual', claimed_at=? WHERE id=?",
                (profile, to_iso(utc_now()), mid),
            )
            self.conn.commit()
            # A claim changes that person's reference weight, which can settle later auto readings.
            self.reassign_auto()
            return self._present([self.conn.execute("SELECT * FROM measurements WHERE id=?", (mid,)).fetchone()])[0]

    def release(self, mid: int) -> dict:
        """Undo a manual claim: the reading goes back to automatic assignment."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE measurements SET assign_method='auto', claimed_at=NULL WHERE id=?", (mid,)
            )
            if cur.rowcount == 0:
                raise KeyError(mid)
            self.conn.commit()
            self.reassign_auto()
            return self._present([self.conn.execute("SELECT * FROM measurements WHERE id=?", (mid,)).fetchone()])[0]

    def _present(self, rows) -> list[dict]:
        """Rows as API objects, with body composition for the person each belongs to."""
        profiles = self._profile_map()
        out = []
        for r in rows:
            d = {
                "id": r["id"], "ts": r["ts"], "weight": r["weight"],
                "profile": r["profile"], "assign_method": r["assign_method"],
                "auto_profile": r["auto_profile"], "confidence": r["confidence"],
                "claimed_at": r["claimed_at"], "scale_user": r["user_slug"],
            }
            for f in SEGMENT_FIELDS + ["impedance"]:
                if r[f] is not None:
                    d[f] = r[f]
            p = profiles.get(r["profile"]) if r["profile"] else None
            if p and r["user_slug"] == p["slug"]:
                # ble-scale-sync computed these for this same person (possibly the scale's own analysis).
                for f in PERSONAL_FIELDS:
                    if r[f] is not None:
                        d[f] = r[f]
            elif p:
                d.update(estimate_composition(r["weight"], p, parse_ts(r["ts"])))
                d["estimated"] = True
            out.append(d)
        return out

    def query(self, profile: str | None, since: datetime | None, unassigned: bool = False,
              limit: int | None = None, order: str = "ASC") -> list[dict]:
        sql = "SELECT * FROM measurements WHERE 1=1"
        args: list = []
        if unassigned:
            sql += " AND profile IS NULL AND assign_method != 'manual'"
        elif profile:
            sql += " AND profile=?"
            args.append(profile)
        if since:
            sql += " AND ts >= ?"
            args.append(to_iso(since))
        sql += f" ORDER BY ts {'DESC' if order == 'DESC' else 'ASC'}"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            return self._present(self.conn.execute(sql, args).fetchall())

    def delete(self, mid: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM measurements WHERE id=?", (mid,))
            self.conn.commit()
            if cur.rowcount:
                self.reassign_auto()
            return cur.rowcount > 0


def import_jsonl(store: Store, path: Path) -> tuple[int, int]:
    """Backfill from a ble-scale-sync JSONL file. Returns (created, skipped)."""
    created = skipped = 0
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                _, status, _ = store.insert(payload, assign=False)
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("%s:%d skipped: %s", path, lineno, exc)
                skipped += 1
                continue
            created += status == "created"
            skipped += status != "created"
    # Imported out of order relative to existing data: settle assignments once at the end.
    store.reassign_auto()
    return created, skipped


class Handler(BaseHTTPRequestHandler):
    server_version = "ScaleHub/2.0"
    store: Store
    api_token: str | None

    # --- helpers -----------------------------------------------------------
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _ingest_authorized(self) -> bool:
        if not self.api_token:
            return True
        given = self.headers.get("X-Api-Key") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            given = given or auth[7:]
        return given == self.api_token

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("body too large")
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _params(self):
        parsed = urlparse(self.path)
        return parsed.path, {k: v[-1] for k, v in parse_qs(parsed.query).items()}

    # --- routes ------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._params()
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if path == "/api/profiles":
            return self._json(200, self.store.profiles())
        if path == "/api/recent":
            # Latest weigh-ins from everyone, for the "who was this?" inbox.
            return self._json(200, self.store.query(None, None, limit=int(q.get("limit", 15)), order="DESC"))
        if path in ("/api/measurements", "/api/export.csv"):
            since = None
            days = q.get("days")
            if days and days != "all":
                try:
                    since = utc_now() - timedelta(days=float(days))
                except ValueError:
                    return self._json(400, {"error": "days must be a number or 'all'"})
            prof = q.get("profile") or q.get("user") or None
            rows = self.store.query(None if prof == "_unassigned" else prof, since, unassigned=prof == "_unassigned")
            if path == "/api/measurements":
                return self._json(200, rows)
            return self._csv(rows)
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path, _ = self._params()
        try:
            if path == "/api/ingest":
                if not self._ingest_authorized():
                    return self._json(401, {"error": "bad or missing X-Api-Key"})
                payload = self._body()
                mid, status, prof = self.store.insert(payload)
                log.info("Reading %s: %.2f kg (id %s) -> %s", status, payload["weight"], mid, prof or "unclaimed")
                return self._json(201 if status == "created" else 200, {"id": mid, "status": status, "profile": prof})
            if path == "/api/profiles":
                return self._json(201, self.store.save_profile(self._body()))
            m = re.fullmatch(r"/api/profiles/([a-z0-9-]+)", path)
            if m:
                return self._json(200, self.store.save_profile(self._body(), m.group(1)))
            m = re.fullmatch(r"/api/measurements/(\d+)/claim", path)
            if m:
                body = self._body()
                prof = body.get("profile") or None
                try:
                    return self._json(200, self.store.claim(int(m.group(1)), prof, bool(body.get("confirm"))))
                except ClaimConflict as c:
                    return self._json(409, {
                        "error": "already claimed", "needs_confirm": True,
                        "claimed_by": c.row["profile"], "claimed_at": c.row["claimed_at"],
                    })
            m = re.fullmatch(r"/api/measurements/(\d+)/release", path)
            if m:
                return self._json(200, self.store.release(int(m.group(1))))
        except KeyError:
            return self._json(404, {"error": "not found"})
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    do_PUT = do_POST

    def do_DELETE(self):
        path, _ = self._params()
        m = re.fullmatch(r"/api/measurements/(\d+)", path)
        if m:
            if self.store.delete(int(m.group(1))):
                return self._json(200, {"deleted": int(m.group(1))})
            return self._json(404, {"error": "no such measurement"})
        m = re.fullmatch(r"/api/profiles/([a-z0-9-]+)", path)
        if m:
            if self.store.delete_profile(m.group(1)):
                return self._json(200, {"deleted": m.group(1)})
            return self._json(404, {"error": "no such profile"})
        self._json(404, {"error": "not found"})

    def _static(self, name: str):
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _csv(self, rows: list[dict]):
        buf = io.StringIO()
        cols = ["id", "ts", "profile", "assign_method", "confidence", "estimated", *METRIC_FIELDS]
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        self._send(200, buf.getvalue().encode(), "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="scale-measurements.csv"'})


def main(argv=None):
    p = argparse.ArgumentParser(description="Scale Hub dashboard server")
    p.add_argument("--host", default=os.environ.get("SCALE_HUB_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("SCALE_HUB_PORT", "8080")))
    p.add_argument("--db", default=os.environ.get("SCALE_HUB_DB", str(BASE_DIR / "data" / "scale.db")))
    p.add_argument("--import-jsonl", metavar="FILE", help="backfill readings from a ble-scale-sync JSONL file and exit")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(Path(args.db))

    if args.import_jsonl:
        created, skipped = import_jsonl(store, Path(args.import_jsonl))
        print(f"Imported {created} readings, skipped {skipped}.")
        return 0

    Handler.store = store
    Handler.api_token = os.environ.get("SCALE_HUB_TOKEN") or None
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Scale Hub on http://%s:%d (db %s, ingest token %s)", args.host, args.port, args.db,
             "on" if Handler.api_token else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
