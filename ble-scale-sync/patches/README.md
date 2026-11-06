# Local ble-scale-sync patches

`install.sh` applies every `*.patch` here to the ble-scale-sync source in
`~/ble-scale-sync/src` before building it. A patch that no longer applies to a newer
ble-scale-sync is skipped with a warning.

- `rmsc04-resend-profile.patch`: after the Renpho R-MSC04 sends its settled weight, write
  the b2 profile once more with that weight, as the Renpho app does (seen in an iOS
  Bluetooth capture). Meant to stop the scale dropping the link about 5 s in, before it
  sends body composition. Turn it off with `BLE_RMSC04_RESEND_PROFILE=0` in
  `~/ble-scale-sync/.env`. Not yet confirmed on a scale; upstream issue:
  https://github.com/KristianP26/ble-scale-sync/issues/434
