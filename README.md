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
  `data/scale.db` and serves the dashboard on port 8080. It uses only the Python standard
  library and loads nothing from the internet, so it works offline.

## What you need

- Raspberry Pi 3/4/5 or Zero 2 W with built-in Bluetooth, running Raspberry Pi OS Bookworm or
  newer (64-bit recommended), placed within a few metres of the scale.
- Optional: a Garmin Connect account.

## Install

Copy this folder to the Pi (for example to `~/scale-hub`), then run:

```bash
cd ~/scale-hub && chmod +x install.sh && ./install.sh
```

Then reboot once (`sudo reboot`) so the Bluetooth fix below takes effect, and follow the
steps it prints:

1. `nano ~/ble-scale-sync/config.yaml` and set your name, height, birth date, gender and
   weight range.
2. `cd ~/ble-scale-sync && ble-scale-sync validate`
3. `sudo systemctl restart ble-scale`
4. Step on the scale and stay on until the display shows the results (about 15 s after the
   weight settles). `journalctl -u ble-scale -f` shows a `Measurement:` line.
5. Open `http://<pi-ip>:8080` on any device on your network and add a profile for each person
   (see [Household](#household-multi-user-no-login)).

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
(*Show first* in their profile). The **Body tab** has a body map (left/right lean balance, or
lean change against about 30 days ago), the fat / muscle / other-lean split for each source,
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

## Household (multi-user, no login)

Everyone shares one dashboard. Tap your name at the top to see your own numbers; the page
remembers the choice on that device. **+ Add person** creates a profile (name, colour, and
optionally height, birth date, sex and usual weight).

**Automatic matching.** Each weigh-in goes to the person whose recent readings it is closest to
(the median of their nearest readings in time; the allowed gap widens the longer they haven't
weighed in). For people with no history yet, their *usual weight* is used. A weigh-in stays
**unclaimed** when nobody is close enough (a visitor) or when two people are too close to call.
The badge shows how sure the match was, for example `auto 96%`.

**Claiming.** The panel at the top lists unclaimed weigh-ins and the latest few from everyone.
- **That's me** gives the reading to whoever is selected at the top. **Someone else…** gives it
  to another person, or marks it **Guest / not us**.
- Claiming a reading that was only matched automatically happens straight away. Taking one that
  someone already **claimed by hand** asks first: *"already claimed by Mom on … Move it to Kid?"*
- **Back to automatic** undoes a manual claim.
- A manual claim never gets changed automatically. Each claim also improves future matching: one
  claim can settle a whole batch of that person's unclaimed readings.

**Body composition per person.** Every weigh-in is calculated for the person it belongs to,
from the impedance plus their height, age and sex (see [Body composition](#body-composition)).
Moving a reading to someone else recalculates it for them.

**Garmin with several people.** Garmin uploads happen in ble-scale-sync at weigh-in time, using
its own matching by `weight_range`. A claim on the dashboard does not move a Garmin upload. For
per-person Garmin, list each person in `config.yaml` with their own `weight_range` and Garmin
exporter ([multi-user guide](https://blescalesync.dev/multi-user)).

## Dashboard

- One card per metric: latest value and change over 7 and 30 days (green means a change in the
  healthy direction). Click a card to chart that metric.
- The chart shows each reading as a dot (dashed for automatic matches, solid for claimed) and a
  7-reading moving average as the line.
- Per-person history table with a **Move…** menu on every row, kg/lb switch, CSV download.

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

There's no login: anyone on your network can claim or edit. Only `/api/ingest` needs the key, so
nothing else can inject readings. A repeat of the same reading (same weight within 2 minutes) is
ignored, so webhook retries and re-imports never create duplicates. If the repeat carries more
measurements (for example the scale's segment impedances after a weight-only reading), it
replaces the stored one.

## Maintenance

```bash
systemctl status scale-hub ble-scale        # are both running?
journalctl -u ble-scale -f                  # live Bluetooth log
./install.sh && sudo systemctl restart ble-scale   # update (pulls and rebuilds ble-scale-sync)
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
