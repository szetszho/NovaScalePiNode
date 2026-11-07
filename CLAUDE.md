# Scale Hub (NovaScalePiNode)

Self-hosted dashboard for a Renpho MorphoScan Nova (R-MSC04) on a Raspberry Pi. Read
`docs/rmsc04-notes.md` before changing anything Bluetooth- or composition-related: it has the
protocol, the verified findings and the reasoning behind the current design.

- `server.py`: Python standard library only (no pip installs on the Pi). SQLite, HTTP API,
  body composition model, and the writer that keeps ble-scale-sync's `config.yaml` in step
  with the chosen scale profile.
- `static/index.html`: the whole web app, no build step, no external scripts or fonts (the Pi may
  be offline). Tabs: Overview, Body, History, Settings (bottom tab bar on phones). Must work at
  390 px width and in dark mode. Design work: use the vendored `impeccable` skill
  (`.claude/skills/impeccable`, `/impeccable critique|polish|audit ...`); this is an Operate-mode
  surface, so restraint and scanability over expression.
- `install.sh`: the install and update path on the Pi; must stay safe to re-run. It applies
  `ble-scale-sync/patches/*.patch` (skips ones that don't apply, rebuilds without them if the
  build fails).
- `ble-scale-sync/patches/rmsc04-household.patch`: household matching (the scale computes for
  whoever stepped on) and the raw `rmsc04` record in the webhook payload. Can't be compiled in
  the cloud container; regenerate it against a fresh ble-scale-sync clone and `git apply --check`.
- `tools/decode_packetlogger.py`: decodes iPhone PacketLogger exports of the scale.
- `docs/ble-scale-sync-upstream.md`: changes to propose to ble-scale-sync;
  `docs/ble-scale-sync-issue-434.md`: the upstream issue write-up.

Checks before committing: `bash -n install.sh`, `python3 -m py_compile server.py`, and a run
of the server against a scratch DB and scratch config
(`SCALE_HUB_RESTART_BSS=0 SCALE_HUB_BSS_CONFIG=/tmp/x.yaml python3 server.py --db /tmp/x.db --port 8765`)
with the UI opened at desktop and phone width. Keep `docs/rmsc04-notes.md` in step with any new
finding or design change, and mark facts verified or inferred.

Don't commit the owner's profile byte, name or scale MAC into docs meant for upstream.
