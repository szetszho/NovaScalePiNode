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
import math
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

# Which published equations a profile uses for impedance-based body composition.
#   general     NHANES III fat-free mass (Chumlea 2002), Janssen 2000 skeletal muscle
#   east_asian  Chen 2016 fat-free mass (704 Chinese adults vs DXA, hand-to-hand path),
#               Janssen 2000 + 2.45 kg (the paper's MRI-measured underestimate in Asians)
BODY_REFERENCES = ("general", "east_asian")
COMPOSITION_SOURCES = ("scale", "model")  # which one a profile's cards and charts show first
JANSSEN_ASIAN_OFFSET_KG = 2.45

# What the R-MSC04 itself reports for the profile it was sent (via ble-scale-sync, which
# sends skeletalMusclePercent only when it used the scale's figures). Water, bone and muscle
# mass are derived from the scale's body fat by ble-scale-sync.
SCALE_FIELDS = ["bodyFatPercent", "skeletalMusclePercent", "visceralFat", "waterPercent", "boneMass", "muscleMass"]
# (water, bone and muscle are then re-derived from the scale's body fat the Renpho way: renpho_split)
MODEL_FIELDS = ["bodyFatPercent", "skeletalMuscleMass", "skeletalMusclePercent", "muscleMass", "waterPercent",
                "boneMass", "visceralFat", "bmr", "metabolicAge", "composition_source", "reference",
                "impedance50kHz", "impedanceRatio"]

# Segmental analysis and "standard" ranges, reconstructed from two Renpho app reports (male,
# 180 cm, 67.85 and 70.25 kg; see docs/rmsc04-notes.md section 6b). Renpho splits the muscle of
# the five segments by fixed shares (arms 12.6 %, trunk 49.8 %, legs 37.6 %; they hold 93.75 % of
# whole-body muscle), not by segment impedance: the trunk's impedance fell 19 % between the
# reports and its share didn't move. Scale Hub uses the same shares and splits each left/right
# pair by measured impedance (lean tissue conducts), so asymmetries still show.
SEG_PARTS = {"Trunk": "Trunk", "LeftArm": "Arm", "RightArm": "Arm", "LeftLeg": "Leg", "RightLeg": "Leg"}
PAIRS = {"Arm": ("LeftArm", "RightArm"), "Leg": ("LeftLeg", "RightLeg")}
SEG_MUSCLE_SHARE = 0.9375                                         # five segments / whole-body muscle
MUSCLE_SPLIT = {"Arm": 0.126, "Trunk": 0.498, "Leg": 0.376}      # per part (a pair counts once)
FAT_SPLIT = {"Arm": 0.067, "Trunk": 0.532, "Leg": 0.370}         # of whole-body fat, per part
# Segment standards: muscle = 0.7123 x the mean of actual and standard weight, split arm 6.43 %,
# trunk 51.32 %, leg 17.91 % (each); fat = standard fat mass split arm 5.37 %, trunk 35.5 %, leg 13.9 %.
STD_SEG_MUSCLE_K = 0.7123
STD_SEG_MUSCLE = {"Arm": 0.0643, "Trunk": 0.5132, "Leg": 0.1791}
STD_SEG_FAT = {"Arm": 0.0537, "Trunk": 0.355, "Leg": 0.139}
FAT_RANGE = {"male": (0.12, 0.24), "female": (0.20, 0.32)}       # female: assumed, not from a report
STD_OF_FFM = {"muscleMass": 0.93, "skeletalMuscleMass": 0.58, "waterMass": 0.73, "proteinMass": 0.20, "boneMass": 0.067}
# How the Renpho app splits fat-free mass: water 73.3 %, bone about 6.7 %, muscle the rest after
# bone, protein the rest after water and bone; BMR is Katch-McArdle (370 + 21.6 x FFM). Both
# reports match these. ble-scale-sync derives water, bone and muscle differently, so the scale's
# figures are re-derived this way to match the app.
RENPHO_WATER_OF_FFM = 0.733
RENPHO_BONE_OF_FFM = 0.067
SEG_MUSCLE_BAND = (90, 110)  # % of standard: Low below, High above (matches the reports)
SEG_FAT_BAND = (80, 160)     # Low threshold matches the reports; High follows the InBody convention


