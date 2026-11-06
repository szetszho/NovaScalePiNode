#!/usr/bin/env python3
"""Decode a Renpho R-MSC04 session from an Apple PacketLogger text export.

Export with full packet bytes (each line ends with the raw hex), then:
    python3 tools/decode_packetlogger.py capture.txt [scale MAC]

Prints every write and notification between the phone and the scale (live 0x21 weight
frames skipped), names each frame, and decodes 0x25 composition records. Standard
library only. Frame layout: 55 AA <cmd> <len u16 BE> <payload> <checksum = sum & 0xFF>.
"""
import re
import sys
from collections import defaultdict

MAC = sys.argv[2] if len(sys.argv) > 2 else "D0:5C:00:23:2D:A1"
NAMES = {
    0x20: "status", 0x21: "live weight", 0x22: "reply b2", 0x23: "reply b3", 0x24: "final weight",
    0x27: "reply b7", 0x28: "reply b8", 0xB0: "ack status", 0xB2: "profile", 0xB3: "clock",
    0xB6: "delete stored record", 0xB7: "name", 0xB8: "settings",
}
STATES = {0x01: "measuring", 0x04: "ending session", 0x05: "not measuring", 0x09: "weight locked",
          0x11: "measurement complete"}
SEGS = ("trunk", "L arm", "R arm", "L leg", "R leg")


def describe(v: bytes) -> str:
    if len(v) < 6 or v[:2] != b"\x55\xaa":
        return ""
    cmd, p = v[2], v[5:-1]
    ok = "" if sum(v[:-1]) & 0xFF == v[-1] else "  BAD CHECKSUM"
    name = NAMES.get(cmd, f"cmd {cmd:02x}")
    if cmd == 0x20 and len(p) >= 5:
        return f"{name}: seq {p[0]}, state 0x{p[1]:02x} ({STATES.get(p[1], '?')}), stored {p[3]}, [9] {p[4]}{ok}"
    if cmd == 0xB2 and len(p) == 9:
        sex = "male" if p[6] & 0x80 else "female"
        return (f"{name}: seq {p[0]}, slot {p[1]:02x}, height {int.from_bytes(p[2:4], 'big') / 10} cm, "
                f"last weight {int.from_bytes(p[4:6], 'big') / 100} kg, byte 0x{p[6]:02x} ({sex}, age {p[6] & 0x7F}), "
                f"tail {p[7]:02x} {p[8]:02x}{ok}")
    if cmd == 0xB3 and len(p) == 11:
        zone = int.from_bytes(p[8:10], "big")
        zone_s = f"UTC-{(zone & 0x7FFF) / 60:g}h" if zone & 0x8000 else f"UTC+{zone / 60:g}h"
        return f"{name}: seq {p[0]}, unix {int.from_bytes(p[4:8], 'big')}, zone 0x{zone:04x} ({zone_s}){ok}"
    if cmd == 0xB7 and len(p) >= 6:
        n = p[5]
        return f"{name}: seq {p[0]}, slot {p[1]:02x}, [{p[2:5].hex(' ')}], {p[6:6 + n].decode('ascii', 'replace')!r}{ok}"
    if cmd == 0x24:
        return f"{name}: {int.from_bytes(v[-3:-1], 'big') / 100} kg{ok}"
    return f"{name}: {p.hex(' ')}{ok}"


def record(f: bytes) -> str:
    u = lambda i: int.from_bytes(f[i:i + 2], "big")  # noqa: E731
    if f[2] == 0x26:
        return f"0x26 stored record seq {f[5]}, {int.from_bytes(f[7:11], 'big')} s old: {f.hex(' ')}"
    z20 = " / ".join(f"{u(12 + 2 * i) / 10}" for i in range(5))
    z100 = " / ".join(f"{u(22 + 2 * i) / 10}" for i in range(5))
    ok = "" if sum(f[:-1]) & 0xFF == f[-1] else "  BAD CHECKSUM"
    return (f"0x25 record seq {f[5]}: {int.from_bytes(f[7:11], 'big') / 100} kg, slot {f[32]:02x}, "
            f"fat {u(33) / 10} %, BMI {u(35) / 10}, skeletal muscle {u(37) / 10} %, visceral {u(39)}\n"
            f"    impedance ({', '.join(SEGS)}) 20 kHz {z20} ohm, 100 kHz {z100} ohm{ok}")


frags = defaultdict(list)
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if MAC not in line or "ATT" not in line:
        continue
    hx = re.findall(r"(?:\b[0-9A-F]{2}\b ?)+\s*$", line)
    if not hx:
        continue
    b = bytes.fromhex(hx[-1].replace(" ", ""))
    if len(b) < 11 or b[8] not in (0x12, 0x1B, 0x1D):  # ATT write, notification, indication
        continue
    v, ts, d = b[11:], line[7:19], "->" if b[8] == 0x12 else "<-"
    if v[:3] == b"\x55\xaa\x21":
        continue
    if v[:1] in (b"\xad", b"\xae", b"\xaf"):  # record fragments: marker, seq, fragments left
        frags[v[1]].append(v)
        if v[2] == 0:
            f = b"".join(x[3:] for x in sorted(frags.pop(v[1]), key=lambda x: -x[2]))
            print(f"{ts} {d} {record(f)}")
        continue
    print(f"{ts} {d} {describe(v) or v.hex(' ')}")
