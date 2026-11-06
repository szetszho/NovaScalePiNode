# R-MSC04: the 5.5 s drop is Linux refusing the scale's connection parameter request (fix: `bluetoothd -P gap`)

## Summary

On Linux (BlueZ), my R-MSC04 hung up **5.5 s after every connection**, before the 0x25
composition record, so every weigh-in was weight only. The cause sits below ble-scale-sync, at the
Bluetooth link layer:

1. While discovering services, bluetoothd's **GAP plugin** reads the scale's Peripheral Preferred
   Connection Parameters (0x2A04: interval 10–12.5 ms) and loads them into the kernel
   (`MGMT Load Connection Parameters`). From then on the kernel keeps them for this device, in
   memory and in `/var/lib/bluetooth/<adapter>/<scale>/info`.
2. About 0.5 s after connecting, the scale sends an **LE Remote Connection Parameter Request**
   for **30–45 ms, latency 2, timeout 5000 ms**.
3. The kernel answers with a **Negative Reply**, because the requested max interval (45 ms) is
   above the stored max (12.5 ms). This is the check in `net/bluetooth/hci_event.c`,
   `hci_le_remote_conn_param_req_evt()`:
   ```c
   if (max > hcon->le_conn_max_interval) {
           send_conn_param_neg_reply(hdev, handle, HCI_ERROR_INVALID_LL_PARAMS);
   ```
4. The scale asks again about 2.5 s later, gets a second Negative Reply, and disconnects with
   **Remote User Terminated Connection (0x13)** about 5.5 s after the connection opened.

An iPhone with the Renpho app gets the same request, **accepts** it, and keeps the connection
for the whole weigh-in. Running bluetoothd with `-P gap` (GAP plugin off), clearing the stored
parameters and rebooting fixed it: the request was accepted, the link stayed up, and the 0x25
record came through with body fat, skeletal muscle, visceral fat and all ten segment impedances.

It also explains why the **first weigh-in after a fresh install can work**. Nothing is stored
yet, the kernel default max interval (50 ms) applies, and the 45 ms request is accepted. Every
later connection fails.

## Setup

- ble-scale-sync 1.39.1 (built from source), built-in Linux transport (node-ble / BlueZ D-Bus)
- Raspberry Pi 4 Model B Rev 1.5 (built-in Bluetooth), Debian GNU/Linux 13 (trixie), kernel `6.18.50+rpt-rpi-v8`, BlueZ 5.82
- `ble.rmsc04_profile_byte` set (captured from the Renpho app on iOS)

## Evidence

### Before: the Pi refuses, and the scale hangs up (`btmon`)

```text
LE Connection Complete (0x01)
    Connection interval: 11.25 msec (0x0009)      <- stored PPCP already in use
...
ATT: Read By Type Response ... Peripheral Preferred Connection Parameters (0x2a04)
    Value[8]: 08000a000000c800                    <- 10–12.5 ms, latency 0, timeout 2 s
@ MGMT Command: Load Connection Parameters (0x0035)
    Min connection interval: 8
    Max connection interval: 10
> LE Remote Connection Parameter Request (0x06)   [t=4.05 s]
    Min connection interval: 30.00 msec (0x0018)
    Max connection interval: 45.00 msec (0x0024)
    Connection latency: 2 (0x0002)
    Supervision timeout: 5000 msec (0x01f4)
< LE Remote Connection Parameter Request Negative Reply (0x08|0x0021)
> LE Remote Connection Parameter Request (0x06)   [t=6.56 s, same values]
< LE Remote Connection Parameter Request Negative Reply (0x08|0x0021)
> HCI Event: Disconnect Complete (0x05)           [t=9.11 s]
    Reason: Remote User Terminated Connection (0x13)
```

The ble-scale-sync summary for such a session. It's the same with `BLE_RMSC04_HANDSHAKE=full`,
and with an extra b2 written after 0x24 (I tried that, and it made no difference):

```text
Renpho R-MSC04: session ended 4.7 s after the handshake started (writes full: b2 answered,
b3 answered; first status 0x01 with 0 stored records; last status 0x01 at 0.0 s; 0x24 yes;
stored records replayed 0; composition record no; last frame 3.4 s before the end)
```

Note: the BlueZ disconnect-reason line did not appear in the debug log for these sessions; I
only got the reason from `btmon`.

### iPhone + Renpho app (PacketLogger, same scale)