def evaluate(value: float, lo: float, hi: float) -> str:
    return "low" if value < lo else "high" if value > hi else "standard"


def renpho_split(weight: float, fat_pct: float) -> dict:
    """Water, bone, protein and muscle the way the Renpho app derives them from body fat."""
    ffm = weight * (1 - fat_pct / 100)
    water, bone = ffm * RENPHO_WATER_OF_FFM, ffm * RENPHO_BONE_OF_FFM
    return {"ffm": ffm, "waterMass": water, "waterPercent": round(100 * water / weight, 1),
            "boneMass": round(bone, 2), "proteinMass": ffm - water - bone, "muscleMass": round(ffm - bone, 2),
            "bmr": int(370 + 21.6 * ffm)}


def standards(p: dict, weight: float, comp: dict) -> list[dict] | None:
    """Whole-body values against Renpho-style standard ranges for this person's height and sex.
    Water, bone, protein and muscle are derived from body fat the Renpho way for every source,
    since the standards are defined on that split."""
    h, sex = p.get("height_cm"), p.get("sex")
    if not h or sex not in FAT_RANGE or comp.get("bodyFatPercent") is None:
        return None
    std_w = 22 * (h / 100) ** 2
    lo, hi = FAT_RANGE[sex]
    std_ffm = std_w * (1 - (lo + hi) / 2)
    split = renpho_split(weight, comp["bodyFatPercent"])
    have = {"weight": weight, "fatMass": weight - split["ffm"], "boneMass": split["boneMass"],
            "proteinMass": split["proteinMass"], "waterMass": split["waterMass"], "muscleMass": split["muscleMass"]}
    if comp.get("skeletalMuscleMass") is not None:
        have["skeletalMuscleMass"] = comp["skeletalMuscleMass"]
    ranges = {"weight": (std_w * 0.85, std_w * 1.15), "fatMass": (std_w * lo, std_w * hi)}
    ranges.update({k: (std_ffm * f * 0.9, std_ffm * f * 1.1) for k, f in STD_OF_FFM.items()})
    out = []
    for key in ("weight", "fatMass", "boneMass", "proteinMass", "waterMass", "muscleMass", "skeletalMuscleMass"):
        if key in have:
            a, b = ranges[key]
            out.append({"key": key, "value": round(have[key], 2), "lo": round(a, 2), "hi": round(b, 2),
                        "eval": evaluate(have[key], a, b)})
    return out


