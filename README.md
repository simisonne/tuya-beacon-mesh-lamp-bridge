# Tuya Beacon-Mesh Ceiling Lamp — Local Control via Raspberry Pi + Home Assistant

Control a Tuya **Beacon Mesh** BLE ceiling lamp (Telink TLSR8266 generation, e.g.
models like `QC0033_P41_CHGM3_CEILING5_DREAM`) **completely locally** — no Tuya
cloud, no app, no gateway. The lamp's command channel is BLE advertising packets;
this project forges valid frames and broadcasts them from a Raspberry Pi's
Bluetooth radio.

This is the first working, field-verified implementation of local control for
this lamp/protocol family (as of 2026-08): the protocol was cracked against live
captures and every frame in the dictionary was verified on-air and visually.

## Features

- **Main light**: on/off, brightness (25/52/79 %), color temperature (0/42/99 %)
- **RGB backlight** (second light entity): on/off, 7 colors, brightness (5/28/52/80/95 %)
- **Home Assistant integration**: native light entities with MQTT auto-discovery
  (`light.<id>` + `light.<id>_backlight`), JSON schema, slider debounce-merging
- **CLI tool** for quick tests without HA
- **Hardened radio path** for the Raspberry Pi's BCM43455:
  - `prepare_radio()` (hciconfig reset+up before every command) against the
    known "silent wedge" (controller reports success but transmits nothing)
  - stale-instance watchdog (5-min time-based hciconfig reset) against stale
    advertisement frames flooding the mesh
  - `clr-adv` before/after every send, self-healing `btmgmt()` with timeout

## How it works

The lamp belongs to the Tuya **Beacon Mesh** family: commands travel as BLE
advertisements (`ADV_IND`, Limited Discoverable), not via a GATT connection.
The app never opens a link — each action is a broadcast frame. A frame is:

```
020101 1b03 | 0b61bc 0007 01 | cnt(2B LE) | kernel(13B) | MIC4(4B) | t5(1B)
flags/len   mesh header   stream+cnt   encrypted DP   const MIC   CRC8^0xB5
```

Two hard-won findings make local control possible without the cloud:

1. **The MIC tail is not cryptographic** — its first 4 bytes are constant per
   command. Copy them from any captured frame.
2. **The 5th tail byte is a plain CRC8** (poly 0x07, init 0, no-refin, XOR 0xB5)
   over the rest of the frame. You can compute it yourself.

The only moving part is the 2-byte counter, which must exceed the last counter
the lamp has seen (the lamp deduplicates on it). It is persisted in
`lampe_counter.json`.

Full protocol details, frame layout and the complete DP dictionary:
**[docs/PROTOCOL.md](docs/PROTOCOL.md)**

## Hardware requirements

- **Raspberry Pi 4** (any Pi with the Broadcom **BCM43455** combo chip — the
  radio hardening in the scripts is written for it; Pi 3B+, Zero 2W and Pi 5
  use the same chip family). Other Bluetooth chips may work but were not tested.
- A **MQTT broker** (the Home Assistant Mosquitto addon is perfect)
- Optional: **Home Assistant** (for the light entities)
- A way to **power-cycle the lamp** (breaker/switch). The lamp's firmware can
  freeze its main-light command handler after a stale-frame flood; a ~10 s power
  cycle is the only recovery (see Troubleshooting).

## Installation (Raspberry Pi)

```bash
# 1. Dependencies
sudo apt update && sudo apt install -y python3-pip bluez
pip3 install paho-mqtt        # or: pipx install paho-mqtt / use a venv

# 2. Install the scripts
sudo mkdir -p /opt/tuya-lamp-bridge
sudo cp tuya_beacon_ctl.py tuya_lampe_mqtt_bridge.py /opt/tuya-lamp-bridge/
sudo cp tuya-lampe-bridge.service /etc/systemd/system/

# 3. Configure (see config.env.example)
sudo cp config.env.example /opt/tuya-lamp-bridge/config.env
sudo nano /opt/tuya-lamp-bridge/config.env

# 4. Counter state: MUST exceed what the lamp last saw (dedup!)
#    Copy your existing lampe_counter.json if you have one, else seed high:
sudo cp lampe_counter.json.example /opt/tuya-lamp-bridge/lampe_counter.json

# 5. Start the bridge
sudo systemctl daemon-reload
sudo systemctl enable --now tuya-lampe-bridge
sudo systemctl status tuya-lampe-bridge
```

**Note on paths**: the unit file uses `/opt/tuya-lamp-bridge` — adjust
`WorkingDirectory`/`ExecStart` if you install elsewhere.