```text
LE - Enhanced Connection Complete - Central - Conn Interval: 30 ms
LE - Remote Connection Parameter Request                  (0.5 s after connect)
LE Remote Connection Parameter Request Reply              <- accepted
LE - Connection Update Complete - Interval: 30 ms - Latency: 60 ms - Supervision Timeout: 5000 ms
... 0x24 at +3.4 s, status 0x09 at +9.6 s, status 0x11 + 0x25 record at +19.2 s
```

The app also writes b7 (name), b8 (settings) and b6, and repeats b2 after 0x24 and after the
record. None of that turned out to be needed.

### After: `-P gap`, stored parameters cleared, reboot

```text
LE Connection Complete (0x01)
    Connection interval: 45.00 msec (0x0024)
LE Connection Update Complete (0x03)
    Status: Success (0x00)
    Connection interval: 45.00 msec (0x0024)
    Connection latency: 2 (0x0002)
    Supervision timeout: 5000 msec (0x01f4)
...
< HCI Command: Disconnect (0x01|0x0006)            <- our own disconnect after the reading, ~15 s in
    Reason: Connection Terminated By Local Host (0x16)
```

ble-scale-sync debug log of the same weigh-in:

```text
[BLE] Subscribed to 2 notification(s). Step on the scale.
Renpho R-MSC04: status seq 0, state 0x01 (measuring), 0 stored records, [7] 01, [9] 64
Renpho R-MSC04: scale answered b2 (seq 0) with 09
Renpho R-MSC04: scale answered b3 (seq 1) with 0701
Renpho R-MSC04: status seq 1, state 0x09 (weight locked), 0 stored records, [7] 01, [9] 64
Renpho R-MSC04: status seq 2, state 0x11 (measurement complete), 0 stored records, [7] 01, [9] 64
Notification 2a12: [ad 03 02 55 aa 25 00 24 03 11 00 00 1b 71 0a 00 ab 0d 11 0c]
Notification 2a12: [ae 03 01 86 0a 75 0a 1b 00 82 0b 8c 0a fa 09 32 08 db 09 00]
Notification 2a12: [af 03 00 9c 00 e5 01 df 00 03 99]
Renpho R-MSC04: record 70.25 kg, fat 15.6 %, BMI 22.9, skeletal muscle 47.9 %, visceral 3;
  impedance (trunk / L arm / R arm / L leg / R leg)
  20 kHz 17.1 / 334.5 / 320.6 / 267.7 / 258.7 ohm, 100 kHz 13.0 / 295.6 / 281.0 / 235.4 / 226.7 ohm
Renpho R-MSC04: the record's BMI matches the height we sent (BMI within 0.1)
Renpho R-MSC04: session ended 10.9 s after the handshake started (writes full: b2 answered,
  b3 answered; first status 0x01 with 0 stored records; last status 0x11 at 10.6 s; 0x24 no;
  stored records replayed 0; composition record used; last frame 0.0 s before the end)
Renpho R-MSC04: using the scale's body fat, skeletal muscle and visceral fat, computed for the
  profile sent with ble.rmsc04_profile_byte (#434)
```

That session connected after the weight had locked (no 0x24), and the D057 path picked up the
record correctly.

## Workaround (what I did)

1. Run bluetoothd without the GAP plugin:
   ```bash
   sudo mkdir -p /etc/systemd/system/bluetooth.service.d
   printf '[Service]\nExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd -P gap\n' |
     sudo tee /etc/systemd/system/bluetooth.service.d/override.conf
   ```
2. Remove what's already stored for the scale:
   ```bash
   sudo rm -rf /var/lib/bluetooth/*/<SCALE-MAC> /var/lib/bluetooth/*/cache/<SCALE-MAC>
   ```
3. **Reboot.** Restarting bluetoothd is not enough, because the kernel keeps the loaded parameters
   in memory, and ble-scale-sync's RemoveDevice before the scan logs "Device not in BlueZ cache;
   RemoveDevice is a no-op" and doesn't clear them either.
4. Check with `sudo btmon`: you should see `LE Remote Connection Parameter Request Reply`
   (no "Negative") and `LE Connection Update Complete` at 45 ms.

## Suggestions for ble-scale-sync

- Document the workaround on the Renpho page. The troubleshooting entry "The R-MSC04 drops the
  connection a few seconds in" fits exactly.
- Log the disconnect reason for these sessions. Here only `btmon` showed the 0x13.
- Possibly handle it in code: for the R-MSC04, load wider connection parameters for the scale
  (for example max ≥ 45 ms) before connecting, or clear the kernel's stored parameters for it.
  I haven't tested either; `-P gap` is what I've verified.