def segmental(p: dict, weight: float, comp: dict, row: dict) -> dict | None:
    """Muscle and fat per segment with % of standard, the way the Renpho app report shows them.

    Each part (arms, trunk, legs) gets Renpho's fixed share of the shown muscle and fat mass; a
    left/right pair is then split by measured impedance (lower impedance = more lean, less fat).
    Impedance can't locate fat, so the fat split is an estimate either way."""
    h, sex = p.get("height_cm"), p.get("sex")
    if not h or sex not in FAT_RANGE or comp.get("muscleMass") is None or comp.get("bodyFatPercent") is None:
        return None
    z = {}
    for seg in SEG_PARTS:
        v = row.get(f"impedance{seg}20kHz")
        if not v or v <= 0:
            return None
        z[seg] = v
    std_w = 22 * (h / 100) ** 2
    lo, hi = FAT_RANGE[sex]
    std_fat = std_w * (lo + hi) / 2
    std_seg_muscle = STD_SEG_MUSCLE_K * (std_w + weight) / 2
    muscle = comp["muscleMass"] * SEG_MUSCLE_SHARE
    fat = weight * comp["bodyFatPercent"] / 100
    m_kg, f_kg = {"Trunk": muscle * MUSCLE_SPLIT["Trunk"]}, {"Trunk": fat * FAT_SPLIT["Trunk"]}
    for part, (a, b) in PAIRS.items():
        lean_a = (1 / z[a]) / (1 / z[a] + 1 / z[b])   # share of the pair's lean tissue
        m_kg[a], m_kg[b] = muscle * MUSCLE_SPLIT[part] * lean_a, muscle * MUSCLE_SPLIT[part] * (1 - lean_a)
        f_kg[a], f_kg[b] = fat * FAT_SPLIT[part] * (1 - lean_a), fat * FAT_SPLIT[part] * lean_a
    out = {"muscle": {}, "fat": {}}
    for seg, part in SEG_PARTS.items():
        m_pct = 100 * m_kg[seg] / (std_seg_muscle * STD_SEG_MUSCLE[part])
        f_pct = 100 * f_kg[seg] / (std_fat * STD_SEG_FAT[part])
        out["muscle"][seg] = {"kg": round(m_kg[seg], 2), "pct": round(m_pct, 1), "eval": evaluate(m_pct, *SEG_MUSCLE_BAND)}
        out["fat"][seg] = {"kg": round(f_kg[seg], 2), "pct": round(f_pct, 1), "eval": evaluate(f_pct, *SEG_FAT_BAND)}
    limbs = sum(m_kg[s] for s in ("LeftArm", "RightArm", "LeftLeg", "RightLeg"))
    out["smi"] = round(limbs / (h / 100) ** 2, 1)            # appendicular muscle / height²
    out["obesityDegree"] = round(100 * weight / std_w)         # weight / standard weight
    return out


# ble-scale-sync's config, which Scale Hub keeps in step with the chosen scale profile.
BSS_CONFIG = Path(os.environ.get("SCALE_HUB_BSS_CONFIG") or Path.home() / "ble-scale-sync" / "config.yaml")
RESYNC_SECONDS = 6 * 3600  # re-check the profile byte (age changes on birthdays)

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
    body_reference TEXT NOT NULL DEFAULT 'general',
    primary_source TEXT NOT NULL DEFAULT 'scale',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
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
    "scale_for": "TEXT",   # the Scale Hub profile the scale was set to compute for at this weigh-in
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


# Fraction of the way from 20 to 100 kHz on a log-frequency scale where 50 kHz sits.
F50 = math.log(50 / 20) / math.log(100 / 20)


def whole_body_impedance(row) -> dict | None:
    """Hand-to-foot and hand-to-hand impedance from the eight-electrode segments.

    A hand-to-foot path runs through one arm, the trunk and one leg, so it is
    approximated as arm + trunk + leg averaged over both sides; hand-to-hand is
    left arm + right arm. The prediction equations were built on 50 kHz
    measurements; 50 kHz is interpolated between the scale's 20 and 100 kHz
    readings on a log-frequency scale. At these frequencies impedance is within
    about 1 % of resistance.
    """
    segs = ("Trunk", "LeftArm", "RightArm", "LeftLeg", "RightLeg")
    z = {}
    for seg in segs:
        lo, hi = row.get(f"impedance{seg}20kHz"), row.get(f"impedance{seg}100kHz")
        if not lo or not hi or lo <= 0 or hi <= 0 or hi > lo * 1.02:
            return None  # missing, or impedance rising with frequency: a bad contact
        z[seg] = (lo, hi, lo + (hi - lo) * F50)

    def path(i, parts):
        return sum(z[p][i] for p in parts)

    limbs = ("LeftArm", "RightArm", "LeftLeg", "RightLeg")
    z20 = path(0, limbs) / 2 + z["Trunk"][0]
    z100 = path(1, limbs) / 2 + z["Trunk"][1]
    z50 = path(2, limbs) / 2 + z["Trunk"][2]
    hand50 = path(2, ("LeftArm", "RightArm"))
    if not 200 <= z50 <= 1200 or not 150 <= hand50 <= 1200:
        return None
    return {"z20": z20, "z100": z100, "z50": z50, "hand50": hand50}


