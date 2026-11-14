"""
Relay: send readings from a board on THIS laptop to Kelvin running on ANOTHER laptop.

We use it for the cooling probe: the robot is plugged into the laptop running server.py,
and the cooling probe is plugged into a second laptop running this script.

    python relay.py --port COM6 --to http://192.168.1.23:8000

server.py prints the address to use ("Other laptops reach this one at ...").
Both laptops must be on the same network. Venue Wi-Fi often blocks laptops from
talking to each other, so put both on one phone hotspot if it doesn't connect.
Close the Arduino Serial Monitor first: only one program can use a USB port at a time.
"""

import argparse
import json
import threading
import time
import urllib.request

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="this board's COM port, e.g. COM6")
    ap.add_argument("--to", required=True, help="Kelvin's address, e.g. http://192.168.1.23:8000")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()
    url = args.to.rstrip("/") + "/api/lines"

    # 1. check we can reach Kelvin before opening the board
    try:
        with urllib.request.urlopen(args.to.rstrip("/") + "/health", timeout=5) as r:
            print("Reached Kelvin:", r.read(200).decode(errors="ignore"))
    except Exception as e:
        print(f"Can't reach Kelvin at {args.to}: {e}")
        print("Check: same network (try a phone hotspot), right IP, server.py running, Windows firewall allowed Python.")
        return

    pending, lock = [], threading.Lock()
    stats = {"sent": 0, "failed": 0}

    def sender():
        """Every 0.2 s, post whatever lines came in. Keeps going if the network hiccups."""
        while True:
            time.sleep(0.2)
            with lock:
                batch = pending[:500]
                del pending[:500]
            if not batch:
                continue
            try:
                req = urllib.request.Request(url, json.dumps({"lines": batch}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).close()
                stats["sent"] += len(batch)
            except Exception as e:
                stats["failed"] += len(batch)
                print("Send failed:", e)

    threading.Thread(target=sender, daemon=True).start()

    # 2. read the board forever, reconnecting if the cable is bumped
    while True:
        try:
            with serial.Serial(args.port, args.baud, timeout=1) as ser:
                print(f"Reading {args.port}. Forwarding to {url}. Ctrl+C to stop.")
                last_print = time.time()
                while True:
                    raw = ser.readline()
                    if raw:
                        line = raw.decode(errors="ignore").strip()
                        if line:
                            with lock:
                                pending.append(line)
                    if time.time() - last_print > 10:
                        print(f"  sent {stats['sent']} lines, {stats['failed']} failed")
                        last_print = time.time()
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"Board problem on {args.port}: {e}. Retrying in 2 s.")
            time.sleep(2)


if __name__ == "__main__":
    main()
