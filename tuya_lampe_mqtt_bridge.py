#!/usr/bin/env python3
"""Tuya Beacon-Mesh ceiling lamp - MQTT bridge to Home Assistant (local control).

Reads config.env (MQTT access + topics) from the same directory.
Forge logic is imported from tuya_beacon_ctl.py (same directory).

Flow: HA (JSON light) -> MQTT <prefix>/set -> forge -> btmgmt -> lamp
      Status -> MQTT <prefix>/state -> HA

Brightness/ColorTemp: the app sends 2 frames per action (temp value + brightness
value), the unchanged value first, then the changed one (capture-verified
2026-08-13). ON/OFF is a single frame.

Depends on: python3 + paho-mqtt (pip install paho-mqtt), bluez btmgmt/hciconfig.
The bridge must run as root (systemd unit provided) OR the service user needs
NOPASSWD sudoers for btmgmt/hciconfig/pkill/systemctl bluetooth (see README).
"""
import os, sys, json, time, signal, threading, subprocess, importlib.util
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))

def load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env(os.path.join(HERE, "config.env"))
MQTT_HOST = ENV.get("MQTT_HOST", "192.168.1.173")
MQTT_PORT = int(ENV.get("MQTT_PORT", "1883"))
MQTT_USER = ENV.get("MQTT_USER", "homeassistant")
MQTT_PASS = ENV.get("MQTT_PASS", "")
PREFIX = ENV.get("MQTT_TOPIC_PREFIX", "tuya/lampe")
DISC_PREFIX = ENV.get("HA_DISCOVERY_PREFIX", "homeassistant")
NAME = ENV.get("DEVICE_NAME", "Tuya Ceiling Lamp")
UNIQ = ENV.get("DEVICE_IDENTIFIER", "tuya_lampe_deckenlampe")

CMD_TOPIC = f"{PREFIX}/set"
STATE_TOPIC = f"{PREFIX}/state"
DISC_TOPIC = f"{DISC_PREFIX}/light/{UNIQ}/config"
BL_CMD_TOPIC = f"{PREFIX}/backlight/set"
BL_STATE_TOPIC = f"{PREFIX}/backlight/state"
BL_DISC_TOPIC = f"{DISC_PREFIX}/light/{UNIQ}_backlight/config"

# Forge logic imported from tuya_beacon_ctl.py (same directory)
spec = importlib.util.spec_from_file_location("tbc", os.path.join(HERE, "tuya_beacon_ctl.py"))
tbc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tbc)

BRIGHTNESS = {"hell25": 25, "hell52": 52, "hell79": 79}
COLORTEMP = {"temp0": 0, "temp42": 42, "temp99": 99}
# Mireds range per HA target: 154 (cold/6500K) .. 500 (warm/2000K) <-> temp_value 1000..0
MIREDS_COLD, MIREDS_WARM = 154, 500

# Backlight (2026-08-13 capture-verified, stream 02!):
BL_BRIGHTNESS = {"bl5": 5, "bl28": 28, "bl52": 52, "bl80": 80, "bl95": 95}
BL_COLORS = {"farbe_rot": (255, 0, 0), "farbe_gelb": (255, 255, 0),
             "farbe_gruen": (0, 255, 0), "farbe_hellblau": (0, 255, 255),
             "farbe_blau": (0, 0, 255), "farbe_pink": (255, 0, 255),
             "farbe_weiss": (255, 255, 255)}

state = {"state": "OFF", "brightness": 52, "color_temp": 42}
bl_state = {"state": "OFF", "brightness": 95, "rgb_color": (255, 255, 255)}

def send_dp(name):
    kern, mic4 = tbc.DP[name]
    stream = "bl" if name.startswith("bl") or name.startswith("farbe") else None
    c = tbc.get_counter() + 1
    adv = tbc.forge(c, kern, mic4, stream)
    out = tbc.send(adv, c)
    tbc.save_counter(c)
    print(f"[bridge] -> {name} (Z=0x{c:04x}): {out}", flush=True)
    return out

def nearest(v, table):
    return min(table, key=lambda x: abs(x - v))

def nearest_color(rgb):
    return min(BL_COLORS, key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb, BL_COLORS[k])))

def dp_for(name, table):
    return [k for k, v in table.items() if v == name][0]