**Note on privileges**: the service runs as `root` (the unit sets `User=root`),
so `btmgmt`/`hciconfig` work without sudoers entries. If you prefer a non-root
service user, add NOPASSWD sudoers entries for `btmgmt`, `hciconfig`, `pkill`
and `systemctl restart bluetooth` instead.

## Usage

### CLI (quick tests)

```bash
cd /opt/tuya-lamp-bridge
./tuya_beacon_ctl.py an              # main light on
./tuya_beacon_ctl.py aus             # main light off
./tuya_beacon_ctl.py hell52          # brightness 52 %
./tuya_beacon_ctl.py temp99          # color temp 99 % (cold)
./tuya_beacon_ctl.py bl_anders       # backlight on
./tuya_beacon_ctl.py farbe_gruen     # backlight green
./tuya_beacon_ctl.py bl80            # backlight 80 %
```

(As a non-root user this needs the NOPASSWD sudoers entries above.)

### Home Assistant

The bridge publishes MQTT auto-discovery on connect, so two light entities just
appear (after the broker has been reloaded once):

- `light.tuya_lampe_deckenlampe` — main light (brightness + color temperature)
- `light.tuya_lampe_deckenlampe_backlight` — RGB backlight

That's it — they behave like normal light entities: dashboard cards, scenes,
automations all work. There are no state guards in the bridge: it always sends
(fresh counter = harmless re-apply), so external changes (app, CLI, power cycle)
cannot desync it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `add-adv` says "Instance added" but the lamp does nothing | BCM43455 "silent wedge" — the controller reports success but transmits nothing. `prepare_radio()` (hciconfig reset+up) before every command prevents it; verify on-air with a BLE sniffer when in doubt. |
| Lamp worked, then ignores everything (main light), backlight still works | **Lamp-side partial freeze** (firmware bug): the stream-01 handler froze, typically after a stale-frame flood. The app will also fail ("can't connect"). Fix: power-cycle the lamp ~10 s. The lamp boots ON — verify recovery with an OFF command first, then ON. |
| A stale frame floods the mesh for minutes | BCM43455 stale advertising instance: `clr-adv` reports "removed" but the controller keeps transmitting. The bridge clears all instances before/after every send and runs a 5-min hciconfig watchdog; `send()` hard-resets the radio if `clr-adv` returns 0x0d. |
| Commands rejected / silently ignored | Counter dedup: the lamp ignores any counter ≤ last seen. Raise `lampe_counter.json` (`{"cnt": <higher>}`) or restore a backup. |
| `add-adv` fails with `0x0d Invalid Parameters` | The frame is not exactly 31 B. `0x0d` right after a previous frame or with no instances to clear is NORMAL — don't chase it. |
| First send after reboot fails (`Set scan/adv parameters failed`) | Controller not fully up: `hciconfig hci0 reset && hciconfig hci0 up`, then retry (the scripts do this automatically). |

## Extending: other lamps / more levels

The DP dictionary in `tuya_beacon_ctl.py` holds the *encrypted* DP kernels of
the captured lamp. Kernels are **device-specific** (encrypted with the device's
beacon key) and **not interpolable** between sampled values — you cannot derive
an unsampled brightness from two neighbors.

To control a different lamp (or add 10 % steps to yours), capture its frames
with an nRF52840 BLE sniffer while someone drives the Tuya app:

```bash
SNIFFER_EXTCAP=/path/to/nrf_sniffer/extcap python3 tools/plaintext_capture.py 420
```

then map each captured kernel to the pressed action and extend the `DP` dict.
See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the frame layout.

## Protocol identification & credits

- Protocol: **Tuya Beacon Mesh** (adv-only control, Telink TLSR8266 / PHY62xx
  family). Identified via the Tuya beacon SDK (`tuya-iotos-beacon-sdk-ak80x`),
  the elektroda reverse-engineering thread (topic 4092517, same `DC:23:50:xx`
  MAC OUI family) and live captures (0 GATT connections, deterministic DP
  kernels, rotating advertising MACs).
- Related projects: [11z4t/tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh)
  (HA codec/decoder for the same mesh family — data decoding, no control).
- The lamp model controlled here: `QC0033_P41_CHGM3_CEILING5_DREAM` ("Smart
  Life" app, no cloud account required for local control).

## Security & responsibility

- No credentials are needed: the frames carry no key exchange, and the MIC is
  a constant per command. This is why local control works at all — but it also
  means anyone within BLE range who knows the frames could toggle your lamp.
  BLE range is the only boundary; treat the frames accordingly.
- Use this only on devices you own.

## License

MIT — see [LICENSE](LICENSE).
