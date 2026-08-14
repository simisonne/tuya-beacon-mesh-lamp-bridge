# Tuya Beacon Mesh Ceiling Lamp, Local Control via Home Assistant

Control a Tuya **Beacon Mesh** BLE ceiling lamp (Telink TLSR8266 generation,
e.g. models like `QC0033_P41_CHGM3_CEILING5_DREAM`) **completely locally**:
no Tuya cloud, no app, no gateway. The lamp's command channel is BLE
advertising packets, and this project forges valid frames and broadcasts them
from your own hardware.

This is the first working, field verified implementation of local control for
this lamp/protocol family (as of 2026-08). The protocol was cracked against
live captures and every frame in the dictionary was verified on air and
visually.

## Features

- **Main light**: on/off, brightness (25/52/79 %), color temperature (0/42/99 %)
- **RGB backlight** (second light entity): on/off, 7 colors, brightness (5/28/52/80/95 %)
- **Home Assistant integration**: native light entities with MQTT auto
  discovery (`light.<id>` + `light.<id>_backlight`), JSON schema, slider
  debounce merging
- **Two sender options**, pick what you have:
  - **ESP32-S3** (recommended): a dedicated transmitter, no host Bluetooth
    stack involved, counter survives power loss in NVS
  - **Raspberry Pi** (BCM43455): uses the Pi's built in Bluetooth radio via
    `btmgmt`, with hardening against the chip's known "silent wedge"
- **CLI tool** for quick tests without HA

## How it works

The lamp belongs to the Tuya **Beacon Mesh** family: commands travel as BLE
advertisements (Limited Discoverable), not via a GATT connection. The app
never opens a link. Each action is a broadcast frame:

```
020101 1b03 | 0b61bc 0007 02 | cnt(2B LE) | kernel(13B) | MIC4(4B) | t5(1B)
flags/len   mesh header      counter      encrypted DP   const MIC   CRC8^0xB5
```

Two findings make local control possible without the cloud:

1. **The MIC tail is not cryptographic**. Its first 4 bytes are constant per
   command. Copy them from any captured frame.
2. **The 5th tail byte is a plain CRC8** (poly 0x07, init 0, no-refin, XOR
   0xB5) over the rest of the frame. You can compute it yourself.

The only moving part is the 2 byte counter, which must exceed the last counter
the lamp has seen (the lamp deduplicates on it). The counter lives in the
ESP32 NVS (serial mode) or in `lamp_counter.json` (btmgmt mode).

**Header note (verified against the official app 2026-08-14):** all commands,
main light AND backlight, use the mesh header `0b61bc000702`. The `000701`
variant belongs to the WiFi gateway path with its own counter space; frames
sent there get stale-rejected once the gateway has been active.

Full protocol details, frame layout and the complete DP dictionary:
**[docs/PROTOCOL.md](docs/PROTOCOL.md)**

## Architecture

```
Home Assistant -> MQTT broker -> tuya_lamp_mqtt_bridge.py -> sender -> lamp
                                     |                 |
                                     |                 +-- ESP32-S3 (serial mode, default)
                                     +-- Raspberry Pi BCM43455 (btmgmt mode)
```

The bridge translates HA light commands (JSON schema) into the discrete DP
commands, maps brightness/color to the nearest captured value, and hands each
command to the sender transport. It publishes MQTT auto discovery on connect,
so the light entities just appear.

## Choose your sender

| | ESP32-S3 (serial mode) | Raspberry Pi (btmgmt mode) |
|---|---|---|
| Hardware | any ESP32-S3 board | Pi 3B+, 4, Zero 2W, 5 (BCM43455 family) |
| Stability | dedicated radio, very reliable | needs radio hardening (wedge, stale instances) |
| Counter | NVS, survives power loss | `lamp_counter.json` |
| Command latency | ~50 ms | ~1-3 s (radio prep included) |
| Extra software | arduino-cli + esptool once | none (bluez only) |
| Setup | flash once, then serial | install scripts + sudoers |

