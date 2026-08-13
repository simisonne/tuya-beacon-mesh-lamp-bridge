#!/usr/bin/env python3
"""Tuya Beacon Mesh ceiling lamp, local control from a Raspberry Pi (no app/cloud/gateway).

Protocol (cracked 2026-08-13, verified against 29/29 captured frames):
  AdvData = 020101 1b03 | 0b61bc000701 | cnt(2B LE) | kern(13B) | MIC4(4B) | t5(1B)
  t5 = CRC8(poly=0x07, init=0, no-refin) over [0b61bc000701 | cnt | kern | MIC4] XOR 0xB5
  cnt must be greater than the last counter the lamp has seen (lamp side dedup).
  It is persisted in a state file next to this script (all callers share it).

The kernel is the encrypted DP payload (Tuya beacon SDK frame_send with the
per device beacon key). Values are device specific: if your lamp has different
brightness/color levels, capture your own frames (see tools/plaintext_capture.py
and docs/PROTOCOL.md) and extend the DP dictionary below.

Requires: bluez btmgmt + hciconfig with NOPASSWD sudoers entries for the
invoking user (see README.md).
"""
import json
import os
import subprocess
import sys
import time

# --- Constants -----------------------------------------------------------
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lamp_counter.json")
# Timestamp of the last guaranteed cleanup (send() sets it after the bluetooth
# restart). The bridge uses it to skip the 5s radio reset before a command
# when the radio is already fresh (latency optimization, 2026-08-13 ~21:30).
LAST_CLEANUP = 0.0

# DP dictionary: 22 commands extracted from a known plaintext capture (2026-08-13)
# IMPORTANT: kernels are 13B (26 hex). bri* kernels END on 'ec'. An intermediate
# version with 12B kernels (4c09c4f84bb8693ddfaf6ae0) produced broken 30B frames -> kernel 0x0d!
DP = {
    "on":       ("dfcf36aa6b2c34ed15db67bd3d", "60d8d6fe"),   # relay on
    "off":      ("3a36cebe8edf95850ae6308b55", "883e6ed4"),   # relay off
    "bri25":    ("be56d4cfffdfb0cf314e65b036", "4ccfa9a3"),   # brightness 25%
    "bri52":    ("5c41d16eaabc41c23e6cba39c6", "ecb5ec3f"),   # brightness 52%
    "bri79":    ("4c09c4f84bb8693ddfaf6ae0ec", "44a50cc9"),   # brightness 79%
    "temp0":    ("109ee02e7dbad12c34f43b2a01", "960c8317"),   # color_temp 0% (warm)
    "temp42":   ("8037842f227b37f792f187846a", "83b10565"),   # color_temp 42%
    "temp99":   ("66e175b3b76ec42eef842daf88", "5a36660b"),   # color_temp 99% (cold)
    # Backlight (freshly captured 2026-08-13, order app verified):
    # bl_on = ON, bl_off = OFF (the old labels were inverted!)
    "bl_on":    ("e9447e2fc85af4287c3b9a550b", "2ce9e348"),   # backlight ON
    "bl_off":   ("0c6322e99fc4517f9a2360263d", "8e678e23"),   # backlight OFF
    "bl5":      ("55bb35e8f823f11193525d107d", "a6fff9fb"),   # brightness 5%
    "bl28":     ("03b0758709ac62a6eee4d5c1ac", "9d406905"),   # brightness 28%
    "bl52":     ("3efa92fa33ad32c5943ab27288", "9c0da56c"),   # brightness 52%
    "bl80":     ("9a4ca07eaec900f8b9dc8815eb", "420596d2"),   # brightness 80%
    "bl95":     ("3a9aead6506397aadafca74298", "20f3313c"),   # brightness 95%
    "color_red":     ("7526a354938ae0c6a949a1f7ab", "4ce1ea73"),
    "color_yellow":  ("1616b127aecd0ba3f47a803976", "1199b355"),
    "color_green":   ("b3c485f9f2b7065f4cab3d33cd", "568a072f"),
    "color_cyan":    ("5b77017b898c8c275b9a04511a", "3aaa4e6b"),
    "color_blue":    ("fe2cdc8c63b7cc39a9a772991d", "9720117b"),
    "color_pink":    ("8c71de5029a3e7129bf93dd9ad", "9040012e"),
    "color_white":   ("fb7dd77a687da3ce3f2322fd66", "7ceb5fed"),
}

# --- Protocol ------------------------------------------------------------
MESH = "0b61bc000701"    # Stream 01 = main light DPs (on/off/bri/temp)
MESH_BL = "0b61bc000702" # Stream 02 = backlight DPs (bl_*/color_*)!
# 2026-08-13 capture verified: the app's backlight frames use the header
# 0b61bc000702. With the stream-01 header the lamp silently ignored them.

