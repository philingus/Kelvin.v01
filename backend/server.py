"""
Kelvin backend.

Reads the robot and the cooling probe over USB, spots mistakes, explains them,
and streams everything to the dashboard at ws://localhost:8000/ws.

Run with fake hardware (no boards needed):
    python server.py --fake

Run with real boards (find the COM port in the Arduino IDE under Tools > Port):
    python server.py --motor-port COM5 --cooling-port COM6

Either port can be left out. Close the Arduino Serial Monitor first:
only one program can use a USB port at a time.

Optional AI explanations: set GEMINI_API_KEY (or ANTHROPIC_API_KEY) before running.
Without a key, Kelvin uses built-in plain-language explanations, so the demo
never waits on the internet.
"""

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

ROOM_TEMP = 22.0           # degrees C, change to the room's temperature
COOL_DROP_ALERT = 6.0      # degrees below expected that counts as a mistake
OVERSHOOT_ALERT = 25.0     # percent overshoot that counts as a mistake

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    global loop
    loop = asyncio.get_running_loop()
    yield

app = FastAPI(lifespan=lifespan)
clients: set = set()
loop: asyncio.AbstractEventLoop | None = None
motor_link = None          # object with .write(str) for sending commands to the robot
settings = {"kp": 1.0, "ki": 2.0, "kd": 0.0}


# ---------------------------------------------------------------- broadcast

def send(msg: dict):
    """Thread-safe: queue a JSON message to every connected dashboard."""
    if loop is None:
        return
    text = json.dumps(msg)
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(_safe_send(ws, text), loop)


async def _safe_send(ws, text):
    try:
        await ws.send_text(text)
    except Exception:
        clients.discard(ws)


# ---------------------------------------------------------------- explanations

TEMPLATES = {
    "overshoot": "The wheel overshot its target by {over} percent and kept oscillating. "
                 "The first setting, kp, is too high for this motor. Lower it to about a third.",
    "slow": "The wheel only reached {reach} percent of its target. The first setting, kp, is too low. Raise it by half.",
    "steady_error": "The wheel settled {err} percent below its target and stayed there. "
                    "Add some of the second setting, ki, to close the gap.",
    "saturated": "The motor was at full power {sat} percent of the time. "
                 "The settings are too aggressive, or the battery is low. Lower kp, and check the battery.",
    "jitter": "The motor power is jittering. The third setting, kd, is amplifying sensor noise. Lower it.",
    "fixed": "That worked. Overshoot went from {before} percent to {after} percent.",
    "not_fixed": "Better, but still {after} percent overshoot. Let's cut kp in half again.",
    "probe_out": "Your reading dropped {drop} degrees below where it should be. "
                 "The probe looks like it is out of the water.",
    "probe_back": "The probe is back and the readings match again. I logged the mistake for the report.",
}


