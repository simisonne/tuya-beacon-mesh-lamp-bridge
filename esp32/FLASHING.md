# ESP32-S3 sender - build & flash

The `tuya_beacon_tx.ino` sketch turns an **ESP32-S3** into a dedicated
transmitter for the lamp's control frames. The ESP32 forges each frame
(counter from NVS, MIC4 constants, CRC8 tail) and broadcasts a single BLE
advertisement per command. The bridge talks to it over USB serial.

## Why an ESP32

The protocol is a one way broadcast. Any device that can emit a raw 31 byte
BLE advertisement can drive the lamp. A dedicated ESP32 is the most stable
option: no host Bluetooth stack, no controller quirks, sub 100 ms reaction
time, and the counter survives power loss in NVS.

## Requirements

- An ESP32-S3 board (any variant; the native USB enumerates as
  `303a:1001 Espressif USB JTAG/serial debug unit`)
- `arduino-cli` (or the Arduino IDE) with the `esp32` board package
  (Espressif, version 3.x)
- `esptool` (pip: `pip install esptool`)

## Build

```bash
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli compile \
  --fqbn "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc" \
  --output-dir build \
  tuya_beacon_tx
```

The FQBN matters: `CDCOnBoot=cdc` routes `Serial` over the USB port
(`/dev/ttyACM0` on Linux). Without it the sketch talks to UART0 pins 43/44
and the USB port stays silent. `USBMode=hwcdc` keeps the native
USB-Serial-JTAG interface.

## Flash

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
  write_flash 0x0 build/tuya_beacon_tx.ino.merged.bin
```

- The merged image covers the whole flash. **It also wipes NVS**, so the
  counter resets to the default after every flash. Re-sync it before the
  first command:
  `echo "resync 0x0600" > /dev/ttyACM0` (or via the bridge's serial port),
  using a counter higher than what the lamp last saw.
- Stop the MQTT bridge before flashing if it holds the serial port open.

## Serial protocol (115200 baud, line based)

```
on off bri25 bri52 bri79 temp0 temp42 temp99      main light
bl_on bl_off bl5 bl28 bl52 bl80 bl95              backlight
color_red color_yellow color_green color_cyan     backlight colors
color_blue color_pink color_white
status                                          print the counter
resync <hex>                                    set the counter
help
```

Each command answers with `OK <name> Z=0x....`.

## Pitfalls (field verified 2026-08-14)

1. **`BLEDevice::init()` must run ONCE in `setup()`.** Calling it per command
   triggers the RF calibration sweep, a current spike that trips the brownout
   detector on a USB supply (`E BOD: Brownout detector was triggered`, always
   the same crash PC). The sketch keeps the advertising object alive and only
   swaps the data per command.
2. **Counter sync**: the lamp deduplicates on the counter. If the official app
   was used in between, re-sync the ESP32 to the app counter + 1. Read the app
   counter from the air with a BLE scan (the app frames carry the counter as
   the 4th 16-bit UUID of the `1b03` AD element) or power cycle the lamp once
   (clears its dedup memory).
3. **Header**: all commands use `0b61bc000702`. The `000701` variant belongs
   to the WiFi gateway path and gets stale-rejected.
4. **USB enumeration** on some hosts (Raspberry Pi 4 hub) can fail with
   `error -71` on first plug. Replug or try another port. A "VeriFone USB
   UART" line in dmesg is a different device, not the ESP32.
