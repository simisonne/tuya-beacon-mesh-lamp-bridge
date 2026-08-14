// ESP32-S3 Tuya Beacon-Mesh transmitter - forged frames, NVS counter, serial control.
// Forge protocol: see docs/PROTOCOL.md
//   Frame (31 B AdvData): 020101 | 1b03 | mesh(6) | cnt(2 LE) | kern(13) | MIC4(4) | t5(1)
//   t5 = crc8(poly 0x07, init 0) over mesh+cnt+kern+MIC4, XOR 0xB5
//   Header 0b61bc000702 for ALL commands (app verified 2026-08-14).
// Counter: persisted in NVS, +1 per command, global (ahead of the app's spaces).
// Burst: one ADV frame per command, then the radio goes idle. Never long-lived
// retransmission (a stale frame repeating on air can latch the lamp into a
// relay loop and freeze its command handler).
//
// Build (Arduino CLI, esp32 core 3.x):
//   arduino-cli compile --fqbn "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc" tuya_beacon_tx
//   esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 460800 write_flash 0x0 <merged.bin>
// Pitfalls: BLEDevice::init() only ONCE in setup() (per-command init triggers
// an RF calibration current spike -> brownout reset on a USB supply), and the
// merged image wipes NVS, so resync the counter after every flash.

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>
#include <Preferences.h>

// ---------- command dictionary: kern(13) + MIC4(4), per command ----------
struct Cmd { const char *name; uint8_t kern[13]; uint8_t mic4[4]; bool bl; };