def ai_rewrite(issue: str, facts: dict, fallback: str) -> str:
    """Ask an LLM for a short spoken explanation. Returns the fallback on any problem."""
    prompt = (
        "You are Kelvin, a friendly lab partner for engineering students. In at most two short "
        "sentences that will be read out loud, explain this lab mistake and the fix in plain words. "
        f"Issue: {issue}. Measurements: {json.dumps(facts)}. Suggested fix already decided: {fallback}"
    )
    try:
        if os.environ.get("GEMINI_API_KEY"):
            model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                   f"?key={os.environ['GEMINI_API_KEY']}")
            body = {"contents": [{"parts": [{"text": prompt}]}]}
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.load(r)
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if os.environ.get("ANTHROPIC_API_KEY"):
            model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
            body = {"model": model, "max_tokens": 150, "messages": [{"role": "user", "content": prompt}]}
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                {"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.load(r)
            return data["content"][0]["text"].strip()
    except Exception as e:
        print("AI explanation failed, using template:", e)
    return fallback


def diagnose(lab: str, issue: str, facts: dict, fix: dict | None = None):
    """Send a diagnosis. The AI call runs in a background thread so nothing blocks."""
    text = TEMPLATES[issue].format(**facts)

    def worker():
        final = ai_rewrite(issue, facts, text) if issue not in ("fixed", "probe_back") else text
        msg = {"type": "diagnosis", "lab": lab,
               "issue": "ok" if issue in ("fixed", "probe_back") else issue, "text": final}
        if fix:
            msg["fix"] = fix
        print(f"[diagnosis] {lab} {issue}: {final}  fix={fix}")
        send(msg)

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- motor analysis

class MotorWatcher:
    """Collects one step at a time (target change to target change) and judges it."""

    def __init__(self):
        self.target = None
        self.start_speed = 0.0
        self.samples = []          # (speed, motor)
        self.flagged = False
        self.verify = False
        self.last_over = None

    def add(self, target, speed, motor):
        if self.target is None:
            self.target = target
        if target != self.target:
            self.finish()
            self.start_speed = speed
            self.target = target
            self.samples = []
        self.samples.append((speed, motor))

    def finish(self):
        step = self.target - self.start_speed
        if len(self.samples) < 25 or abs(step) < 5 or self.target == 0:
            return
        speeds = [s for s, _ in self.samples]
        motors = [m for _, m in self.samples]
        sign = 1 if step > 0 else -1
        peak = max(speeds) if sign > 0 else min(speeds)
        over = round(max(0.0, (peak - self.target) * sign / abs(step) * 100))
        tail = speeds[-max(5, len(speeds) // 5):]
        final = statistics.mean(tail)
        reach = round((final - self.start_speed) / step * 100)
        err = round((self.target - final) * sign / abs(self.target) * 100)
        sat = round(sum(1 for m in motors if abs(m) >= 250) / len(motors) * 100)
        diffs = [abs(motors[i] - motors[i - 1]) for i in range(1, len(motors))]
        jitter = statistics.mean(diffs) if diffs else 0

        if self.verify:
            self.verify = False
            if over < OVERSHOOT_ALERT:
                diagnose("motor", "fixed", {"before": self.last_over, "after": over})
                self.flagged = False
            else:
                diagnose("motor", "not_fixed", {"after": over},
                         {"kp": round(settings["kp"] * 0.5, 3)})
            self.last_over = over
            return

        self.last_over = over
        if self.flagged:
            return
        issue, facts, fix = None, {}, None
        if over >= OVERSHOOT_ALERT:
            issue, facts, fix = "overshoot", {"over": over}, {"kp": round(settings["kp"] * 0.35, 3)}
        elif sat >= 60:
            issue, facts, fix = "saturated", {"sat": sat}, {"kp": round(settings["kp"] * 0.66, 3)}
        elif reach < 85:
            issue, facts, fix = "slow", {"reach": reach}, {"kp": round(settings["kp"] * 1.5, 3)}
        elif err >= 8:
            issue, facts, fix = "steady_error", {"err": err}, {"ki": round(max(settings["ki"] * 1.5, 0.5), 3)}
        elif jitter > 40 and settings["kd"] > 0:
            issue, facts, fix = "jitter", {}, {"kd": round(settings["kd"] * 0.5, 3)}
        if issue:
            self.flagged = True
            diagnose("motor", issue, facts, fix)


motor_watch = MotorWatcher()


def on_motor_line(parts):
    # T,time_ms,target,speed,motor,tilt
    try:
        t, target, speed, motor = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
        tilt = float(parts[5]) if len(parts) > 5 else 0.0
    except (ValueError, IndexError):
        return
    send({"lab": "motor", "t": t, "target": target, "speed": speed, "motor": motor, "tilt": tilt})
    motor_watch.add(target, speed, motor)


# ---------------------------------------------------------------- cooling analysis

class CoolingWatcher:
    """Fits Newton's law of cooling to good readings and flags readings that break from it."""

    def __init__(self):
        self.t0 = None
        self.temp0 = None
        self.k = None
        self.good = []             # (seconds, temp) used for the fit
        self.flagged = False

    def expected(self, t):
        if self.k is None:
            return None
        return ROOM_TEMP + (self.temp0 - ROOM_TEMP) * math.exp(-self.k * (t - self.t0))

    def fit(self):
        pts = [(t - self.t0, math.log(temp - ROOM_TEMP)) for t, temp in self.good if temp - ROOM_TEMP > 0.5]
        if len(pts) < 8:
            return
        # least squares slope through ln(T - room) against time, anchored at the first reading
        y0 = math.log(self.temp0 - ROOM_TEMP)
        num = sum(t * (y0 - y) for t, y in pts)
        den = sum(t * t for t, _ in pts)
        if den > 0 and num > 0:
            self.k = num / den

    def add(self, t, temp):
        if self.t0 is None or temp > (self.temp0 or 0) + 10:   # new run: hot water poured
            self.t0, self.temp0, self.k, self.good, self.flagged = t, temp, None, [], False
        exp = self.expected(t)
        if exp is not None and exp - temp > COOL_DROP_ALERT and not self.flagged:
            self.flagged = True
            diagnose("cooling", "probe_out", {"drop": round(exp - temp)})
        elif self.flagged and exp is not None and abs(exp - temp) < 1.5:
            self.flagged = False
            diagnose("cooling", "probe_back", {})
        if not self.flagged:
            self.good.append((t, temp))
            self.fit()
        return exp


cool_watch = CoolingWatcher()


def on_cooling_line(parts):
    # C,time_ms,temp_c
    try:
        t, temp = float(parts[1]), float(parts[2])
    except (ValueError, IndexError):
        return
    exp = cool_watch.add(t / 1000.0, temp)
    msg = {"lab": "cooling", "t": t, "temp": temp}
    if exp is not None:
        msg["expected"] = round(exp, 2)
    send(msg)


# ---------------------------------------------------------------- line routing

def on_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        if line:
            print("[board]", line)
        return
    parts = line.split(",")
    if parts[0] == "T":
        on_motor_line(parts)
    elif parts[0] == "C":
        on_cooling_line(parts)


# ---------------------------------------------------------------- real boards

class SerialLink:
    def __init__(self, port, baud=115200):
        import serial
        self.ser = serial.Serial(port, baud, timeout=1)
        self.lock = threading.Lock()
        threading.Thread(target=self.read_loop, daemon=True).start()
        print(f"Connected to {port}")

    def read_loop(self):
        while True:
            try:
                raw = self.ser.readline()
                if raw:
                    on_line(raw.decode(errors="ignore"))
            except Exception as e:
                print("Serial read error:", e)
                time.sleep(1)

    def write(self, text: str):
        with self.lock:
            self.ser.write((text.strip() + "\n").encode())


# ---------------------------------------------------------------- fake boards

class FakeMotor:
    """Behaves like the MinSeg speed sketch: same output lines, same K and S commands."""

    def __init__(self):
        self.kp, self.ki, self.kd = settings["kp"], settings["ki"], settings["kd"]
        self.target, self.speed, self.vel = 40.0, 0.0, 0.0
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.stepper, daemon=True).start()

    def write(self, text):
        p = text.strip().split(",")
        if p[0] == "K" and len(p) == 4:
            self.kp, self.ki, self.kd = float(p[1]), float(p[2]), float(p[3])
        elif p[0] == "S" and len(p) == 2:
            self.target = float(p[1])

    def stepper(self):
        while True:                      # flip the target every 4 seconds, like a lab exercise
            time.sleep(4)
            self.target = 100.0 if self.target == 40.0 else 40.0

    def run(self):
        t0 = time.time()
        while True:
            time.sleep(0.02)
            wn, zeta = 7.0, max(0.08, min(1.0, 1.0 / max(self.kp, 0.01)))
            self.vel += (wn * wn * (self.target - self.speed) - 2 * zeta * wn * self.vel) * 0.02
            self.speed += self.vel * 0.02
            sp = self.speed + random.uniform(-0.4, 0.4)
            motor = max(-255, min(255, sp * 2 + self.vel * 0.4))
            on_line(f"T,{int((time.time() - t0) * 1000)},{self.target:.1f},{sp:.1f},{motor:.0f},0")


class FakeCooling:
    """Behaves like the cooling probe sketch. Type 'out' or 'in' in the terminal to move the probe."""

    def __init__(self):
        self.out = False
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        t0, cur = time.time(), 80.0
        while True:
            time.sleep(1)
            t = time.time() - t0
            truth = ROOM_TEMP + (80 - ROOM_TEMP) * math.exp(-0.012 * t)
            cur += (ROOM_TEMP - cur) * 0.35 if self.out else (truth - cur) * 0.6
            on_line(f"C,{int(t * 1000)},{cur + random.uniform(-0.15, 0.15):.2f}")


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "settings", **settings}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "apply_fix" and isinstance(msg.get("fix"), dict):
                settings.update({k: float(v) for k, v in msg["fix"].items() if k in settings})
                if motor_link:
                    motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
                motor_watch.verify = True
                print("[fix applied]", settings)
                send({"type": "settings", **settings})
            elif msg.get("type") == "command" and motor_link:   # e.g. {"type":"command","text":"S,80"}
                motor_link.write(str(msg.get("text", "")))
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.get("/ready")
def ready():
    return {"ok": True}


@app.get("/")
def health():
    return {"ok": True, "settings": settings, "dashboards": len(clients)}


# ---------------------------------------------------------------- main

def main():
    global motor_link
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor-port", help="robot COM port, e.g. COM5 or /dev/ttyACM0")
    ap.add_argument("--cooling-port", help="cooling probe COM port, e.g. COM6")
    ap.add_argument("--fake", action="store_true", help="simulate both boards")
    ap.add_argument("--kp", type=float, default=settings["kp"])
    ap.add_argument("--ki", type=float, default=settings["ki"])
    ap.add_argument("--kd", type=float, default=settings["kd"])
    args = ap.parse_args()
    settings.update(kp=args.kp, ki=args.ki, kd=args.kd)

    cooling = None
    if args.fake:
        motor_link, cooling = FakeMotor(), FakeCooling()
        print("Fake boards running. Type 'bad' for a bad controller, 'out'/'in' to move the probe.")
    else:
        if args.motor_port:
            motor_link = SerialLink(args.motor_port)
        if args.cooling_port:
            SerialLink(args.cooling_port)

    def console():
        while True:
            cmd = input().strip()
            if cmd == "bad" and motor_link:
                settings.update(kp=6.0)
                motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
                motor_watch.flagged = False
                send({"type": "settings", **settings})
                print("Loaded a bad controller (kp=6)")
            elif cmd in ("out", "in") and cooling:
                cooling.out = cmd == "out"
            elif cmd and motor_link:
                motor_link.write(cmd)    # anything else goes straight to the robot, e.g. S,80
    threading.Thread(target=console, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