Both feed the same bridge. `SEND_MODE=serial` is the default.

## Option A: ESP32-S3 (recommended)

1. Flash `esp32/tuya_beacon_tx.ino` (build and flash steps, FQBN and pitfalls:
   **[esp32/FLASHING.md](esp32/FLASHING.md)**)
2. Plug the ESP32 into any USB port (a Raspberry Pi, a mini PC, anything that
   can run the bridge and sits near the lamp)
3. Configure the bridge with `SEND_MODE=serial` and the serial device path
   (`/dev/ttyACM0` typically)

The sketch forges each frame, keeps the counter in NVS, and broadcasts a
single advertisement per command. The bridge sends command names over serial
(the ESP32 replies `OK <name> Z=0x....` per command).

## Option B: Raspberry Pi (BCM43455)

The Pi's built in Bluetooth radio broadcasts the frames directly via
`btmgmt`. `tuya_beacon_ctl.py` is the standalone CLI tool (forge + transmit +
counter file), and the bridge uses it in `SEND_MODE=btmgmt`.

```bash
# Dependencies (Debian/Raspberry Pi OS)
sudo apt update && sudo apt install -y python3-pip bluez
pip3 install paho-mqtt        # or: pipx install paho-mqtt / use a venv

# Install
sudo mkdir -p /opt/tuya-lamp-bridge
sudo cp tuya_beacon_ctl.py tuya_lamp_mqtt_bridge.py /opt/tuya-lamp-bridge/
sudo cp tuya-lamp-bridge.service /etc/systemd/system/
sudo cp config.env.example /opt/tuya-lamp-bridge/config.env
sudo nano /opt/tuya-lamp-bridge/config.env     # set SEND_MODE=btmgmt + MQTT
sudo cp lamp_counter.json.example /opt/tuya-lamp-bridge/lamp_counter.json

# Start
sudo systemctl daemon-reload
sudo systemctl enable --now tuya-lamp-bridge
```

**Privileges**: the unit runs as `root`, so `btmgmt`/`hciconfig` work without
sudoers entries. For a non root service user, add NOPASSWD sudoers entries for
`btmgmt`, `hciconfig`, `pkill` and `systemctl restart bluetooth`.

**Radio hardening** (why the Pi path is more involved): the BCM43455 can
report "Instance added" while transmitting nothing (silent wedge) and can keep
transmitting instances that `clr-adv` reported gone (stale frames flood the
mesh). The bridge counters both: `prepare_radio()` (hciconfig reset+up before
every command, skipped when a cleanup is younger than 60 s) and a 5 minute
time based watchdog. `send()` in the ctl cleans up with a bluetooth restart
after every frame.

## Configuration

`config.env` (see `config.env.example`):

```ini
SEND_MODE=serial            # serial (ESP32) or btmgmt (Raspberry Pi)
SERIAL_PORT=/dev/ttyACM0    # serial mode only
SERIAL_BAUD=115200
MQTT_HOST=...               # your broker
MQTT_USER=...
MQTT_PASS=...
MQTT_TOPIC_PREFIX=tuya/lamp
DEVICE_NAME=Tuya Ceiling Lamp
DEVICE_IDENTIFIER=tuya_beacon_lamp
```

## Usage

### CLI (btmgmt mode, quick tests)

```bash
cd /opt/tuya-lamp-bridge
./tuya_beacon_ctl.py on              # main light on
./tuya_beacon_ctl.py off             # main light off
./tuya_beacon_ctl.py bri52           # brightness 52 %
./tuya_beacon_ctl.py temp99          # color temp 99 % (cold)
./tuya_beacon_ctl.py bl_on           # backlight on
./tuya_beacon_ctl.py color_green     # backlight green
./tuya_beacon_ctl.py bl80            # backlight 80 %
```

(As a non root user this needs the NOPASSWD sudoers entries above.)