def estimate_composition(weight: float, p: dict, when: datetime, row: dict | None = None,
                         impedance_only: bool = False) -> dict:
    """Body composition for one weigh-in and the person's profile.

    With the scale's segment impedances (adults only):
      fat-free mass  NHANES III, Chumlea et al. 2002, Int J Obes 26:1596; or for
                     body_reference east_asian, Chen et al. 2016, Nutrients 8:654
      skeletal muscle Janssen et al. 2000, J Appl Physiol 89:465 (MRI-validated),
                     plus 2.45 kg for east_asian
      BMR            Katch-McArdle, from fat-free mass
    Without them, the BMI-based fallback ble-scale-sync also uses: Deurenberg 1991
    body fat and Mifflin-St Jeor BMR. Water, bone and muscle mass come from
    fat-free mass in both cases. BMI needs only height; the rest needs height,
    age and sex. impedance_only skips the BMI fallback, so a person whose history
    has impedance doesn't get a trend that jumps between the two methods.
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
    sex_const = 5 if male else -161
    ideal_bmr = 10 * weight + 6.25 * h - 5 * 25 + sex_const  # Mifflin-St Jeor at age 25

    wb = whole_body_impedance(row) if row and age >= 18 else None
    reference = p.get("body_reference") if p.get("body_reference") in BODY_REFERENCES else "general"
    ffm = smm = None
    if wb:
        hr = h * h / wb["z50"]
        if reference == "east_asian":
            hh = h * h / wb["hand50"]
            if male:
                ffm = 0.537 * hh - 0.126 * age + 0.217 * weight + 18.235
            else:
                ffm = 0.615 * hh - 0.144 * age + 0.132 * weight + 16.507
        elif male:
            ffm = -10.678 + 0.262 * weight + 0.652 * hr + 0.015 * wb["z50"]
        else:
            ffm = -9.529 + 0.168 * weight + 0.696 * hr + 0.016 * wb["z50"]
        smm = 0.401 * hr + 3.825 * male - 0.071 * age + 5.102
        if reference == "east_asian":
            smm += JANSSEN_ASIAN_OFFSET_KG
        bf = 100 * (1 - ffm / weight)
        if not (3 <= bf <= 60) or not (0 < smm < ffm):
            ffm = smm = None  # equation pushed outside its valid range: fall back to BMI

    if ffm is not None:
        bf = 100 * (1 - ffm / weight)
        bmr = 370 + 21.6 * ffm
        source = "impedance"
        out["reference"] = reference
        out.update(
            skeletalMuscleMass=round(smm, 1),
            skeletalMusclePercent=round(smm / weight * 100, 1),
            impedance50kHz=round(wb["z50"], 1),
            impedanceRatio=round(wb["z100"] / wb["z20"], 3),
        )
    elif impedance_only:
        return out
    else:
        bf = (1.51 * bmi - 0.7 * age - 3.6 * male + 1.4) if age <= 15 else (1.2 * bmi + 0.23 * age - 10.8 * male - 5.4)
        if athlete:
            bf *= 0.85
        bf = max(3.0, min(bf, 60.0))
        bmr = 10 * weight + 6.25 * h - 5 * age + sex_const
        if athlete:
            bmr *= 1.05
        source = "bmi"
    lbm = weight * (1 - bf / 100)
    bone = lbm * 0.042
    # Metabolic age has no standard definition; this is ble-scale-sync's (Mifflin-St Jeor
    # against the same formula at 25), so it matches what reaches Garmin. Comparing the
    # lean-mass BMR above against Mifflin would skew it by a decade or more.
    mifflin = 10 * weight + 6.25 * h - 5 * age + sex_const
    if athlete:
        mifflin *= 1.05
    meta_age = max(12, age + int((ideal_bmr - mifflin) / 15))
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
        composition_source=source,
    )
    return out


def profile_byte(sex: str, age: int) -> int:
    """The R-MSC04 b2 profile byte: bit 7 set for male, the low 7 bits the age in years
    (from Renpho app captures: male 30 = 0x9E, female 30 = 0x1E, female 32 = 0x20)."""
    return (0x80 if sex == "male" else 0x00) | max(0, min(int(age), 0x7F))


_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def _block(lines: list[str], key: str) -> tuple[int, int] | None:
    """Line range [start, end) of a top-level `key:` block in a YAML file."""
    start = next((i for i, l in enumerate(lines) if re.match(rf"^{key}\s*:\s*(#.*)?$", l)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _TOP_KEY.match(lines[i])), len(lines))
    return start, end


def first_user_slug(path: Path) -> str | None:
    """The slug of the first user in ble-scale-sync's config.yaml (who it tags readings with)."""
    lines = path.read_text(encoding="utf-8").split("\n")
    users = _block(lines, "users")
    if not users:
        return None
    depth = None
    for i in range(users[0] + 1, users[1]):
        m = re.match(r"^(\s*)- ", lines[i])
        if m:
            if depth is not None:
                break
            depth = len(m.group(1))
        m = re.match(r"^\s*(?:- )?slug\s*:\s*['\"]?([a-z0-9-]+)", lines[i])
        if m and depth is not None:
            return m.group(1)
    return None


