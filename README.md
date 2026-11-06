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

Then follow the steps it prints:

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

## Body fat accuracy

By default, ble-scale-sync gets the **weight** from the Nova, and body fat is a BMI-based estimate.
To use the scale's own 8-electrode body fat, skeletal muscle and visceral fat, set
`ble.rmsc04_profile_byte` in `config.yaml`. That byte comes from one Android Bluetooth HCI log
of the Renpho app. Steps:
<https://blescalesync.dev/guide/scales/renpho#own-body-fat>. Segment impedances
(arms, legs, trunk) are shown on the dashboard when the scale sends them.

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

**Body composition per person.** BMI, body fat, water, muscle, bone, BMR and metabolic age are
estimated from weight plus that person's height, age and sex (the same formulas ble-scale-sync
uses when a scale reports no body fat). Readings for the person set up in
`~/ble-scale-sync/config.yaml` with the **same slug** as their dashboard profile keep
ble-scale-sync's values, including the scale's own analysis when `rmsc04_profile_byte` is set.

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
ignored, so webhook retries and re-imports never create duplicates.

## Maintenance

```bash
systemctl status scale-hub ble-scale        # are both running?
journalctl -u ble-scale -f                  # live Bluetooth log
sudo npm install -g ble-scale-sync@latest && sudo systemctl restart ble-scale   # update
cp ~/scale-hub/data/scale.db ~/scale-backup-$(date +%F).db                       # backup
python3 ~/scale-hub/server.py --import-jsonl ~/ble-scale-sync/measurements.jsonl # rebuild DB from backup
```

Troubleshooting for the Bluetooth side: <https://blescalesync.dev/troubleshooting>.
If the Nova drops the connection early, the weight is still saved but body composition is
missing for that weigh-in. Moving the Pi closer helps.

## Remote access (outside your home)

The dashboard has no login, so don't port-forward it. Use Tailscale or your router's VPN
(for example UniFi Teleport or WireGuard) to reach `http://<pi>:8080` from anywhere.
