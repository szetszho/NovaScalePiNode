# Local ble-scale-sync patches

`install.sh` applies every `*.patch` here to the ble-scale-sync source in `~/ble-scale-sync/src`
before building it. A patch that no longer applies to a newer ble-scale-sync is skipped with a
warning, and the build carries on without it.

- `rmsc04-household.patch` (Renpho R-MSC04): when the weight settles, sends the profile of the
  household member nearest by weight (from `household.json`, written by Scale Hub when
  **Settings → Renpho figures for everyone** is on), so the scale computes its own analysis for
  whoever stepped on. Also adds the scale's raw record (`rmsc04`: fat, BMI, skeletal muscle,
  visceral fat, slot, who the profile was sent to) to the webhook payload, which Scale Hub
  verifies against the person's height. Without `household.json`, or with it switched off, the
  only change is the extra `rmsc04` field. Proposed upstream: `docs/ble-scale-sync-upstream.md`
  items 5 and 7.