### Home Assistant

The bridge publishes MQTT auto discovery on connect, so two light entities
just appear (after the broker has been reloaded once):

- `light.tuya_beacon_lamp`, main light (brightness + color temperature)
- `light.tuya_beacon_lamp_backlight`, RGB backlight

That's it. They behave like normal light entities: dashboard cards, scenes,
automations all work. There are no state guards in the bridge: it always sends
(a fresh counter is a harmless reapply), so external changes (app, CLI, power
cycle) cannot desync it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Lamp worked, then ignores everything, app says frozen | **Lamp side freeze** (firmware bug), the command handler froze, typically after a stale frame flood. The app fails too. Fix: power cycle the lamp ~10 s. It boots ON, so verify recovery with an OFF command first. |
| (Pi) `add-adv` says "Instance added" but the lamp does nothing | BCM43455 "silent wedge": the controller reports success but transmits nothing. `prepare_radio()` before every command prevents it. Verify on air with a BLE sniffer when in doubt. |
| (Pi) a stale frame floods the mesh for minutes | BCM43455 stale advertising instance. The bridge clears all instances before/after every send and runs the 5 min watchdog. |
| Commands rejected / silently ignored | Counter dedup: the lamp ignores any counter ≤ last seen. Raise the counter (ESP32: `resync 0x....`; Pi: edit `lamp_counter.json`) above the app's current value. Read the app's counter from the air with a BLE scan (it is the 4th 16-bit UUID of the `1b03` AD element) or power cycle the lamp once. |
| (ESP32) brownout reset when sending (`E BOD`) | The BLE stack was re-initialized per command (RF calibration spike). The sketch inits once in `setup()`. If you modified it, don't call `BLEDevice::init()` per command. |
| (ESP32) no reply on the serial port after flashing | FQBN missing `CDCOnBoot=cdc`, so `Serial` goes to UART0 pins instead of USB. Rebuild with the FQBN from FLASHING.md. |
| First send after reboot fails (`Set scan/adv parameters failed`) | Controller not fully up: `hciconfig hci0 reset && hciconfig hci0 up`, then retry (the scripts do this automatically). |

## Extending: other lamps / more levels

The DP dictionary holds the *encrypted* DP kernels of the captured lamp.
Kernels are **device specific** (encrypted with the device's beacon key) and
**not interpolable** between sampled values. You cannot derive an unsampled
brightness from two neighbors.

To control a different lamp (or add 10 % steps to yours), capture its frames
with an nRF52840 BLE sniffer while someone drives the Tuya app:

```bash
SNIFFER_EXTCAP=/path/to/nrf_sniffer/extcap python3 tools/plaintext_capture.py 420
```

then map each captured kernel to the pressed action and extend the `DP` dict
and the bridge's `BRIGHTNESS`/`COLORTEMP`/`BL_BRIGHTNESS`/`BL_COLORS` tables.
See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the frame layout. The app
controls the lamp with WiFi off + BT on, so no cloud account is needed to
capture.

## Protocol identification & credits

- Protocol: **Tuya Beacon Mesh** (adv only control, Telink TLSR8266 / PHY62xx
  family). Identified via the Tuya beacon SDK (`tuya-iotos-beacon-sdk-ak80x`),
  the elektroda reverse engineering thread (topic 4092517, same `DC:23:50:xx`
  MAC OUI family) and live captures (0 GATT connections, deterministic DP
  kernels, rotating advertising MACs).
- Related projects: [11z4t/tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh)
  (HA codec/decoder for the same mesh family, data decoding, no control).

## Security & responsibility

- No credentials are needed: the frames carry no key exchange, and the MIC is
  a constant per command. This is why local control works at all, but it also
  means anyone within BLE range who knows the frames could toggle your lamp.
  BLE range is the only boundary. Treat the frames accordingly.
- Use this only on devices you own.

## License

MIT. See [LICENSE](LICENSE).
