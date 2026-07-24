#!/usr/bin/env python3
"""Push a firmware.bin to the discovery logger's HTTP /update endpoint.

One-directional TCP push (client -> device), unlike ArduinoOTA's UDP
invitation + callback-connection handshake, which needs a route back from
the device to this machine and turned out to be unreliable over the
hobocamp's HaLow bridge. The device streams the body straight to flash and
only reboots if the write completes and the MD5 matches; any failure
leaves the currently-running firmware untouched.

Sends the body in small chunks with progress output and a per-chunk (not
overall) timeout - confirmed live that a real transfer over a marginal
link can legitimately take 80+ seconds, so a single static timeout on the
whole operation was giving up on transfers that were actually still
making progress. Retries the whole push automatically on failure, since a
partial/aborted attempt never commits anything on the device side (only a
verified, complete, MD5-matched write reboots it).

Usage:
    python tools/push_update.py .pio/build/esp32-c3-devkitm-1/firmware.bin \
        --host 192.168.2.4 --token <OTA_PASSWORD from secrets.h>
"""

import argparse
import hashlib
import socket
import sys
import time

CHUNK_SIZE = 4096


def send_once(host: str, port: int, token: str, data: bytes, md5: str,
               chunk_timeout: float) -> tuple[bool, str]:
    """One attempt. Returns (success, message)."""
    size = len(data)
    request_line = f"POST /update?token={token} HTTP/1.1\r\n"
    headers = (
        f"Host: {host}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {size}\r\n"
        f"X-Firmware-MD5: {md5}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )

    sock = socket.create_connection((host, port), timeout=chunk_timeout)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(chunk_timeout)
        sock.sendall(request_line.encode())
        sock.sendall(headers.encode())

        sent = 0
        last_pct = -10
        while sent < size:
            chunk = data[sent:sent + CHUNK_SIZE]
            sock.sendall(chunk)
            sent += len(chunk)
            pct = int(100 * sent / size)
            if pct >= last_pct + 10:
                last_pct = pct - (pct % 10)
                print(f"  {pct}% ({sent}/{size} bytes)", flush=True)

        # Response won't arrive until the device finishes Update.end() and
        # writes it, which can itself take a few seconds - give it the
        # same per-operation patience as the upload itself.
        sock.settimeout(max(chunk_timeout, 30.0))
        response = b""
        while b"\r\n\r\n" not in response:
            piece = sock.recv(4096)
            if not piece:
                break
            response += piece
        # Best-effort: read any remaining body already buffered.
        try:
            sock.settimeout(2.0)
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                response += piece
        except OSError:
            pass

        text = response.decode(errors="replace")
        status_line = text.split("\r\n", 1)[0] if text else "(no response)"
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        ok = " 200 " in f" {status_line} "
        return ok, f"{status_line} - {body.strip()}"
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("firmware", help="Path to firmware.bin")
    parser.add_argument("--host", default="192.168.2.4")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--token", required=True, help="OTA_PASSWORD from secrets.h")
    parser.add_argument("--chunk-timeout", type=float, default=30.0,
                         help="Seconds of no progress on a single send/recv before giving up "
                              "on this attempt (not a cap on total transfer time)")
    parser.add_argument("--retries", type=int, default=5,
                         help="Automatic retries on failure; safe since a failed/partial "
                              "push never reboots the device")
    parser.add_argument("--retry-delay", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.firmware, "rb") as f:
        data = f.read()
    size = len(data)
    md5 = hashlib.md5(data).hexdigest()
    print(f"Firmware: {args.firmware} ({size} bytes, md5={md5})")

    for attempt in range(1, args.retries + 1):
        print(f"\nAttempt {attempt}/{args.retries}: uploading to "
              f"http://{args.host}:{args.port}/update ...")
        start = time.time()
        try:
            ok, message = send_once(args.host, args.port, args.token, data, md5,
                                     args.chunk_timeout)
        except OSError as exc:
            ok, message = False, f"connection error: {exc}"
        elapsed = time.time() - start
        print(f"  [{elapsed:.1f}s] {message}")

        if ok:
            print("\nUpdate accepted, device is rebooting. Give it ~10-15s then check /status.")
            return 0

        if attempt < args.retries:
            print(f"  Failed, retrying in {args.retry_delay:.0f}s "
                  f"(safe - nothing commits on the device until a full verified write)...")
            time.sleep(args.retry_delay)

    print(f"\nGave up after {args.retries} attempts. Device is still running its "
          "previous firmware untouched.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
