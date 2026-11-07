# Proposed ble-scale-sync changes for the Renpho R-MSC04

Notes for a later PR to [KristianP26/ble-scale-sync](https://github.com/KristianP26/ble-scale-sync),
based on one owner's captures and tests (see `docs/rmsc04-notes.md` for all the evidence and
`docs/ble-scale-sync-issue-434.md` for the issue write-up). Each item says what to change, where,
and how sure we are. Suggested order: 1 and 2 are documentation, 3–5 are small and verified,
6–8 need testing first.

## 1. Document the connection-parameter workaround (verified)

`docs/guide/scales/renpho.md`, section "The R-MSC04 drops the connection a few seconds in":
the drop is Linux refusing the scale's LE connection parameter request (30–45 ms) after
bluetoothd's GAP plugin loaded the scale's PPCP (10–12.5 ms). Workaround: `bluetoothd -P gap`,
remove the stored device, reboot. Include the `btmon` lines to check. Also worth a line in
`docs/troubleshooting.md`, since other scales that send a parameter request above their own
PPCP would hit the same thing.

## 2. Log why the link ended (verified gap)

During these sessions the debug log never printed the
`BlueZ: <mac> disconnected ... after connect: <reason>` line from
`src/ble/handler-node-ble/disconnect-reasons.ts`; only `btmon` showed the 0x13. Worth checking
why the reason isn't captured on BlueZ 5.82 / kernel 6.18 (the Device1 `Disconnected` signal,
or the MGMT `Device Disconnected` reason).

## 3. Derive `rmsc04_profile_byte` instead of sniffing it (verified on one unit)

Byte = `(gender === 'male' ? 0x80 : 0) | age` (age in whole years on the weigh-in date). Seen by
changing one setting at a time in the Renpho app: switching only the sex flips bit 7 (set for
male), and test profiles female 30 = 0x1E and female 32 = 0x20 show the age in the low 7 bits;
the guest byte 0xA9 fits as male 41.
Athlete mode and activity level are not sent. In `src/scales/renpho-msc04.ts`:

- `writeGuestProfile`: when `ble.rmsc04_profile_byte` is unset, compute the byte from the
  first user's `gender` and `age` instead of `GUEST_PROFILE_BYTE`.
- Keep `rmsc04_profile_byte` as an override.
- The D079 gate (`ownProfileComposition`) then applies to everyone with a complete profile;
  no Android HCI log needed.

Caveat: the comment in the source says "0x80 | age" did not fit two profiles of known age in
an earlier report, so ask for more data on #434 before making it the default.

## 4. Time zone west of UTC (verified on two captures)

`buildClockFrame` sends 0 for zones west of UTC because the encoding was unknown. The Renpho app
at UTC−6 sends `0x8168`: bit 15 set for west, then the minutes (360). Proposed:
`zone = east >= 0 ? east : 0x8000 | -east`, and widen `zoneMinutes()` accordingly; update the
`MAX_ZONE_MINUTES` comment and the docs line "Time zones west of UTC are sent ... as UTC".

## 5. Say where the composition came from (prototype: the `rmsc04` payload field in `ble-scale-sync/patches/rmsc04-household.patch`)

Downstream (Scale Hub here) has to infer "the scale's own figures were used" from the presence
of `skeletalMusclePercent`. Proposed optional fields on `BodyComposition` for this scale:

- `compositionSource: 'scale' | 'estimate'`
- `scaleBmi` (the record's BMI, which gives away the height the scale used)
- `scaleSlot` (record byte 32: the slot the scale computed for)

## 6. The scale's analysis ignores sex and age (verified, affects the docs)

With near-identical impedances, slot 01 returned 11.3 / 11.2 / 11.5 % fat for male 30,
female 32 and female 30, and the Renpho app showed the same. Only height (and weight) moved it.
Worth stating in `docs/guide/scales/renpho.md`, because it means the D079 gate's sex and
birth-date checks protect against less than they seem to, and the height check
(`fitsSentHeight`) is the one that matters.

## 7. Multi-user: send the matched user's profile after 0x24 (verified on one unit; prototype in `ble-scale-sync/patches/rmsc04-household.patch`)

The record arrives about 16 s after 0x24, after status 0x09 and 0x11. By 0x24 ble-scale-sync
knows the weight, so it can match the user and write b2 again with that user's height and byte.
**The scale uses that late b2:** with a 180 cm profile sent at connect, a 163 cm household
member's weigh-in got a record whose BMI gives 163.1 cm after the patch re-sent b2 at 0x24
(the D079 gate then logged "the scale's BMI does not fit the height sent to it", since it
compares against the connect-time profile). So every household member can get the scale's own
figures. What it needs in ble-scale-sync:

- match the user by weight at the first 0x24 (the existing `users[]` matching would do) and
  write their b2 (guest slot 09, their height and byte, the settled weight);
- the D079 gate and `fitsSentHeight` should compare against the **last** b2 of the session;
- `last_known_weight` should be written for the matched user, not the first one.

Limit: no 0x24 when the weight locked before the connect (D057 path); then the connect-time
profile applies. Whether a b2 after status 0x09 still counts is untested.

## 8. Document that the app's per-limb figures are not in the record (verified)

The 0x25 record has the ten impedances, body fat, BMI, skeletal muscle % and visceral fat only.
The Renpho app's segment muscle and fat, protein, bone, water, BMR, SMI and standards are
computed in the app; two app reports show it splits segment muscle by fixed shares and barely
uses segment impedance (details: `docs/rmsc04-notes.md` section 6b). Worth a line in
`docs/guide/scales/renpho.md` so users don't expect those from the scale.

## 9. Stored weigh-ins (0x26) and battery (untested)

- 0x26 records are weigh-ins taken while nobody was connected (46 bytes, seconds-old at
  [7..10]). They are logged and ignored today; they could be exported as history. The app
  answers each with `b6 <seq> 01`, which most likely deletes it from the scale, so don't send b6
  unless export succeeded.
- Status byte [9] was 0x64 (100) in every session: probably the battery percentage; a
  diagnostic field would confirm it over time.
