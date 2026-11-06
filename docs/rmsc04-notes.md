# Renpho R-MSC04 (MorphoScan Nova) + Scale Hub: knowledge base

Everything learned while getting this scale working on a Raspberry Pi, for the owner and for
future agents working on this repo. Facts are marked **verified** (seen in logs or captures
here) or **inferred** (reasoning, not yet tested).

## 1. System overview

```text
R-MSC04 ──BLE──▶ ble-scale-sync (Node, ~/ble-scale-sync/src, built from GitHub)
                   │ webhook POST /api/ingest (X-Api-Key = SCALE_HUB_TOKEN from ~/ble-scale-sync/.env)
                   │ file exporter ./measurements.jsonl (backup, re-import with --import-jsonl)
                   ▼
                 Scale Hub (server.py, stdlib only, SQLite in data/scale.db, static/index.html)
                   │ writes the "scale profile" back into ~/ble-scale-sync/config.yaml
                   ▼
                 ble-scale-sync reloads config.yaml before its next scan
```

- `install.sh`: apt packages, Node 22, clones and builds ble-scale-sync (it is **not on npm**),
  Python venv for the Garmin uploader, `.env` with the ingest token, config from
  `ble-scale-sync/config.yaml.template`, systemd units, and the bluetoothd `-P gap` override.
  Safe to re-run; it is also the update path.
- systemd: `scale-hub.service` (env `SCALE_HUB_PORT`, `SCALE_HUB_BSS_CONFIG`) and
  `ble-scale.service` (`CONTINUOUS_MODE=true`, venv first in `PATH`).
- Tested setup: Raspberry Pi 4 Model B Rev 1.5, Debian 13 (trixie), kernel 6.18.50+rpt-rpi-v8,
  BlueZ 5.82, ble-scale-sync 1.39.1.

## 2. The 5.5-second drop (solved, verified)

Symptom: every weigh-in weight-only; ble-scale-sync debug summary
`session ended ~4.7 s after the handshake ... composition record no`.

Cause, from `btmon`:
1. bluetoothd's GAP plugin (`profiles/gap/gas.c`) reads the scale's Peripheral Preferred
   Connection Parameters (0x2A04 = `08 00 0a 00 00 00 c8 00`: 10–12.5 ms, latency 0, 2 s) and
   loads them into the kernel (`MGMT Load Connection Parameters`). Kept in kernel memory and in
   `/var/lib/bluetooth/<adapter>/<scale>/info`.
2. About 0.5 s after connecting, the scale sends LE Remote Connection Parameter Request
   30–45 ms, latency 2, timeout 5 s.
3. Linux `hci_le_remote_conn_param_req_evt()` (`net/bluetooth/hci_event.c`) refuses it because
   `max > hcon->le_conn_max_interval` (12.5 ms): Negative Reply.
4. The scale asks again about 2.5 s later, is refused again, and disconnects (0x13) about
   5.5 s after connecting.

The iPhone accepts the same request and keeps the link. The first weigh-in after a fresh
install works, because nothing is stored yet and the kernel default max (50 ms) applies.