def publish_state(client):
    b = round(state["brightness"] / 100 * 255)
    mireds = round(MIREDS_WARM - state["color_temp"] / 100 * (MIREDS_WARM - MIREDS_COLD))
    # JSON light with supported_color_modes needs "color_mode" in the state
    payload = json.dumps({"state": state["state"], "color_mode": "color_temp",
                          "brightness": b, "color_temp": mireds})
    client.publish(STATE_TOPIC, payload, retain=True)
    print(f"[bridge] state: {payload}", flush=True)

def publish_bl_state(client):
    b = round(bl_state["brightness"] / 100 * 255)
    r, g, bl = bl_state["rgb_color"]
    payload = json.dumps({"state": bl_state["state"], "color_mode": "rgb",
                          "brightness": b, "rgb_color": [r, g, bl]})
    client.publish(BL_STATE_TOPIC, payload, retain=True)
    print(f"[bridge] bl-state: {payload}", flush=True)

# --- Debounce-merge: slider bursts become ONE command --------------------
# (2026-08-13: without merging, 10+ MQTT messages per drag queued up -> 70s+ latency)
DEBOUNCE = 0.8          # seconds of quiet before the accumulated command is sent
pending = {}            # merged main-light commands
bl_pending = {}         # merged backlight commands
pending_time = 0.0
bl_pending_time = 0.0
plock = threading.Lock()

def on_message(client, userdata, msg):
    global pending, bl_pending, pending_time, bl_pending_time
    try:
        cmd = json.loads(msg.payload.decode())
    except Exception:
        print(f"[bridge] unparseable: {msg.payload!r}", flush=True)
        return
    is_bl = msg.topic == BL_CMD_TOPIC
    print(f"[bridge] {'bl-' if is_bl else ''}cmd: {json.dumps(cmd)}", flush=True)
    with plock:
        if is_bl:
            for k in ("state", "brightness"):
                if k in cmd:
                    bl_pending[k] = cmd[k]
            # HA JSON light sends color as "color": {r,g,b} (sometimes "rgb_color")
            if "rgb_color" in cmd:
                bl_pending["rgb_color"] = list(cmd["rgb_color"])
            elif isinstance(cmd.get("color"), dict):
                c = cmd["color"]
                bl_pending["rgb_color"] = [c.get("r", 0), c.get("g", 0), c.get("b", 0)]
            bl_pending_time = time.time()
        else:
            for k in ("state", "brightness", "color_temp"):
                if k in cmd:
                    pending[k] = cmd[k]
            pending_time = time.time()

def worker(client):
    global pending, bl_pending, pending_time, bl_pending_time, state, bl_state
    last_cleanup = time.time()
    while True:
        time.sleep(0.2)
        # Stale-instance watchdog (2026-08-13: clr-adv reports "removed" but the
        # controller keeps transmitting -> an old frame floods the mesh and blocks
        # new sends; hciconfig reset + up clears it reliably).
        # TIME-BASED instead of idle-only (2026-08-13 ~16:40): the flood happened
        # during ACTIVE use (16:05-16:08), so an idle-only watchdog never fired.
        # Now reset every 5 min regardless of traffic. The worker is single-threaded
        # (sends block), so this never interrupts an in-flight send; the next
        # command does its own prepare_radio anyway.
        if time.time() - last_cleanup > 300:
            last_cleanup = time.time()
            tbc.prepare_radio()
            print("[bridge] Stale-Watchdog: hciconfig reset (5-min)", flush=True)
        with plock:
            take, take_bl = None, None
            if pending and time.time() - pending_time >= DEBOUNCE:
                take = dict(pending); pending.clear()
            if bl_pending and time.time() - bl_pending_time >= DEBOUNCE:
                take_bl = dict(bl_pending); bl_pending.clear()
        if take is not None:
            _apply_main(client, take)
        if take_bl is not None:
            _apply_bl(client, take_bl)