#define K(...) { __VA_ARGS__ }
static const Cmd CMDS[] = {
  // main light
  {"on",      K(0xdf,0xcf,0x36,0xaa,0x6b,0x2c,0x34,0xed,0x15,0xdb,0x67,0xbd,0x3d), K(0x60,0xd8,0xd6,0xfe), false},
  {"off",     K(0x3a,0x36,0xce,0xbe,0x8e,0xdf,0x95,0x85,0x0a,0xe6,0x30,0x8b,0x55), K(0x88,0x3e,0x6e,0xd4), false},
  {"bri25",   K(0xbe,0x56,0xd4,0xcf,0xff,0xdf,0xb0,0xcf,0x31,0x4e,0x65,0xb0,0x36), K(0x4c,0xcf,0xa9,0xa3), false},
  {"bri52",   K(0x5c,0x41,0xd1,0x6e,0xaa,0xbc,0x41,0xc2,0x3e,0x6c,0xba,0x39,0xc6), K(0xec,0xb5,0xec,0x3f), false},
  {"bri79",   K(0x4c,0x09,0xc4,0xf8,0x4b,0xb8,0x69,0x3d,0xdf,0xaf,0x6a,0xe0,0xec), K(0x44,0xa5,0x0c,0xc9), false},
  {"temp0",   K(0x10,0x9e,0xe0,0x2e,0x7d,0xba,0xd1,0x2c,0x34,0xf4,0x3b,0x2a,0x01), K(0x96,0x0c,0x83,0x17), false},
  {"temp42",  K(0x80,0x37,0x84,0x2f,0x22,0x7b,0x37,0xf7,0x92,0xf1,0x87,0x84,0x6a), K(0x83,0xb1,0x05,0x65), false},
  {"temp99",  K(0x66,0xe1,0x75,0xb3,0xb7,0x6e,0xc4,0x2e,0xef,0x84,0x2d,0xaf,0x88), K(0x5a,0x36,0x66,0x0b), false},
  // backlight
  {"bl_on",   K(0xe9,0x44,0x7e,0x2f,0xc8,0x5a,0xf4,0x28,0x7c,0x3b,0x9a,0x55,0x0b), K(0x2c,0xe9,0xe3,0x48), true},
  {"bl_off",  K(0x0c,0x63,0x22,0xe9,0x9f,0xc4,0x51,0x7f,0x9a,0x23,0x60,0x26,0x3d), K(0x8e,0x67,0x8e,0x23), true},
  {"bl5",     K(0x55,0xbb,0x35,0xe8,0xf8,0x23,0xf1,0x11,0x93,0x52,0x5d,0x10,0x7d), K(0xa6,0xff,0xf9,0xfb), true},
  {"bl28",    K(0x03,0xb0,0x75,0x87,0x09,0xac,0x62,0xa6,0xee,0xe4,0xd5,0xc1,0xac), K(0x9d,0x40,0x69,0x05), true},
  {"bl52",    K(0x3e,0xfa,0x92,0xfa,0x33,0xad,0x32,0xc5,0x94,0x3a,0xb2,0x72,0x88), K(0x9c,0x0d,0xa5,0x6c), true},
  {"bl80",    K(0x9a,0x4c,0xa0,0x7e,0xae,0xc9,0x00,0xf8,0xb9,0xdc,0x88,0x15,0xeb), K(0x42,0x06,0xd2,0xd2), true},
  {"bl95",    K(0x3a,0x9a,0xea,0xd6,0x50,0x63,0x97,0xaa,0xda,0xfc,0xa7,0x42,0x98), K(0x20,0xf3,0x31,0x3c), true},
  {"color_red",     K(0x75,0x26,0xa3,0x54,0x93,0x8a,0xe0,0xc6,0xa9,0x49,0xa1,0xf7,0xab), K(0x4c,0xe1,0xea,0x73), true},
  {"color_yellow",  K(0x16,0x16,0xb1,0x27,0xae,0xcd,0x0b,0xa3,0xf4,0x7a,0x80,0x39,0x76), K(0x11,0x99,0xb3,0x55), true},
  {"color_green",   K(0xb3,0xc4,0x85,0xf9,0xf2,0xb7,0x06,0x5f,0x4c,0xab,0x3d,0x33,0xcd), K(0x56,0x8a,0x07,0x2f), true},
  {"color_cyan",    K(0x5b,0x77,0x01,0x7b,0x89,0x8c,0x8c,0x27,0x5b,0x9a,0x04,0x51,0x1a), K(0x3a,0xaa,0x4e,0x6b), true},
  {"color_blue",    K(0xfe,0x2c,0xdc,0x8c,0x63,0xb7,0xcc,0x39,0xa9,0xa7,0x72,0x99,0x1d), K(0x97,0x20,0x11,0x7b), true},
  {"color_pink",    K(0x8c,0x71,0xde,0x50,0x29,0xa3,0xe7,0x12,0x9b,0xf9,0x3d,0xd9,0xad), K(0x90,0x40,0x01,0x2e), true},
  {"color_white",   K(0xfb,0x7d,0xd7,0x7a,0x68,0x7d,0xa3,0xce,0x3f,0x23,0x22,0xfd,0x66), K(0x7c,0xeb,0x5f,0xed), true},
};
static const int NUM_CMDS = sizeof(CMDS) / sizeof(CMDS[0]);

// ---------- mesh header ----------
// 2026-08-14: the app sends ALL commands (main light AND backlight) on
// 0b61bc000702. The 000701 variant is the WiFi gateway path with its own
// counter space; frames sent there get stale-rejected once the gateway has
// been active. Always 000702.
static const uint8_t HDR[6] = {0x0b, 0x61, 0xbc, 0x00, 0x07, 0x02};

// ---------- state ----------
Preferences prefs;
static uint32_t g_counter = 0x0600;   // seed; overwritten from NVS on boot
static const char *PREFS_NS = "tuya";
static const char *PREFS_KEY = "cnt";
static BLEAdvertising *pAdv = NULL;   // init ONCE in setup() - per-command init
                                      // triggers RF calibration current spikes

