# Scale Hub

Phone-free, always-on logging for the **Renpho MorphoScan Nova (R-MSC04)** on a Raspberry Pi.

```
 MorphoScan Nova ──BLE──▶ ble-scale-sync ──webhook──▶ Scale Hub (SQLite + web dashboard)
                     (on the Pi, 24/7)  └──────────▶ Garmin Connect (optional)
                                        └──────────▶ measurements.jsonl (backup)
```

- **Bluetooth + Garmin:** [ble-scale-sync](https://github.com/KristianP26/ble-scale-sync)
  (open source, GPL-3.0). It already implements the Nova's handshake (guest profile, clock,
  status acks), the weight and analysis frames, and the Garmin upload. It runs as the
  `ble-scale` service.
- **Your own server and web page:** this folder. `server.py` stores every weigh-in in
  `data/scale.db` and serves the web app on port 8080. It uses only the Python standard
  library and loads nothing from the internet, so it works offline.

What it does:

- Full body scans from the Nova: weight, the scale's own body fat, skeletal muscle and visceral
  fat (the same numbers as the Renpho app), and the ten segment impedances.
- Two sources side by side for every weigh-in: the scale's analysis and a model built on
  published equations ([why they differ](docs/rmsc04-notes.md#5-why-the-scale-and-the-model-disagree-so-much)).
- Renpho-style per-limb muscle and fat with Low / Standard / High, standard ranges, BMR, SMI and
  obesity degree, reconstructed from Renpho's own reports.
- A household without logins: weigh-ins are matched to people by weight, anyone can claim one.
- Guest weigh-ins with a printable report, and (experimental) the scale's own analysis for every
  household member.
- Everything learned about the scale, its Bluetooth protocol and the fixes:
  [docs/rmsc04-notes.md](docs/rmsc04-notes.md).

## What you need

- Raspberry Pi 3/4/5 or Zero 2 W with built-in Bluetooth, running Raspberry Pi OS Bookworm or
  newer (64-bit recommended), placed within a few metres of the scale. Tested on a Pi 4 with
  Debian 13 (trixie), kernel 6.18 and BlueZ 5.82.
- Optional: a Garmin Connect account.

## Install

Clone this repository to the Pi (for example to `~/scale-hub`), then run:

```bash
cd ~/scale-hub && chmod +x install.sh && ./install.sh
```

Then reboot once (`sudo reboot`) so the Bluetooth fix (see [Maintenance](#maintenance)) takes
effect, and:

1. Open `http://<pi-ip>:8080` on any device on your network and add a profile for each person,
   with height, birth date and sex (see [Household](#household-multi-user-no-login)).
2. In **Settings → Scale profile**, pick yourself. Scale Hub writes your height, birth date,
   sex and the scale's profile byte into `~/ble-scale-sync/config.yaml`. Widen
   `weight_range` there if anyone in the household falls outside it.
3. `sudo systemctl restart ble-scale`
4. Step on the scale barefoot, hold the handle and stay on until the display shows the full
   result (about 15 s after the weight settles). `journalctl -u ble-scale -f` shows a
   `Measurement:` line, and the web app shows the weigh-in.

**The scale talks to only one device at a time.** If the Renpho app is open on your phone it can
take the connection. Close it, or turn off Bluetooth on the phone.

## Garmin Connect

1. In `~/ble-scale-sync/.env` set `GARMIN_EMAIL=` and `GARMIN_PASSWORD=`.
2. Uncomment the `garmin` block in `~/ble-scale-sync/config.yaml`.
3. `cd ~/ble-scale-sync && ble-scale-sync setup-garmin --all-users` (handles 2FA).
4. `sudo systemctl restart ble-scale`

Strava, Intervals.icu, Home Assistant (MQTT), InfluxDB and others work the same way. See the
[exporter list](https://blescalesync.dev/exporters).

## Body composition

Every weigh-in keeps **both** the scale's own analysis and Scale Hub's model, side by side
(Body tab), and each person chooses which one their cards and charts show first
(*Show first* in their profile). The **Body tab** has a body map (muscle and fat per limb with % of standard and Low / Standard / High
like the Renpho app report, left/right lean balance, or lean change against about 30 days ago),
whole-body values against Renpho's standard ranges, the fat / muscle / other-lean split for each source,
and a scale-vs-model table. Why the two can differ by 10 points or more:
[docs/rmsc04-notes.md](docs/rmsc04-notes.md#5-why-the-scale-and-the-model-disagree-so-much).

1. **The scale's own analysis (Renpho).** The numbers the Renpho app shows, for the person set
   as the **scale profile** (see below). It follows height, weight and impedance, not sex or age.
2. **Scale Hub's model, from the scale's segment impedance.** When you stand barefoot,
   hold the handle and stay on until the display shows results, the Nova sends ten raw
   impedances: trunk, both arms and both legs, at 20 and 100 kHz. Scale Hub turns them into:
   - **Whole-body impedance at 50 kHz:** arm + trunk + leg averaged over both sides, with
     50 kHz interpolated between 20 and 100 kHz on a log scale. That is the input the
     published equations use.
   - **Fat-free mass and body fat:** NHANES III equations (Chumlea et al. 2002, Int J Obes
     26:1596).
   - **Skeletal muscle (kg and %):** Janssen et al. 2000 (J Appl Physiol 89:465), validated
     against MRI in 388 adults; standard error about 2.7 kg.
   - **BMR:** Katch-McArdle, from fat-free mass. Water, bone and muscle mass come from
     fat-free mass.
   - **Segments panel:** left/right lean balance for arms and legs, and each segment's lean
     change over about 30 days. These compare 100 kHz impedance: for the same limb, lower
     impedance means more lean tissue and water.
   - **100/20 kHz ratio:** a lower ratio means more cell mass relative to fluid. Use it as a
     trend, not a diagnosis.
   - **East Asian reference (per profile):** set *Body composition reference → East Asian*
     in a person's profile to use equations built for Asian adults (see below).
3. **BMI-based estimate.** Deurenberg's formula from weight, height, age and sex, used when
   there's no impedance. Once a person has impedance weigh-ins, their weigh-ins without the
   handle show weight and BMI only, so their trends don't jump between the two methods.

Limits: the equations are for adults (18+); children get the BMI estimate. Janssen's equation
underestimates skeletal muscle in Asian adults, and both equations were built on single-frequency
hand-to-foot analyzers, so a summed eight-electrode path is an approximation. Expect about
±3–4 % body fat against a lab method, and treat changes over weeks as more reliable than any
single number. Weigh in at the same time of day, because hydration moves impedance. The Renpho
app's per-limb fat and muscle figures use Renpho's own unpublished formula and are not
reproduced here.

### East Asian reference

Body build differs: at the same height, weight and impedance, East Asian adults on average
have a different split of fat, muscle and water than the mostly white samples behind the
standard equations, and more body fat at a given BMI. Each profile can pick its reference:

| | General | East Asian |
| --- | --- | --- |
| Fat-free mass and body fat | NHANES III (Chumlea 2002): US adults, hand-to-foot | **Chen et al. 2016** (Nutrients 8:654): 704 healthy Chinese adults, against DXA, hand-to-hand (left arm + right arm on the Nova). Men: 0.537·h²/Z − 0.126·age + 0.217·weight + 18.235. Women: 0.615·h²/Z − 0.144·age + 0.132·weight + 16.507 |
| Skeletal muscle | Janssen 2000 | Janssen 2000 **+ 2.45 kg**: the same paper found its equation underestimated MRI-measured muscle in Asians by 2.45 ± 1.61 kg |
| BMI categories | WHO: healthy < 25, overweight 25–30, obese ≥ 30 | WHO 2004 Asia action points: healthy < 23, increased risk 23–27.5, high risk ≥ 27.5 |

The note under the chart also shows what the other reference gives for the same weigh-in, so
you can compare both on your own readings. The difference goes either way depending on each
person's arm vs leg impedance. Limits: Chen's equation is Chinese-derived and not separately
validated in Japanese or Korean adults, and the 2.45 kg is an average offset. For South Asian
backgrounds (Indian, Pakistani and others), body fat at a given BMI is higher still, and no
equation here was built for them.

### Scale profile (Settings tab)

At the start of each weigh-in ble-scale-sync sends the scale one profile, and the scale computes
its own analysis for it, before anyone knows who is on the scale. In **Settings → Scale profile**
pick that person. Scale Hub then writes their name, height, birth date, sex, athlete flag and last
weight into the first user of `~/ble-scale-sync/config.yaml`, plus `ble.rmsc04_profile_byte`
worked out from sex and age (`0x80` for male + age in years). It re-checks every 6 hours, so the
byte follows birthdays. ble-scale-sync reloads the file before its next scan. Comments, exporters
and other users in the file are left as they are, and so is the user's slug (Garmin keeps its
login per slug).

The scale's figures are shown for the scale profile only; everyone else gets the model. Keep
**one user** in `config.yaml` for this: every weigh-in is then matched to it, so the scale's
figures and impedances come through for everyone, and Scale Hub works out who it was. With
several users there (for example Garmin per person), weigh-ins matched to the others still get
impedances and the model, but not the scale's own figures.

### Renpho figures for everyone

Turn on **Settings → Scale profile → Renpho figures for everyone** to get the scale's own
analysis for every household member, not only the scale profile. Scale Hub writes
`~/ble-scale-sync/household.json` (each person's height, profile byte and usual weight), and the
ble-scale-sync household patch (`ble-scale-sync/patches/`, applied by `install.sh`) sends the
profile of whoever it matches by weight once the weight settles, about 6 seconds before the scale
starts its body scan. Each result is checked: the scale's BMI has to give back that person's
height within 1.5 cm, otherwise the result isn't credited to them and they get the model. The
Body tab says which happened. If two people weigh within about a kilo of each other, the patch
doesn't guess and the scale keeps the scale profile's figures.

Confirmed on the scale: a 163 cm household member got a result computed for 163 cm while the
Pi had connected with a 180 cm profile. One habit makes it work every time: **step on after
the scale has woken up and the Pi has connected** (a second or two). If someone is already
standing on it when the Pi connects, the weight never "settles" while connected, so the patch
can't tell who it is and that weigh-in falls back to the model (unless it's the scale profile
person).

### Guest weigh-in

For a visitor: **Guest weigh-in** (person menu or Settings → Guests), enter height, age and sex,
tap **Start**. Scale Hub makes a temporary guest profile the scale profile and restarts the
Bluetooth listener so the scale gets it straight away (about 12 seconds; the app counts down,
then says when to step on). The next weigh-in is the guest's, and the scale profile switches
straight back. The guest sees a full report (weight, body fat, muscle per limb, standards,
scale vs model) to print or delete; **Done** returns to normal. Guest weigh-ins never appear in
anyone's history or matching; past reports stay under Settings → Guests until deleted. A guest
session with no weigh-in ends by itself after 10 minutes.

The restart works because Scale Hub and ble-scale-sync run as the same user: Scale Hub sends
ble-scale-sync a SIGTERM and systemd starts it again with the new profile. Set
`SCALE_HUB_RESTART_BSS=0` in the scale-hub service to turn that off (the guest profile then
applies from the listener's next scan cycle, which can be too late).

## Household (multi-user, no login)

Everyone shares one web app. Tap the name at the top right to switch person, edit a profile or
add one; the page remembers the choice on that device. A profile has a name, colour, and
optionally height, birth date, sex, usual weight, which equations the model uses, and whether
the scale's analysis or the model is shown first.

**Automatic matching.** Each weigh-in goes to the person whose recent readings it is closest to
(the median of their nearest readings in time; the allowed gap widens the longer they haven't
weighed in). For people with no history yet, their *usual weight* is used. A weigh-in stays
**unclaimed** when nobody is close enough (a visitor) or when two people are too close to call.
The badge shows how sure the match was, for example `auto 96%`.

**Claiming.** Unclaimed weigh-ins show as a banner on Overview and under **History → Needs a
name**; Overview also lists the latest few from everyone.
- **That's me** gives the reading to whoever is selected at the top. **Someone else…** gives it
  to another person, or marks it **Guest / not us**.
- Claiming a reading that was only matched automatically happens straight away. Taking one that
  someone already **claimed by hand** asks first: *"already claimed by Mom on … Move it to Kid?"*
- **Back to automatic** undoes a manual claim.
- A manual claim never gets changed automatically. Each claim also improves future matching: one
  claim can settle a whole batch of that person's unclaimed readings.

**Body composition per person.** The model is calculated for the person each weigh-in belongs
to, from the impedance plus their height, age and sex, so moving a reading to someone else
recalculates it for them. The scale's own analysis belongs to whoever the scale computed it for
(the scale profile, or with household matching the person it matched), checked against that
person's height.

**Garmin with several people.** Garmin uploads happen in ble-scale-sync at weigh-in time, using
its own matching by `weight_range`. A claim on the dashboard does not move a Garmin upload. For
per-person Garmin, list each person in `config.yaml` with their own `weight_range` and Garmin
exporter ([multi-user guide](https://blescalesync.dev/multi-user)).

## Web app

Four tabs (a bottom tab bar on phones), light and dark mode, kg or lb:

- **Overview:** the selected figure large with its 7, 30 and 90-day change and a chart (7-reading
  average; the other source dashed), every body composition figure in one list (tap one to chart
  it), and the latest weigh-ins from everyone.
- **Body:** the body map (muscle or fat per limb with % of standard, left/right balance, or lean
  change over about 30 days), each segment's figures, the standard ranges with obesity degree,
  SMI and BMR, scale vs model, and the raw impedances.
- **History:** weigh-ins that need a name, and each person's weigh-ins for the chosen range, with
  **Move…**, bulk delete and CSV export.
- **Settings:** people, the scale profile and household matching, guest reports, and how the
  numbers are made.

### API

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/ingest` | ble-scale-sync webhook body. Needs `X-Api-Key` when `SCALE_HUB_TOKEN` is set |
| GET | `/api/profiles` | People, with reading counts |
| POST | `/api/profiles` / `/api/profiles/<slug>` | Create / update a person |
| DELETE | `/api/profiles/<slug>` | Remove a person; their readings become unclaimed |
| GET | `/api/measurements?days=90&profile=dad` | `profile=_unassigned` for unclaimed, `days=all` for everything |
| GET | `/api/recent?limit=15` | Latest weigh-ins from everyone |
| POST | `/api/measurements/<id>/claim` | `{"profile": "dad"}` (`null` = guest). Returns 409 if someone else claimed it by hand; resend with `"confirm": true` |
| POST | `/api/measurements/<id>/release` | Back to automatic matching |
| GET | `/api/export.csv?days=all&profile=dad` | CSV download |
| DELETE | `/api/measurements/<id>` | Delete a reading |
| POST | `/api/measurements/delete` | `{"ids": [1, 2, 3]}`: delete several readings (History tab: tick rows or "Date" for all in range, then **Delete**) |
| GET / POST | `/api/settings` | `{"scale_profile": "dad"}` (`null` = not managed), `{"household": true}`; returns the sync and household status |
| GET / POST | `/api/guest` | Guest session status / start one with `{"name", "height_cm", "age", "sex"}` |
| POST | `/api/guest/cancel`, `/api/guest/close` | Cancel a waiting guest session / finish one (back to normal) |

Each measurement carries the figures shown first at the top level, plus `scale` (the scale's own
analysis, `computed_for`, `verified`, `mine`), `model` and `model_alt` (the two equation sets),
`standards` and `segmental` (Renpho-style ranges and per-limb figures).

There's no login: anyone on your network can claim or edit. Only `/api/ingest` needs the key, so
nothing else can inject readings. A repeat of the same reading (same weight within 2 minutes) is
ignored, so webhook retries and re-imports never create duplicates. If the repeat carries more
measurements (for example the scale's segment impedances after a weight-only reading), it
replaces the stored one.

## Maintenance

```bash
systemctl status scale-hub ble-scale        # are both running?
journalctl -u ble-scale -f                  # live Bluetooth log
git pull && ./install.sh && sudo systemctl restart scale-hub ble-scale   # update (also rebuilds ble-scale-sync with ble-scale-sync/patches/)
cp ~/scale-hub/data/scale.db ~/scale-backup-$(date +%F).db                       # backup
python3 ~/scale-hub/server.py --import-jsonl ~/ble-scale-sync/measurements.jsonl # rebuild DB from backup
```

Troubleshooting for the Bluetooth side: <https://blescalesync.dev/troubleshooting>.
**The Nova hangs up about 5.5 s after connecting** (weight only, no body composition) when
Linux refuses its request for a 30-45 ms connection interval. That happens once bluetoothd's
GAP plugin has loaded the scale's advertised preference (10-12.5 ms). `install.sh` runs
bluetoothd with `-P gap` to prevent it (`/etc/systemd/system/bluetooth.service.d/scale-hub.conf`).
If it still happens, clear what is stored for the scale and reboot:

```bash
sudo rm -rf /var/lib/bluetooth/*/<SCALE-MAC> /var/lib/bluetooth/*/cache/<SCALE-MAC>
sudo reboot
```

Check with `sudo btmon`: a working session shows `LE Remote Connection Parameter Request Reply`
and `LE Connection Update Complete` with a 45 ms interval, not a `Negative Reply`.

## Remote access (outside your home)

The dashboard has no login, so don't port-forward it. Use Tailscale or your router's VPN
(for example UniFi Teleport or WireGuard) to reach `http://<pi>:8080` from anywhere.
