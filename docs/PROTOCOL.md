# Tuya Beacon Mesh, Protocol Notes

Field verified against a `QC0033_P41_CHGM3_CEILING5_DREAM` ceiling lamp
(Telink TLSR8266), 2026-08. Verification method: nRF52840 BLE sniffer captures
correlated with known app presses. 29/29 captured frames matched the frame
layout below, and forged frames were visually confirmed on the lamp (on/off,
brightness, color temp, backlight, colors).

## Protocol identification

- **Tuya Beacon Mesh**: commands travel as BLE advertising packets (`ADV_IND`,
  Limited Discoverable `020101`). The app never opens a GATT connection
  (0 CONNECT_REQ in clean captures. WiFi off + BT on still controls the lamp).
- Device family: Telink TLSR8266 (fe65 GATT generation / beacon remotes),
  MAC OUI family `DC:23:50:xx` (same family as the elektroda thread's lamps).
- Advertising MAC **rotates** on every burst. Never match by address, match by
  payload (marker `0b61bc` / `1361bc`).
- Every frame is transmitted by 2-3 mesh nodes (lamp + relays) with identical
  payload but different packet CRC, so the MIC is not bound to the source
  address. **Any transmitter works**.

## Frame anatomy (31 B AdvData)

```
offset  bytes  meaning
0       3      AD: 02 01 01            (flags, Limited Discoverable)
3       2      AD: 1b 03               (length 0x1b incl. type byte, type 0x03)
5       6      0b 61 bc 00 07 02       (mesh header; 000702 = direct control
                                        path, used by the app for ALL commands)
11      2      counter (LE)
13      13     kernel (encrypted DP payload, starts with 0x05)
26      4      MIC4 (constant per command)
30      1      t5 = CRC8(mesh|cnt|kern|MIC4) ^ 0xB5
```

Notes:

- The `1b03` length byte counts **including** the type byte: `0x1b` + type
  `0x03` + 26 data bytes = 27, matching the 31 B total with the 3 byte flags AD.
- Sniffer dumps append a 3 byte packet CRC. **Strip it** before treating the
  hex as AdvData, and never include it in the advertised payload (the kernel
  TLV check then fails with `0x0d`).
- A 30 B frame gives `add-adv 0x0d Invalid Parameters`. That is a build bug,
  not a controller problem.

## Counter & dedup semantics

- The lamp deduplicates on the counter: it executes only frames with a counter
  **greater** than the last one it processed. This is **global across sources**
  (a Pi frame was rejected after an app frame with a higher counter), so:
  - persist the counter (here: `lamp_counter.json`) and increment per command.
  - when moving to a new sender, copy the state file or seed high, starting at
    `0` gets everything silently rejected.
  - a power cycle resets the lamp's dedup memory (any counter works afterwards).
- The app resends its *old* counters when it is (re)opened. Those are not
  fresh presses.
- Each app action produces **2 frames**: (temp value, brightness value), the
  unchanged value first, then the changed one. ON/OFF is a single frame.
- The unchanged value stays byte identical across all counters (deterministic
  kernel, the frame counter and MIC tail are the only varying parts).

## CRC8 tail (t5)

```
crc = 0
for b in [mesh | cnt | kern | MIC4]:
    crc ^= b
    for _ in range(8):
        crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
t5 = crc ^ 0xB5
```

poly 0x07, init 0, no-refin, final XOR 0xB5. Verified 29/29.

## MIC4

The first 4 bytes of the tail are **constant per command**. Grab them from any
captured frame. No cryptography is involved in the tail. The only secret like
component is the kernel itself (see below).

## DP dictionary (captured 2026-08-13)

`name: (kernel 13B, MIC4)`. **All commands use header `0b61bc000702`**
(verified against the official app 2026-08-14: main light AND backlight frames
ride 000702; the `...01` header is the WiFi gateway path with its own counter
space and must NOT be used for control):

| command | kernel | MIC4 | stream |
|---|---|---|---|
| on (relay on) | `dfcf36aa6b2c34ed15db67bd3d` | `60d8d6fe` | 02 |
| off (relay off) | `3a36cebe8edf95850ae6308b55` | `883e6ed4` | 02 |
| bri25 (brightness 25%) | `be56d4cfffdfb0cf314e65b036` | `4ccfa9a3` | 02 |
| bri52 (52%) | `5c41d16eaabc41c23e6cba39c6` | `ecb5ec3f` | 02 |
| bri79 (79%) | `4c09c4f84bb8693ddfaf6ae0ec` | `44a50cc9` | 02 |
| temp0 (color temp 0%, warm) | `109ee02e7dbad12c34f43b2a01` | `960c8317` | 02 |
| temp42 (42%) | `8037842f227b37f792f187846a` | `83b10565` | 02 |
| temp99 (99%, cold) | `66e175b3b76ec42eef842daf88` | `5a36660b` | 02 |
| bl_on (backlight ON) | `e9447e2fc85af4287c3b9a550b` | `2ce9e348` | 02 |
| bl_off (backlight OFF) | `0c6322e99fc4517f9a2360263d` | `8e678e23` | 02 |
| bl5 (backlight 5%) | `55bb35e8f823f11193525d107d` | `a6fff9fb` | 02 |
| bl28 (28%) | `03b0758709ac62a6eee4d5c1ac` | `9d406905` | 02 |
| bl52 (52%) | `3efa92fa33ad32c5943ab27288` | `9c0da56c` | 02 |
| bl80 (80%) | `9a4ca07eaec900f8b9dc8815eb` | `420596d2` | 02 |
| bl95 (95%) | `3a9aead6506397aadafca74298` | `20f3313c` | 02 |
| color_red (red) | `7526a354938ae0c6a949a1f7ab` | `4ce1ea73` | 02 |
| color_yellow (yellow) | `1616b127aecd0ba3f47a803976` | `1199b355` | 02 |
| color_green (green) | `b3c485f9f2b7065f4cab3d33cd` | `568a072f` | 02 |
| color_cyan (cyan) | `5b77017b898c8c275b9a04511a` | `3aaa4e6b` | 02 |
| color_blue (blue) | `fe2cdc8c63b7cc39a9a772991d` | `9720117b` | 02 |
| color_pink (pink) | `8c71de5029a3e7129bf93dd9ad` | `9040012e` | 02 |
| color_white (white) | `fb7dd77a687da3ce3f2322fd66` | `7ceb5fed` | 02 |

**Labels pitfall**: the backlight labels were once inverted in earlier notes.
`bl_on` = ON, `bl_off` = OFF (app verified).

## Kernels are device specific and not interpolable

The kernel is the DP payload encrypted with the device's beacon key (Tuya
beacon SDK `frame_send(..., beaconkey, ...)`). Consequences:

