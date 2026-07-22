#!/usr/bin/env python3
"""Push a firmware.bin to the discovery logger's HTTP /update endpoint.

One-directional TCP push (client -> device), unlike ArduinoOTA's UDP
invitation + callback-connection handshake, which needs a route back from
the device to this machine and turned out to be unreliable over the
hobocamp's HaLow bridge. The device streams the body straight to flash and
only reboots if the write completes and the MD5 matches; any failure
leaves the currently-running firmware untouched.

Usage:
    python tools/push_update.py .pio/build/esp32-c3-devkitm-1/firmware.bin \
        --host 192.168.2.4 --token <OTA_PASSWORD from secrets.h>
"""

import argparse
import hashlib
import http.client
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", help="Path to firmware.bin")
    parser.add_argument("--host", default="192.168.2.4")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--token", required=True, help="OTA_PASSWORD from secrets.h")
    parser.add_argument("--timeout", type=float, default=120.0, help="Socket timeout in seconds")
    args = parser.parse_args()

    with open(args.firmware, "rb") as f:
        data = f.read()
    size = len(data)
    md5 = hashlib.md5(data).hexdigest()
    print(f"Firmware: {args.firmware} ({size} bytes, md5={md5})")

    conn = http.client.HTTPConnection(args.host, args.port, timeout=args.timeout)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Firmware-MD5": md5,
    }

    print(f"Uploading to http://{args.host}:{args.port}/update ...")
    start = time.time()
    try:
        conn.request("POST", f"/update?token={args.token}", body=data, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")
    except OSError as exc:
        print(f"Connection failed: {exc}")
        return 1
    elapsed = time.time() - start

    print(f"Response {resp.status} in {elapsed:.1f}s: {body.strip()}")
    if resp.status != 200:
        return 1

    print("Update accepted, device is rebooting. Give it ~10-15s then check /status.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
