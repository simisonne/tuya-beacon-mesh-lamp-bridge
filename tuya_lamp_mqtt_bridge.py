#!/usr/bin/env python3
"""Tuya Beacon Mesh ceiling lamp - MQTT bridge to Home Assistant (local control).

Reads config.env (MQTT access + topics) from the same directory.
Sends commands over USB serial to the ESP32-S3 (tuya_beacon_tx.ino), which
forges the frames and broadcasts them as BLE advertising (counter lives in the
ESP32 NVS).

Flow: HA (JSON light) -> MQTT <prefix>/set -> serial -> ESP32 -> BLE -> lamp
      Status -> MQTT <prefix>/state -> HA

No btmgmt/BCM43455 anymore: forge, counter and transmit all run on the ESP32.
The bridge keeps only the MQTT part with debounce merge and nearest mapping
onto the 23 dictionary commands (an/aus/hell*/temp*/bl_*/farbe_*).
"""
import os, sys, json, time, signal, threading
import serial
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
NAME = ENV.get("DEVICE_NAME", "Deckenlampe")
UNIQ = ENV.get("DEVICE_IDENTIFIER", "tuya_lampe_deckenlampe")
SERIAL_PORT = ENV.get("SERIAL_PORT", "/dev/ttyACM0")
SERIAL_BAUD = int(ENV.get("SERIAL_BAUD", "115200"))

CMD_TOPIC = f"{PREFIX}/set"
STATE_TOPIC = f"{PREFIX}/state"
DISC_TOPIC = f"{DISC_PREFIX}/light/{UNIQ}/config"
BL_CMD_TOPIC = f"{PREFIX}/backlight/set"
BL_STATE_TOPIC = f"{PREFIX}/backlight/state"
BL_DISC_TOPIC = f"{DISC_PREFIX}/light/{UNIQ}_backlight/config"

BRIGHTNESS = {"hell25": 25, "hell52": 52, "hell79": 79}
COLORTEMP = {"temp0": 0, "temp42": 42, "temp99": 99}
# Mireds range per HA target: 154 (cold/6500K) .. 500 (warm/2000K) <-> temp_value 1000..0
MIREDS_COLD, MIREDS_WARM = 154, 500

# Backlight (capture verified 2026-08-13, stream 02):
BL_BRIGHTNESS = {"bl5": 5, "bl28": 28, "bl52": 52, "bl80": 80, "bl95": 95}
BL_COLORS = {"farbe_rot": (255, 0, 0), "farbe_gelb": (255, 255, 0),
             "farbe_gruen": (0, 255, 0), "farbe_hellblau": (0, 255, 255),
             "farbe_blau": (0, 0, 255), "farbe_pink": (255, 0, 255),
             "farbe_weiss": (255, 255, 255)}

state = {"state": "OFF", "brightness": 52, "color_temp": 42}
bl_state = {"state": "OFF", "brightness": 95, "rgb_color": (255, 255, 255)}

# --- Serial layer to the ESP32 -------------------------------------------------
_ser = None
_ser_lock = threading.Lock()

def serial_send(name):
    """Send one command name to the ESP32 and return the response line."""
    global _ser
    with _ser_lock:
        try:
            if _ser is None:
                _ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=3)
                time.sleep(0.3)          # let the port settle after (re)connect
                _ser.reset_input_buffer()
            _ser.reset_input_buffer()
            _ser.write((name + "\n").encode())
            out = ""
            line = _ser.readline().decode("utf-8", errors="replace").strip()
            if line:
                out = line
            print(f"[bridge] -> {name}: {out or '(no reply)'}", flush=True)
            return out
        except serial.SerialException as e:
            print(f"[bridge] serial error ({e}), port reopens on next command",
                  flush=True)
            try:
                if _ser is not None:
                    _ser.close()
            except Exception:
                pass
            _ser = None
            return None

def send_dp(name):
    """Always send (fresh counter on the ESP32 is harmless, the lamp applies it again).
    No state guards (2026-08-13)."""
    return serial_send(name)

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

# --- Debounce merge: slider bursts collapse into ONE command --------------------
# (2026-08-13: without the merge, 10+ MQTT messages per drag queued up -> 70s+ latency)
DEBOUNCE = 0.8          # seconds of quiet before the collected command is sent
pending = {}            # merged main light commands
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
    while True:
        time.sleep(0.2)
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
        # Always send (fresh counter = harmless, the lamp applies it again). External
        # changes (CLI, app, power cycle) desync the internal state, guards
        # would skip commands wrongly (2026-08-13).
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
        if "state" in cmd:
            if cmd["state"] in ("ON", "on", "true", True):
                send_dp("bl_anders")
                bl_state["state"] = "ON"
                changed = True
            else:
                send_dp("bl_an")
                bl_state["state"] = "OFF"
                changed = True
        # Brightness/color without state -> switch the backlight on first (app behavior)
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
        print("[bridge] ERROR in worker (bl):\n" + traceback.format_exc(), flush=True)

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[bridge] MQTT connected (rc={reason_code})", flush=True)
    client.subscribe(CMD_TOPIC)
    client.subscribe(BL_CMD_TOPIC)
    disc = {
        "name": "Deckenlampe",
        "object_id": "deckenlampe",
        "unique_id": UNIQ,
        "schema": "json",
        "command_topic": CMD_TOPIC,
        "state_topic": STATE_TOPIC,
        "supported_color_modes": ["color_temp"],   # color_temp implies brightness (HA validator: brightness not combinable)
        "min_mireds": 154,
        "max_mireds": 500,
        "device": {"identifiers": [UNIQ], "name": NAME,
                   "manufacturer": "Tuya (local)", "model": "Beacon Mesh"},
    }
    client.publish(DISC_TOPIC, json.dumps(disc), retain=True)
    bl_disc = {
        "name": "Deckenlampe Backlight",
        "object_id": "deckenlampe_backlight",
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
    print(f"[bridge] discovery: {DISC_TOPIC} + {BL_DISC_TOPIC}", flush=True)

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

print(f"[bridge] start: {MQTT_HOST}:{MQTT_PORT} cmd={CMD_TOPIC} serial={SERIAL_PORT}", flush=True)
try:
    _ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=3)
    time.sleep(0.3)
    _ser.reset_input_buffer()
    _ser.write(b"status\n")
    line = _ser.readline().decode("utf-8", errors="replace").strip()
    print(f"[bridge] ESP32: {line or '(no reply)'}", flush=True)
except serial.SerialException as e:
    print(f"[bridge] warning: ESP32 not reachable ({e}) - retried on first command",
          flush=True)
client.connect(MQTT_HOST, MQTT_PORT, 60)
threading.Thread(target=worker, args=(client,), daemon=True, name="worker").start()
client.loop_forever()