Fix: bluetoothd with `-P gap` (`/etc/systemd/system/bluetooth.service.d/scale-hub.conf`),
delete the stored device directories, **reboot** (restarting bluetoothd does not clear the
kernel's copy). Check with `sudo btmon`: `LE Remote Connection Parameter Request Reply` and
`LE Connection Update Complete` at 45 ms.

Things that did **not** matter: writing b2 again after 0x24 (a patch for that was tried and
removed), `BLE_RMSC04_HANDSHAKE` modes, staying still, phone Bluetooth (still keep the Renpho
app closed: the scale takes one connection at a time).

## 3. Protocol (verified from three iOS captures and Pi debug logs)

Service 0x1A10: write 0x2A11 (Write Request), notify 0x2A10 (live weight), indicate 0x2A12
(everything else). Frame: `55 AA <cmd> <len u16 BE> <payload> <checksum = sum of all previous
bytes & 0xFF>`. Decoder: `tools/decode_packetlogger.py <PacketLogger text export with bytes>`.

| Dir | Cmd | Payload | Meaning |
|---|---|---|---|
| ← | 0x20 | seq, state, [7]=01, stored count, [9] | status. States: 01 measuring, 09 weight locked, 11 measurement complete, 05 not measuring, 04 ending. [9] was 0x64 (100) in every session: probably battery % (inferred) |
| → | b0 | status seq, 01 | ack of each 0x20 |
| ← | 0x21 | ..., weight u16 BE /100 | live weight (notify 0x2A10) |
| ← | 0x24 | seq, 11, ..., weight | settled weight |
| → | b3 | seq, 07 01 01, unix u32 BE, zone u16, 00 | clock. **Zone: minutes east; west of UTC = 0x8000 \| minutes** (UTC−6 = `0x8168`) |
| → | b2 | seq, slot, height u16 (0.1 cm), last weight u16 (0.01 kg), **profile byte**, 03, 02 | profile. App writes its user slot (01); ble-scale-sync writes guest slot 09 with tail `ff 02` |
| → | b7 | seq, slot, 00 01 00, name length, ASCII name | user name (not needed) |
| → | b8 | seq, slot, 10 × 00 | settings, all zeros (not needed) |
| → | b6 | seq, 01 | sent after a 0x26 replay; deletes the synced stored record (inferred) |
| ← | 0x22/0x23/0x27/0x28 | write seq, result | replies to b2/b3/b7/b8 (b2 reply carries the slot) |
| ← | 0x25 | 42 bytes in 3 fragments (`ad`/`ae`/`af`, seq, fragments left) | composition record |
| ← | 0x26 | 46 bytes | stored weigh-in taken while nobody was connected; [7..10] = seconds old |

0x25 record (offsets in the reassembled 42-byte frame, big-endian): weight u32 at 7 (/100),
20 kHz impedances u16 ×5 at 12 (/10 Ω: trunk, L arm, R arm, L leg, R leg), 100 kHz ×5 at 22,
**[32] = slot the record was computed for** (01 app user, 09 guest), fat u16 at 33 (/10 %),
BMI at 35 (/10), skeletal muscle % at 37 (/10), visceral fat at 39.

**Profile byte = (0x80 if male) | age in years** (verified by changing one setting at a time
in the Renpho app: male 30 = 0x9E, female 30 = 0x1E, female 32 = 0x20). Athlete mode and
activity level are not sent anywhere. ble-scale-sync's guest byte 0xA9 = male 41.

Timing (app sessions): 0x24 at +3 s, 0x09 about 6 s later, 0x11 + 0x25 about 16 s after 0x24.
App seq numbers keep counting across sessions.

## 4. What the scale's own body fat depends on (verified)

Same scale, near-identical impedances, slot 01:

| Profile sent | Fat | Skeletal muscle | Visceral | BMI |
|---|---|---|---|---|
| male 30, 180 cm | 11.3 % | 50.4 % | 1 | 21.7 |
| female 32, 180 cm | 11.2 % | 50.6 % | 1 | 21.7 |
| female 30, 180 cm | 11.5 % | 50.3 % | 1 | 21.7 |
| male 30, **175 cm** (Pi, guest slot) | 15.6 % | 47.9 % | 3 | 22.9 |

The Renpho app showed the same numbers. **The scale's analysis follows height, weight and
impedance, not sex or age.** BMI in the record reveals the height used:
`height = sqrt(weight / BMI)`.

## 5. Why the scale and the model disagree so much

Example (male 30, 180 cm, 70.25 kg): scale 11.3 %, model General 23.6 %, model East Asian 17.1 %.

1. **Different formulas, no shared truth.** Renpho's is unpublished and, per section 4,
   ignores sex and age. Physiologically that's implausible: at the same impedance, women
   carry roughly 8–12 points more body fat. The published equations include sex and age, but
   were built on general populations (NHANES III: US adults; Chen 2016: Chinese adults).
2. **Sensitivity to the impedance path.** NHANES III male: FFM = −10.678 + 0.262·W + 0.652·H²/Z +
   0.015·Z, with Z the hand-to-foot resistance at 50 kHz. Scale Hub has no hand-to-foot
   measurement; it approximates one as arm + trunk + leg (averaged over sides) and
   interpolates 50 kHz between 20 and 100 kHz. Here Z50 ≈ 564 Ω, H²/Z ≈ 57.5. For the
   equation to give Renpho's 11 %, Z would have to be about 460 Ω, about 18 % lower. A
   summed eight-electrode path and a real single-frequency analyzer can differ by that much.
3. **Athletes.** General-population equations tend to overstate fat in very lean, trained
   people (more muscle water per unit of impedance). The owner trains a lot, and the BMI is
   21.7. Renpho's figure may be closer for them, but nobody knows without DXA.
4. **East Asian variant** uses hand-to-hand impedance (arms only), so it reacts to arm vs leg
   differences in a different way.

Practical rule: pick one source per person and watch the trend; ±3–4 % absolute error is
typical even for good BIA. Scale Hub stores both for every weigh-in and lets each profile
choose which is shown first.

## 6. Scale Hub design (current)

- Every reading stores the raw payload, weight, ble-scale-sync's figures, the ten segment
  impedances, assignment state, and `scale_for` (the scale profile at ingest time).
- `/api/measurements` returns per row: top-level primary values (by the profile's
  `primary_source`, `scale` or `model`), `scale` (the scale's figures, `computed_for`, `mine`),
  `model` (published equations), `model_alt` (the other equation set).
- The scale's figures count as the scale's only when ble-scale-sync sent
  `skeletalMusclePercent` (it does so only when it used the record's composition).
- **Scale profile** (Settings tab, `settings.scale_profile`): Scale Hub writes that person's
  name, height (inches if `scale.height_unit: in`), birth date, sex, athlete flag and last
  weight into the **first user** of ble-scale-sync's `config.yaml`, plus
  `ble.rmsc04_profile_byte` from the formula. Line edits only (comments, exporters and other
  users stay). The **slug is left alone** (Garmin keeps tokens per user slug). Re-synced at
  start-up, on profile save and every 6 hours (birthdays). ble-scale-sync rewrites
  `config.yaml` itself (`last_known_weight`) with a comment-preserving YAML library.
- Multi-user: keep **one user** in ble-scale-sync's config. Every weigh-in is then matched to
  it, the scale's figures are exported for every weigh-in (computed for the scale profile's
  height), the impedances too, and Scale Hub assigns the person by weight. The scale's
  figures are shown only for the scale profile; everyone else gets the model. With several
  users in config (for example Garmin per person), the scale's figures only come through for
  weigh-ins ble-scale-sync matches to the first user, but impedances come for all, so the
  model still works.
- Duplicates: the same weight within 2 minutes is one weigh-in. A repeat with more fields (for
  example impedances after a weight-only reading) replaces it.

## 6b. Renpho app report: segments and standards (reconstructed from two reports)

The app's per-limb figures are **not sent by the scale**; the app computes them. Two "Body
Composition Analysis Reports" (male, 30, 180 cm, 11.3 % fat both times):

| | Report 1 (Oct 26) | Report 2 (Nov 5, same weigh-in as iOS capture 1) |
|---|---|---|
| Weight / FFM | 67.85 / 60.18 kg | 70.25 / 62.31 kg |
| 20 kHz R arm, L arm, trunk, R leg, L leg | 343.1, 349.1, 20.8, 268.9, 280.1 Ω | 319.2, 334.3, 16.8, 258.4, 266.7 Ω |
| Muscle R arm, L arm, trunk, R leg, L leg | 3.29, 3.27, 26.20, 9.87, 9.87 kg | 3.49, 3.44, 27.14, 10.25, 10.25 kg |
| Segment standard arm / trunk / leg | 3.19 / 25.45 / 8.88 kg | 3.24 / 25.85 / 9.02 kg |
| Fat R arm, L arm, trunk, R leg, L leg | 0.26, 0.27, 3.98, 1.42, 1.42 kg | 0.24, 0.28, 4.33, 1.47, 1.47 kg |
| BMR / SMI / obesity degree | 1669 / 8.1 / 95 % | 1714 / 8.5 / 98 % |

What fits both (implemented in `server.py`: `renpho_split`, `standards`, `segmental`):

- **Fat-free split**: water = 73.3 % of FFM, bone ≈ 6.7 %, muscle = FFM − bone, protein =
  FFM − water − bone. **BMR = Katch-McArdle, 370 + 21.6 × FFM** (exact for both).
- **Standards**: standard weight = BMI 22 (71.3 kg), range ±15 %. Fat mass 12–24 % of
  standard weight for men. Muscle, skeletal muscle, water, protein and bone standards =
  0.93 / 0.58 / 0.73 / 0.20 / 0.067 × the fat-free mass at standard weight, each ±10 %. Female
  fat range 20–32 % is an **assumption**.
- **Segment muscle**: the five segments hold 93.75 % of muscle, split by **fixed shares**: arms
  12.6 %, trunk 49.8 %, legs 37.6 % (the trunk's impedance fell 19 % between the reports and its
  share stayed put). Renpho barely uses segment impedance: identical legs both times, arms
  differ by about 1 %. Scale Hub splits each left/right pair by 1/Z instead, so real asymmetry shows.
- **Segment standards**: 0.7123 × (weight + standard weight) / 2, split arm 6.43 %, trunk
  51.32 %, leg 17.91 % (each). Bands Low < 90 %, High > 110 % (111 % and 114 % legs were "High").
- **Segment fat**: arms 6.7 %, trunk 53.2 % (51.9 and 54.6 % in the reports: the one part that
  varies), legs 37.0 % of fat mass; standards are standard fat × 5.37 / 35.5 / 13.9 %; Low < 80 %
  (79.3 % Low, 82.1 % Standard); High > 160 % assumed.
- **SMI** = (arm + leg muscle) / height²; **obesity degree** = weight / standard weight.

Scale Hub reproduces both reports to within about 0.2 kg per segment, with exact pair totals and
trunk muscle. Still open: the body score (76, 79), subcutaneous fat (8.1 % both), metabolic age,
WHR, and a woman's report.

## 7. How to test and debug

- Live log: `journalctl -u ble-scale -f -a` (`-a`, or journald shows `[blob data]`).
  `runtime.debug: true` in config for frame-level logs; turn off after.
- Radio: `sudo btmon -w ~/scale.btsnoop | tee ~/btmon.txt`, then
  `grep -n -E -A6 "Connection Complete|Connection Parameter|Connection Update Complete|Disconnect" ~/btmon.txt`.
- iPhone capture: Apple Bluetooth logging profile on the iPhone + PacketLogger (Additional
  Tools for Xcode), File → New iOS Trace, export as text **with packet bytes**, then run
  `tools/decode_packetlogger.py`.
- Server: `python3 -m py_compile server.py`; run locally with
  `SCALE_HUB_BSS_CONFIG=/tmp/x.yaml python3 server.py --db /tmp/x.db --port 8765`.
- UI: Playwright is available in the cloud dev container; check 390 px width for horizontal
  overflow on every tab.

## 8. Open questions

- Does a b2 sent after 0x24 (for example with the matched user's height) change the 0x25
  record? Would let the scale compute for whoever is on it. Untested.
- Is status [9] the battery level?
- 0x26 stored records: ble-scale-sync ignores them; they could recover weigh-ins taken while
  the Pi was down.
- Female owners and other units: confirm the profile byte formula and the sex-independence.
