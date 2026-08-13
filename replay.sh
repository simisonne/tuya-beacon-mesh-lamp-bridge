#!/bin/bash
# LEGACY: static-frame replay sender (btmgmt legacy path, Pi/BCM43455).
# SUPERSEDED by tuya_beacon_ctl.py, which forges frames with FRESH counters.
# The static frames below carry old counters and will be dedup-rejected by a
# lamp that has already seen higher counters. Kept for protocol reference.
# Usage: ./replay.sh {aus|an|stop}
# V3: exact original frame (31B AdvData, flags+1b03 included), length byte
# 1b->1c corrected (28 instead of 27 so the kernel TLV check passes). CRC NOT included.
AUS="0201011c030b61bc0007019805dfcf36aa6b2c34ed15db67bd3d60d8d6feff"
AN="0201011c030b61bc00070197053a36cebe8edf95850ae6308b55883e6ed480"
case "$1" in
  aus)  sudo btmgmt -i hci0 add-adv -c -d "$AUS" 1 && echo "OFF frame transmitting (1.28s interval)... (stop with: ./replay.sh stop)" ;;
  an)   sudo btmgmt -i hci0 add-adv -c -d "$AN" 1 && echo "ON frame transmitting (1.28s interval)... (stop with: ./replay.sh stop)" ;;
  stop) sudo btmgmt -i hci0 rm-adv 1 && echo "Advertising stopped" ;;
  *) echo "Usage: $0 {aus|an|stop}"; exit 1 ;;
esac