def crc8_poly07(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc

def get_counter():
    try:
        with open(STATE) as f:
            return json.load(f)["cnt"]
    except Exception:
        return 0x05E6  # seed: pick a counter above whatever the lamp last saw

def save_counter(c):
    with open(STATE, "w") as f:
        json.dump({"cnt": c}, f)

def forge(counter, kern, mic4, stream=None):
    cnt = counter.to_bytes(2, "little")
    mesh = bytes.fromhex(MESH_BL if stream == "bl" else MESH)
    data = mesh + cnt + bytes.fromhex(kern) + bytes.fromhex(mic4)
    t5 = crc8_poly07(data) ^ 0xB5
    payload = data + bytes([t5])
    return (bytes.fromhex("0201011b03") + payload).hex()

# --- Sending -------------------------------------------------------------
def btmgmt(*args):
    # ALWAYS via sudo - even as root (root may sudo without password).
    # 2026-08-13 live proof: the direct root path (no sudo) reported add-adv
    # "Instance added" but the controller did NOT transmit (sniffer: 0 frames),
    # while the sudo path went on air immediately.
    # stdin=PIPE is the anti hang fix (no use_pty needed, a char device stdin
    # makes btmgmt/ell block forever in epoll_wait).
    cmd = ["sudo", "-n", "btmgmt", "-i", "hci0", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                              stdin=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "-n", "pkill", "-f", "btmgmt -i hci[0]"],
                       capture_output=True)
        subprocess.run(["sudo", "-n", "systemctl", "restart", "bluetooth"],
                       capture_output=True, timeout=25)
        time.sleep(3)
        subprocess.run(["sudo", "-n", "hciconfig", "hci0", "up"], capture_output=True)
        return subprocess.CompletedProcess([], 1, stdout="", stderr="TIMEOUT+BT Reset: btmgmt hang")

def prepare_radio():
    """BCM43455 wedge protection (found live 2026-08-13): after a few minutes the
    controller can silently wedge, add-adv reports 'Instance added' but the radio
    does NOT transmit (sniffer: 0 frames), while hci0 still shows UP/powered.
    hciconfig reset + up reliably restores transmission (~5s). Run before EVERY
    command (once per command, not per frame).
    ALWAYS via sudo, even as root (2026-08-13 ~18:55): the direct root path does
    not really reset the controller (add-adv OK, 0 frames on air), while
    sudo -n hciconfig makes it transmit again. Same quirk as btmgmt."""
    subprocess.run(["sudo", "-n", "hciconfig", "hci0", "reset"], capture_output=True, timeout=15)
    time.sleep(3)
    r = subprocess.run(["sudo", "-n", "hciconfig", "hci0", "up"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        print(f"prepare_radio: hciconfig up error: {r.stderr.strip()[:80]}", flush=True)
    time.sleep(0.5)

def send(adv_hex, counter):
    global LAST_CLEANUP
    btmgmt("clr-adv")                  # clear ALL instances (rm-adv 1 was unreliable)
    time.sleep(0.4)
    # add-adv ASYNC via Popen (2026-08-13 ~20:40): on a loaded controller the
    # MGMT response can take 4-5s, but the instance already advertises from the
    # moment the command is accepted, so the effective window grew to 5-7s and
    # the lamp latched the frame into its relay loop (freeze, 20:23/0x08e4,
    # sniffer verified). The app sends 2-3 copies in 1-2s and never latches.
    # So: let it advertise 1.6s, then clr-adv IMMEDIATELY. The window is then
    # bounded at about 2s regardless of the response time.
    p = subprocess.Popen(["sudo", "-n", "btmgmt", "-i", "hci0", "add-adv", "-c", "-d", adv_hex, "1"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         stdin=subprocess.PIPE)
    time.sleep(1.6)
    btmgmt("clr-adv")
    try:
        out, err = p.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = "", "TIMEOUT add-adv"
    if "added" not in (out + err).lower():
        # transient 0x0d (stale instance or similar) -> retry once
        time.sleep(0.6)
        btmgmt("clr-adv")
        time.sleep(0.4)
        p = subprocess.Popen(["sudo", "-n", "btmgmt", "-i", "hci0", "add-adv", "-c", "-d", adv_hex, "1"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             stdin=subprocess.PIPE)
        time.sleep(1.6)
        btmgmt("clr-adv")
        try:
            out, err = p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = "", "TIMEOUT add-adv retry"
        if "added" not in (out + err).lower():
            return (out or err).strip()
    # GUARANTEED cleanup (2026-08-13 ~20:30): hciconfig reset alone is NOT
    # enough (20:19: the 0x08e2 send left a stale instance despite reset+up
    # which kept flooding for minutes). A systemctl restart bluetooth is the
    # only 100% reliable kill (verified twice). The app never has this
    # problem, its radio leaves no stale instance behind.
    subprocess.run(["sudo", "-n", "systemctl", "restart", "bluetooth"], capture_output=True, timeout=30)
    time.sleep(3)
    subprocess.run(["sudo", "-n", "hciconfig", "hci0", "up"], capture_output=True, timeout=15)
    time.sleep(0.5)
    LAST_CLEANUP = time.time()
    return "OK (Z=0x%04x, ~2s sent)" % counter

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DP:
        print("Usage: tuya_beacon_ctl.py {" + "|".join(DP) + "}")
        sys.exit(1)
    name = sys.argv[1]
    kern, mic4 = DP[name]
    stream = "bl" if name.startswith("bl") or name.startswith("color") else None
    c = get_counter() + 1
    prepare_radio()                    # BCM43455 wedge protection (once per command)
    adv = forge(c, kern, mic4, stream)
    out = send(adv, c)
    save_counter(c)
    print(f"{name}: {out}")

if __name__ == "__main__":
    main()