def _apply_main(client, cmd):
    global state
    try:
        changed = False
        # BCM43455 wedge protection: fresh radio once per command
        tbc.prepare_radio()
        # ALWAYS send (fresh counter = harmless re-apply, lamp dedup only blocks
        # same-counter repeats). Reason: external changes (CLI, app, power cycle)
        # desync the internal state -> state guards would wrongly skip commands.
        if "state" in cmd:
            if cmd["state"] in ("ON", "on", "true", True):
                send_dp("an")
                state["state"] = "ON"
                changed = True
            else:
                send_dp("aus")
                state["state"] = "OFF"
                changed = True
        if state["state"] == "ON":
            if "brightness" in cmd:
                pct = round(int(cmd["brightness"]) / 255 * 100)
                key = nearest(pct, BRIGHTNESS.values())
                send_dp(dp_for(key, BRIGHTNESS))
                state["brightness"] = key
                changed = True
            if "color_temp" in cmd:
                mireds = int(cmd["color_temp"])
                pct = round((MIREDS_WARM - mireds) / (MIREDS_WARM - MIREDS_COLD) * 100)
                key = nearest(pct, COLORTEMP.values())
                send_dp(dp_for(key, COLORTEMP))
                state["color_temp"] = key
                changed = True
        if changed:
            publish_state(client)
    except Exception:
        import traceback
        print("[bridge] ERROR in worker (main):\n" + traceback.format_exc(), flush=True)

def _apply_bl(client, cmd):
    global bl_state
    try:
        changed = False
        tbc.prepare_radio()            # BCM43455 wedge protection
        if "state" in cmd:
            if cmd["state"] in ("ON", "on", "true", True):
                send_dp("bl_anders")
                bl_state["state"] = "ON"
                changed = True
            else:
                send_dp("bl_an")
                bl_state["state"] = "OFF"
                changed = True
        # brightness/color without state -> switch backlight on first (app behavior)
        if ("brightness" in cmd or "rgb_color" in cmd) and bl_state["state"] != "ON" and "state" not in cmd:
            send_dp("bl_anders")
            bl_state["state"] = "ON"
            changed = True
        if bl_state["state"] == "ON":
            if "brightness" in cmd:
                pct = round(int(cmd["brightness"]) / 255 * 100)
                key = nearest(pct, BL_BRIGHTNESS.values())
                send_dp(dp_for(key, BL_BRIGHTNESS))
                bl_state["brightness"] = key
                changed = True
            if "rgb_color" in cmd:
                rgb = tuple(int(x) for x in cmd["rgb_color"][:3])
                send_dp(nearest_color(rgb))
                bl_state["rgb_color"] = rgb
                changed = True
        if changed:
            publish_bl_state(client)
    except Exception:
        import traceback
        print("[bridge] ERROR in worker (backlight):\n" + traceback.format_exc(), flush=True)

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[bridge] MQTT connected (rc={reason_code})", flush=True)
    client.subscribe(CMD_TOPIC)
    client.subscribe(BL_CMD_TOPIC)
    disc = {
        "name": NAME,
        "object_id": UNIQ,
        "unique_id": UNIQ,
        "schema": "json",
        "command_topic": CMD_TOPIC,
        "state_topic": STATE_TOPIC,
        "supported_color_modes": ["color_temp"],   # color_temp implies brightness (HA validator)
        "min_mireds": 154,
        "max_mireds": 500,
        "device": {"identifiers": [UNIQ], "name": NAME,
                   "manufacturer": "Tuya (local)", "model": "Beacon Mesh"},
    }
    client.publish(DISC_TOPIC, json.dumps(disc), retain=True)
    bl_disc = {
        "name": f"{NAME} Backlight",
        "object_id": f"{UNIQ}_backlight",
        "unique_id": f"{UNIQ}_backlight",
        "schema": "json",
        "command_topic": BL_CMD_TOPIC,
        "state_topic": BL_STATE_TOPIC,
        "supported_color_modes": ["rgb"],          # rgb implies brightness
        "device": {"identifiers": [UNIQ], "name": NAME,
                   "manufacturer": "Tuya (local)", "model": "Beacon Mesh"},
    }
    client.publish(BL_DISC_TOPIC, json.dumps(bl_disc), retain=True)
    publish_state(client)
    publish_bl_state(client)
    print(f"[bridge] Discovery: {DISC_TOPIC} + {BL_DISC_TOPIC}", flush=True)

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tuya-lampe-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.on_connect = on_connect
client.on_message = on_message

def stop(sig, frm):
    print("[bridge] stop", flush=True)
    client.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

print(f"[bridge] Start: {MQTT_HOST}:{MQTT_PORT} cmd={CMD_TOPIC}", flush=True)
client.connect(MQTT_HOST, MQTT_PORT, 60)
threading.Thread(target=worker, args=(client,), daemon=True, name="worker").start()
client.loop_forever()