// ---------- CRC8 poly 0x07, init 0, no refin/refout, MSB-first ----------
static uint8_t crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// ---------- forge: build the 31-B AdvData into out ----------
static void forge(uint32_t counter, const Cmd &cmd, uint8_t *out) {
  size_t i = 0;
  out[i++] = 0x02; out[i++] = 0x01; out[i++] = 0x01;        // flags
  out[i++] = 0x1b; out[i++] = 0x03;                          // 27 = 1 type + 26 data
  memcpy(out + i, HDR, 6); i += 6;
  out[i++] = counter & 0xFF; out[i++] = (counter >> 8) & 0xFF; // cnt LE
  memcpy(out + i, cmd.kern, 13); i += 13;
  memcpy(out + i, cmd.mic4, 4); i += 4;
  uint8_t crc = crc8(out + 5, 25);                          // mesh+cnt+kern+MIC4
  out[i++] = crc ^ 0xB5;
  // i == 31
}

// ---------- transmit: single ADV frame, then radio idle ----------
static void transmit(const uint8_t *frame) {
  BLEAdvertisementData advData;
  advData.addData((char *)frame, 31);
  pAdv->setAdvertisementData(advData);
  pAdv->start();
  delay(300);                     // ~1 frame - the lamp acts on the first
  pAdv->stop();
}

// ---------- dispatch ----------
static bool sendCommand(const char *name) {
  for (int n = 0; n < NUM_CMDS; n++) {
    if (strcmp(name, CMDS[n].name) == 0) {
      uint32_t cnt = g_counter + 1;
      uint8_t frame[31];
      forge(cnt, CMDS[n], frame);
      transmit(frame);
      g_counter = cnt;
      prefs.putUInt(PREFS_KEY, g_counter);
      Serial.printf("OK %s Z=0x%04X\n", name, cnt);
      return true;
    }
  }
  return false;
}

static void printHelp() {
  Serial.println("Commands: on off bri25 bri52 bri79 temp0 temp42 temp99");
  Serial.println("          bl_on bl_off bl5 bl28 bl52 bl80 bl95");
  Serial.println("          color_red color_yellow color_green color_cyan");
  Serial.println("          color_blue color_pink color_white");
  Serial.println("          status | resync <hex> | help");
  Serial.printf("Counter: 0x%04X\n", g_counter);
}

void setup() {
  Serial.begin(115200);
  // -6 dBm advertising (ESP_PWR_LVL_N6=2, ESP_BLE_PWR_TYPE_ADV=9) reaches the
  // lamp and keeps the current draw low on a USB supply. Raise to 0 dBm
  // (level 4) if your lamp sits further away.
  BLEDevice::setPower((esp_power_level_t)2, (esp_ble_power_type_t)9);
  BLEDevice::init("");                       // once - RF calibration only at boot
  pAdv = BLEDevice::getAdvertising();
  pAdv->setAdvertisementType(0x00);          // ADV_TYPE_IND (connectable undirected)
  pAdv->setScanResponse(false);
  pAdv->setMinInterval(0x0280);              // 400 ms
  pAdv->setMaxInterval(0x0280);
  prefs.begin(PREFS_NS, false);
  g_counter = prefs.getUInt(PREFS_KEY, 0x0600);
  Serial.printf("\nTuya Beacon TX ESP32-S3, Counter 0x%04X\n", g_counter);
  printHelp();
}

void loop() {
  if (Serial.available()) {
    static char line[64];
    static size_t len = 0;
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        line[len] = 0;
        char *cmd = line;
        char *arg = strchr(line, ' ');
        if (arg) { *arg = 0; arg++; }
        if (strcmp(cmd, "help") == 0) printHelp();
        else if (strcmp(cmd, "status") == 0)
          Serial.printf("Counter 0x%04X\n", g_counter);
        else if (strcmp(cmd, "resync") == 0 && arg) {
          g_counter = (uint32_t)strtoul(arg, NULL, 16);
          prefs.putUInt(PREFS_KEY, g_counter);
          Serial.printf("Counter set to 0x%04X\n", g_counter);
        } else if (!sendCommand(cmd)) {
          Serial.printf("Unknown: %s\n", cmd);
          printHelp();
        }
      }
      len = 0;
    } else if (len < sizeof(line) - 1) {
      line[len++] = c;
    }
  }
}
