#!/usr/bin/env python3
"""Known-plaintext capture for Tuya BLE-mesh lamps (Telink TLSR8266, fe65 gen).

VERIFIED 2026-08-13 on a QC0033 ceiling lamp: the Tuya Smart Life app controls
the lamp via BLE ADVERTISING packets (no GATT connection). Each app action
produces 2 ADV frames = (temp-value, brightness-value). DP values are
DETERMINISTIC: same value => same 14-byte kernel, independent of the frame
counter. This tool records the frames so you can build/extend the DP dictionary
for YOUR lamp.

Payload layout (Nordic SnifferAPI payload bytes):
  AdvA(6) + 020101 (Limited Discoverable) + 1b03 + 0b61bc + 000701
  + <2B counter LE> + <14B kernel starting 0x05> + <8B MIC tail>

CRITICAL: the payload STARTS with the 6-byte AdvA, so the flags sit at hex
offset 12, NOT 0. A naive startswith('020101') filter silently captures
NOTHING (bug hit 2026-08-13).

Prerequisites: an nRF52840 board running the Nordic BLE Sniffer firmware
(USB CDC-ACM). The Nordic SnifferAPI Python package must be importable:
set the SNIFFER_EXTCAP env var to the directory containing SnifferAPI
(extcap dir of the Nordic nRF Sniffer install), e.g.
  SNIFFER_EXTCAP=/opt/nrf_sniffer/extcap python3 plaintext_capture.py 420

Usage: plaintext_capture.py <seconds>

Workflow (someone drives the app): say the action ("ON"), the user presses it
3x with ~3s gaps, check the log, then the next action (brightness 25/52/79%,
color temp 0/42/99%, backlight toggles...). Correlate kernels with actions ->
replay dictionary. Extraction tip: find the 0201011b03 marker, take 68 hex,
strip the 3B CRC tail.
"""
import sys, time, os
sys.path.insert(0, os.environ.get("SNIFFER_EXTCAP", ""))
from SnifferAPI import Sniffer, UART

RUN_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 420.0
MARKER_OK = ('0b61bc', '1361bc')  # lamp family marker in the obfuscated UUIDs

ports = UART.find_sniffer()
if not ports:
    print("NO SNIFFER FOUND")
    sys.exit(1)
print(f"Sniffer on {ports[0]}")

s = Sniffer.Sniffer(portnum=ports[0], baudrate=1000000)
s.start()
s.scan()
print(f"READY ({RUN_SECONDS:.0f}s). Waiting for app actions!", flush=True)

t0 = time.time()
seen = {}
interesting = 0

try:
    while time.time() - t0 < RUN_SECONDS:
        for p in s.getPackets():
            bp = p.blePacket
            if bp is None:
                continue
            a = getattr(bp, 'advAddress', None) or getattr(bp, 'scanAddress', None)
            mac = ":".join(f"{b:02X}" for b in a[:-1]) if a else "?"
            payload = bytes(getattr(bp, 'payload', []) or [])
            ph = payload.hex()
            sec = int(time.time() - t0)
            key = (sec, ph[:60])  # dedup per second + payload head
            if key in seen:
                seen[key] += 1
                continue
            seen[key] = 1
            # flags at hex offset 12-18 (payload begins with 6-byte AdvA!)
            flags_pos = ph[12:18]
            is_limdisc = flags_pos == '020101'
            is_lamp = is_limdisc and any(mk in ph for mk in MARKER_OK)
            if is_lamp:
                interesting += 1
                print(f"[{time.time()-t0:6.1f}s] LAMP {mac} {ph}", flush=True)
            elif is_limdisc:
                print(f"[{time.time()-t0:6.1f}s] LIMDISC {mac} {ph[:80]}", flush=True)
        time.sleep(0.02)
except KeyboardInterrupt:
    print("\nAborted.")
finally:
    print(f"\n=== END === lamp packets: {interesting}, dedup pairs: {len(seen)}")
    try:
        s._stop()
    except Exception:
        pass