- Kernels from a **different lamp are useless** (different beacon key).
- Unsampled values are **not derivable**: no byte is linear between 25 → 52 →
  79 (equidistant delta check). If you want 10 % steps, capture them.

### Capturing your own DP values

1. Flash an nRF52840 board with the Nordic BLE sniffer firmware (USB CDC-ACM).
2. Run `tools/plaintext_capture.py <seconds>` (set `SNIFFER_EXTCAP` to the
   Nordic extcap dir). Have someone press each action in the Tuya app 2-3×
   (~3 s apart), calling out the action name.
3. Extract: find `0201011b03` in each logged line, take 68 hex chars, strip the
   final 3 B CRC, so you get `...000702 <2B cnt> <26B kern+MIC4>`.
4. Split: `kern` = bytes 0-13, `MIC4` = bytes 13-17 of that 26 B block.
   Counter = the 2 bytes before the kernel (LE).
5. Extend the `DP` dict in `tuya_beacon_ctl.py` and the bridge's
   `BRIGHTNESS`/`COLORTEMP`/`BL_BRIGHTNESS`/`BL_COLORS` tables.

The Tuya Smart Life app controls the lamp with WiFi OFF + BT ON. No cloud
account is needed to capture (or control) it.

## Known device quirks (this lamp generation)

- **Partial freeze**: after a stale frame flood the main light command handler
  can stop responding to *everything*, including the official app, while the
  backlight and status beacons keep working. Only a ~10 s power cycle recovers
  it (it also clears the dedup memory). The lamp boots ON, so verify recovery
  with an OFF command first.
- **All control frames use `0b61bc000702`** (app verified 2026-08-14). The
  `...01` header variant belongs to the WiFi gateway path; commands sent there
  get stale-rejected once the gateway has been active.
- **Idle beacons**: the lamp advertises status beacons (company 0x0006, mesh
  header `01 09 20 22`) continuously even when idle. TX stays alive even when
  command processing is frozen. Don't mistake beacons for a healthy lamp.