def write_bss_config(path: Path, p: dict, byte: int, last_weight: float | None) -> tuple[bool, str | None]:
    """Set the first user (who the scale computes for) and ble.rmsc04_profile_byte in
    ble-scale-sync's config.yaml. Line edits only: comments, exporters and other users stay.
    Returns (changed, warning)."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    users = _block(lines, "users")
    if not users:
        raise ValueError("no top-level 'users:' list")
    item = next((i for i in range(users[0] + 1, users[1]) if re.match(r"^\s*- ", lines[i])), None)
    if item is None:
        raise ValueError("'users:' has no entries")
    dash = re.match(r"^(\s*)- ", lines[item])
    indent = len(dash.group(1))
    key_pad = " " * (indent + 2)
    item_end = next((i for i in range(item + 1, users[1])
                     if lines[i].strip() and not lines[i].lstrip().startswith("#")
                     and len(lines[i]) - len(lines[i].lstrip()) <= indent), users[1])
    # The slug stays: Garmin keeps its login per user slug. Scale Hub records which of its
    # profiles the scale was set to on each reading instead (measurements.scale_for).
    inches = any(re.match(r"^\s+height_unit\s*:\s*['\"]?in\b", l) for l in lines)
    height = round(p["height_cm"] / 2.54, 1) if inches else p["height_cm"]
    values = {
        "name": json.dumps(p["name"]),
        "height": f"{height:g}",
        "birth_date": json.dumps(p["birth_date"]),
        "gender": p["sex"],
        "is_athlete": "true" if p.get("athlete") else "false",
    }
    if last_weight:
        values["last_known_weight"] = f"{last_weight:g}"
    for key, val in values.items():
        found = None
        for i in range(item, item_end):
            m = re.match(rf"^(\s*- |{key_pad}){key}\s*:", lines[i])
            if m:
                found = (i, m.group(1))
                break
        if found:
            lines[found[0]] = f"{found[1]}{key}: {val}"
        else:
            lines.insert(item + 1, f"{key_pad}{key}: {val}")
            item_end += 1
    warning = None
    for i in range(item, item_end):
        m = re.search(r"weight_range\s*:\s*\{\s*min\s*:\s*([\d.]+)\s*,\s*max\s*:\s*([\d.]+)", lines[i])
        if m and last_weight and not float(m.group(1)) <= last_weight <= float(m.group(2)):
            warning = (f"{p['name']}'s last weight {last_weight:g} kg is outside weight_range "
                       f"{m.group(1)}–{m.group(2)} in config.yaml; widen it so their weigh-ins match.")
    if any(re.match(r"^\s*- ", lines[i]) for i in range(item_end, users[1])):
        warning = (warning + " " if warning else "") + (
            "config.yaml has more than one user: the scale's own figures are only exported for weigh-ins "
            "ble-scale-sync matches to the first one.")

    line = f'  rmsc04_profile_byte: "{byte:02x}"'
    ble = _block(lines, "ble")
    if ble:
        hit = next((i for i in range(ble[0] + 1, ble[1]) if re.match(r"^\s+rmsc04_profile_byte\s*:", lines[i])), None)
        if hit is not None:
            pad = re.match(r"^(\s+)", lines[hit]).group(1)
            lines[hit] = f'{pad}rmsc04_profile_byte: "{byte:02x}"'
        else:
            lines.insert(ble[0] + 1, line)
    else:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", "ble:", line, ""]
    new = "\n".join(lines)
    if new == text:
        return False, warning
    tmp = path.with_name(path.name + ".scale-hub.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    return True, warning


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
        self.last_sync: dict = {}

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
        pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(profiles)")}
        if "body_reference" not in pcols:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN body_reference TEXT NOT NULL DEFAULT 'general'")
        if "primary_source" not in pcols:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN primary_source TEXT NOT NULL DEFAULT 'scale'")
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
        reference = data.get("body_reference") if data.get("body_reference") in BODY_REFERENCES else "general"
        primary = data.get("primary_source") if data.get("primary_source") in COMPOSITION_SOURCES else "scale"
        color = data.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(data.get("color") or "")) else None

        with self.lock:
            if slug is None:
                base = slugify(name)
                slug, n = base, 2
                while self.conn.execute("SELECT 1 FROM profiles WHERE slug=?", (slug,)).fetchone():
                    slug, n = f"{base}-{n}", n + 1
                count = self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
                self.conn.execute(
                    "INSERT INTO profiles (slug, name, color, height_cm, birth_date, sex, athlete, start_weight, "
                    "body_reference, primary_source, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, name, color or PALETTE[count % len(PALETTE)], height, birth, sex, athlete, start_weight,
                     reference, primary, to_iso(utc_now())),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE profiles SET name=?, color=COALESCE(?, color), height_cm=?, birth_date=?, sex=?, athlete=?, "
                    "start_weight=?, body_reference=?, primary_source=? WHERE slug=?",
                    (name, color, height, birth, sex, athlete, start_weight, reference, primary, slug),
                )
                if cur.rowcount == 0:
                    raise KeyError(slug)
            self.conn.commit()
            # A new or changed reference weight can resolve readings nobody claimed.
            self.reassign_auto()
            saved = dict(self.conn.execute("SELECT * FROM profiles WHERE slug=?", (slug,)).fetchone())
        if self.get_setting("scale_profile") == slug:
            self.sync_scale()
        return saved

    def delete_profile(self, slug: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM profiles WHERE slug=?", (slug,))
            if cur.rowcount == 0:
                return False
            if self.get_setting("scale_profile") == slug:
                self.set_setting("scale_profile", None)
            # Its readings become unclaimed again; manual claims by others are untouched.
            self.conn.execute(
                "UPDATE measurements SET profile=NULL, assign_method='none', auto_profile=NULL, confidence=0, "
                "claimed_at=NULL WHERE profile=? OR (auto_profile=? AND assign_method!='manual')",
                (slug, slug),
            )
            self.conn.commit()
            self.reassign_auto()
            return True

    # --- settings and the scale profile -------------------------------------
    def get_setting(self, key: str) -> str | None:
        with self.lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key: str, value: str | None):
        with self.lock:
            if value is None:
                self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            self.conn.commit()

    def set_scale_profile(self, slug: str | None) -> dict:
        if slug is not None and slug not in self._profile_map():
            raise ValueError("unknown profile")
        self.set_setting("scale_profile", slug)
        return self.sync_scale()

    def sync_scale(self) -> dict:
        """Write the chosen scale profile into ble-scale-sync's config.yaml (it reloads it before
        the next scan). Returns the status the Settings tab shows."""
        slug = self.get_setting("scale_profile")
        status: dict = {"scale_profile": slug, "config": str(BSS_CONFIG), "checked": to_iso(utc_now())}
        if not slug:
            status.update(ok=True, message="Not managed: ble-scale-sync uses the profile in its own config.yaml.")
            self.last_sync = status
            return status
        with self.lock:
            p = self._profile_map().get(slug)
            last = self.conn.execute(
                "SELECT weight FROM measurements WHERE profile=? ORDER BY ts DESC LIMIT 1", (slug,)
            ).fetchone()
        age = age_on(p.get("birth_date"), utc_now()) if p else None
        if not p or not p.get("height_cm") or age is None or p.get("sex") not in ("male", "female"):
            status.update(ok=False, message="The scale profile needs height, birth date and sex.")
            self.last_sync = status
            return status
        byte = profile_byte(p["sex"], age)
        status.update(profile_byte=f"{byte:02x}", age=age, sex=p["sex"], height_cm=p["height_cm"])
        try:
            changed, warning = write_bss_config(BSS_CONFIG, p, byte, last["weight"] if last else None)
            bss_slug = first_user_slug(BSS_CONFIG)
            if bss_slug:
                self.set_setting("bss_slug", bss_slug)
            status.update(ok=True, changed=changed,
                          message=("Updated" if changed else "Already up to date") + f" ({BSS_CONFIG.name}).")
            if warning:
                status["warning"] = warning
        except (OSError, ValueError) as exc:
            status.update(ok=False, message=f"Could not update {BSS_CONFIG}: {exc}")
        self.last_sync = status
        log.info("Scale profile sync: %s", status["message"])
        return status

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
        """Store one reading. Returns (id, 'created'|'updated'|'duplicate', assigned profile).
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
                "SELECT * FROM measurements WHERE ts BETWEEN ? AND ? AND ABS(weight-?) < 0.05",
                (lo, hi, float(weight)),
            ).fetchone()
            if dup:
                # A weigh-in that carries more than the stored one (typically the
                # scale's segment impedances after a weight-only reading of the same
                # weight, e.g. two weigh-ins in a row) replaces its measurements.
                have = sum(dup[f] is not None for f in METRIC_FIELDS)
                new = sum(v is not None for v in values.values())
                if new <= have:
                    return dup["id"], "duplicate", dup["profile"]
                sets = ", ".join(f'"{f}"=?' for f in METRIC_FIELDS)
                self.conn.execute(
                    f"UPDATE measurements SET {sets}, raw=? WHERE id=?",
                    [*(values[f] for f in METRIC_FIELDS), json.dumps(payload), dup["id"]],
                )
                self.conn.commit()
                return dup["id"], "updated", dup["profile"]

            prof, guess, conf = self.estimate(float(weight), ts, user_slug) if assign else (None, None, 0.0)
            scale_for = self.get_setting("scale_profile") if ts >= utc_now() - timedelta(hours=1) else None
            cols = ["ts", "user_slug", "user_name", *METRIC_FIELDS, "raw",
                    "profile", "assign_method", "auto_profile", "confidence", "scale_for"]
            row = [to_iso(ts), user_slug, user_name, *(values[f] for f in METRIC_FIELDS), json.dumps(payload),
                   prof, "auto" if prof else "none", guess, conf, scale_for]
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
        scale_profile, bss_slug = self.get_setting("scale_profile"), self.get_setting("bss_slug")
        with_impedance = {
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT profile FROM measurements WHERE profile IS NOT NULL AND impedanceTrunk100kHz > 0"
            )
        }
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
            scale = None
            if r["skeletalMusclePercent"] is not None and r["bodyFatPercent"] is not None:
                # The scale's own analysis, computed for the profile ble-scale-sync sent it: the
                # Scale Hub profile set as the scale profile then (scale_for). Older or imported
                # readings only carry ble-scale-sync's user slug: its first user is taken to be
                # today's scale profile. The analysis depends on that profile's height, so it
                # belongs to this person only when they are that profile.
                scale = {f: r[f] for f in SCALE_FIELDS if r[f] is not None}
                scale["skeletalMuscleMass"] = round(r["skeletalMusclePercent"] * r["weight"] / 100, 1)
                split = renpho_split(r["weight"], r["bodyFatPercent"])
                scale.update(waterPercent=split["waterPercent"], boneMass=split["boneMass"],
                             muscleMass=split["muscleMass"], proteinMass=round(split["proteinMass"], 2),
                             bmr=split["bmr"])
                computed_for = r["scale_for"] or (
                    scale_profile if scale_profile and r["user_slug"] == bss_slug else r["user_slug"])
                scale["computed_for"] = computed_for
                scale["mine"] = bool(p and computed_for == p["slug"])
                d["scale"] = scale
            if p:
                model = estimate_composition(r["weight"], p, parse_ts(r["ts"]), d,
                                             impedance_only=p["slug"] in with_impedance)
                if "bmi" in model:
                    d["bmi"] = model["bmi"]
                if model.get("composition_source"):
                    d["model"] = {k: model[k] for k in MODEL_FIELDS if k in model}
                if model.get("composition_source") == "impedance":
                    # What the other equation set gives, so the two can be compared on real data.
                    other = "general" if model.get("reference") == "east_asian" else "east_asian"
                    alt = estimate_composition(r["weight"], {**p, "body_reference": other}, parse_ts(r["ts"]), d)
                    if alt.get("composition_source") == "impedance":
                        d["model_alt"] = {"reference": other, "bodyFatPercent": alt["bodyFatPercent"],
                                          "skeletalMuscleMass": alt["skeletalMuscleMass"]}
                        d["bodyFatPercentOther"] = alt["bodyFatPercent"]
                        d["skeletalMuscleMassOther"] = alt["skeletalMuscleMass"]
                use_scale = scale and scale["mine"] and p.get("primary_source", "scale") != "model"
                primary = d.get("model", {})
                if use_scale:
                    primary = {**primary, **{k: v for k, v in scale.items() if k not in ("computed_for", "mine")},
                               "composition_source": "scale"}
                for k, v in primary.items():
                    d[k] = v
                d["estimated"] = not use_scale
                std = standards(p, r["weight"], d)
                if std:
                    d["standards"] = std
                seg = segmental(p, r["weight"], d, d)
                if seg:
                    d["segmental"] = seg
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

    def delete_many(self, ids: list[int]) -> int:
        """Delete several readings at once (History tab); one reassignment pass at the end."""
        with self.lock:
            cur = self.conn.executemany("DELETE FROM measurements WHERE id=?", [(i,) for i in ids])
            self.conn.commit()
            if cur.rowcount:
                self.reassign_auto()
            return cur.rowcount


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
        if path == "/api/settings":
            return self._json(200, {"scale_profile": self.store.get_setting("scale_profile"),
                                    "sync": self.store.last_sync})
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
            if path == "/api/settings":
                body = self._body()
                if "scale_profile" in body:
                    sync = self.store.set_scale_profile(body["scale_profile"] or None)
                else:
                    sync = self.store.sync_scale()
                return self._json(200, {"scale_profile": self.store.get_setting("scale_profile"), "sync": sync})
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
            if path == "/api/measurements/delete":
                ids = self._body().get("ids")
                if not isinstance(ids, list) or not 0 < len(ids) <= 10000 or not all(
                        isinstance(i, int) and not isinstance(i, bool) for i in ids):
                    raise ValueError("ids must be a list of 1 to 10000 measurement ids")
                n = self.store.delete_many(ids)
                log.info("Deleted %d readings", n)
                return self._json(200, {"deleted": n})
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
        cols = ["id", "ts", "profile", "assign_method", "confidence", "composition_source", *METRIC_FIELDS,
                "skeletalMuscleMass", "impedance50kHz", "impedanceRatio",
                *(f"scale_{f}" for f in SCALE_FIELDS), "scale_computed_for",
                *(f"model_{f}" for f in ("bodyFatPercent", "skeletalMuscleMass", "skeletalMusclePercent", "reference"))]
        for r in rows:
            for f, v in (r.get("scale") or {}).items():
                r[f"scale_{f}"] = v
            for f, v in (r.get("model") or {}).items():
                r[f"model_{f}"] = v
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

    def resync():
        try:
            store.sync_scale()
        except Exception:  # never let the timer thread die
            log.exception("scale profile sync failed")
        t = threading.Timer(RESYNC_SECONDS, resync)
        t.daemon = True
        t.start()
    resync()

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
