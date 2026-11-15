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
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
import photon.ta_report as photon_link
from photon.ta_report import photon_ready, poll_ta, send_ta_report, send_ta_text
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

# Read secrets from backend/.env (never pushed to GitHub, see .gitignore)
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env, encoding="utf-8-sig"):
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

ROOM_TEMP = 22.0           # degrees C, set with --room-temp to the room's real temperature
COOL_DROP_ALERT = 6.0      # degrees below expected that counts as a mistake
OVERSHOOT_ALERT = 20.0     # percent overshoot that counts as a mistake (good settings on our MinSeg: ~2%)
STUCK_SECONDS = 120        # tell the TA when a mistake stays unfixed this long

# Photon (TA texting). Gissell's service listens here for events from Kelvin.
# Set NOTIFY_URL in backend/.env or with --notify-url. Leave it empty to turn TA texts off.
STATION = os.environ.get("STATION", "Station 1")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")

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
calib_link = None          # the robot running minseg_balance.ino (calibration lab)
dip_link = None            # the Uno running kelvin_dip.ino (time constant lab)
DEFAULT_LAB = "motor"      # the lab the dashboard opens on
settings = {"kp": 4.0, "ki": 10.0, "kd": 0.0}     # good settings for our MinSeg; --kp/--ki override


# ---------------------------------------------------------------- broadcast

said_log = []          # (time, text) of the last few things Kelvin said out loud


def strip_echo(question: str) -> str:
    """The mic sometimes hears Kelvin's own voice and sends it back as if the student said it.
    Cut out any run of 4+ words that matches something Kelvin said in the last 45 seconds."""
    import difflib
    norm = lambda t: re.sub(r"[^a-z0-9 ]", " ", t.lower().replace("°", " degrees ")).split()
    q_words = question.split()
    q_norm = [(" ".join(norm(w)) or "") for w in q_words]
    keep = [True] * len(q_words)
    for when, text in said_log:
        if time.time() - when > 45:
            continue
        said = norm(text)
        flat = [w.split()[0] if w else "" for w in q_norm]
        for blk in difflib.SequenceMatcher(None, flat, said, autojunk=False).get_matching_blocks():
            if blk.size >= 4:
                for i in range(blk.a, blk.a + blk.size):
                    keep[i] = False
    if all(keep):
        return question
    if keep.count(False) >= 0.4 * len(keep):
        return ""                    # mostly Kelvin's own words: ignore the whole thing
    return " ".join(w for w, k in zip(q_words, keep) if k)

def send(msg: dict):
    """Thread-safe: queue a JSON message to every connected dashboard."""
    if msg.get("type") in ("diagnosis", "answer") and msg.get("text"):
        said_log.append((time.time(), str(msg["text"])))
        del said_log[:-5]
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
                 "The second setting, ki, is too high for kp: it keeps pushing after the wheel reaches speed. "
                 "Lower ki from {old} to {new}.",
    "overshoot_kp": "The wheel overshot its target by {over} percent and kept oscillating. "
                    "The first setting, kp, is too high for this motor. Lower it to about a third.",
    "slow": "The wheel only reached {reach} percent of its target. The first setting, kp, is too low. Raise it by half.",
    "steady_error": "The wheel settled {err} percent below its target and stayed there. "
                    "Add some of the second setting, ki, to close the gap.",
    "saturated": "The motor was at full power {sat} percent of the time. "
                 "The settings are too aggressive, or the battery is low. Lower kp, and check the battery.",
    "jitter": "The motor power is jittering. The third setting, kd, is amplifying sensor noise. Lower it.",
    "fixed": "That worked. Overshoot went from {before} percent to {after} percent.",
    "fixed_gap": "That worked. The gap to the target went from {before} percent to {after} percent.",
    "fixed_reach": "That worked. The wheel now reaches {after} percent of its target, up from {before} percent.",
    "fixed_sat": "That worked. Full power went from {before} percent of the time to {after} percent.",
    "not_fixed": "Better, but still {after} percent overshoot. Let's cut {name} in half again.",
    "probe_out": "Your reading dropped {drop} degrees below where it should be. "
                 "The probe looks like it is out of the water.",
    "probe_fast": "The reading is falling {ratio} times faster than hot water can cool. "
                  "The probe looks like it is out of the water, or in something colder. Put it back in the cup.",
    "probe_high": "Your reading is {rise} degrees above where the cooling water should be. "
                  "Something warmer is touching the probe, or it moved into different water.",
    "probe_back": "The probe is back and the readings are heading back to the curve. I logged the mistake for the report.",
    "drag": "Something is dragging on the wheel. Speed fell {drop} percent below target while motor power went up "
            "{power} percent to fight it. That's a physical problem, not your settings, so don't change the gains. "
            "Check for anything touching the wheel.",
    "drag_load": "Something is loading the wheel. Speed held, but only because motor power jumped {power} percent. "
                 "That's a physical problem, not your settings, so don't change the gains. "
                 "Check for anything touching the wheel.",
    "drag_gone": "The drag is gone and the wheel is back on target. Your settings were fine the whole time.",
    # calibration lab (tilt sensor on the MinSeg, robot held in the student's hands)
    "cal_tilted": "That calibration is off. You held it {pitch} degrees {pdir}, and the board accepted it, "
                  "so now it thinks that lean is upright. Hold it straight and say calibrate.",
    "cal_sideways": "That calibration is off. It was tipped {roll} degrees sideways, and the board never checks sideways. "
                    "Stand it straight and say calibrate.",
    "cal_moved": "The board rejected that one because it moved: {how}. Hold it still and say calibrate.",
    "cal_rejected_tilt": "The board rejected that one. It was {pitch} degrees {pdir}, too far from upright. "
                         "Stand it straight and say calibrate.",
    "cal_sensor": "The board rejected that one. The sensor felt {g} times gravity, so it was bumped. "
                  "Hold it still and say calibrate.",
    "cal_ok": "Calibration is good, within {off} degrees of upright. Say arm to test the wheels.",
    "cal_fixed": "That worked. It was {before} degrees off, and now it's within {after}. Say arm to test the wheels.",
    # time constant lab (dip the probe into hot water for a set time; kelvin_dip.ino on the Uno)
    "dip_late": "You held it in for {dwell} seconds, not {target}. That puts your time constant {pct} percent off: "
                "{tau_assumed} seconds instead of {tau_actual}. {advice}",
    "dip_early": "You pulled it out after {dwell} seconds, not {target}. That puts your time constant {pct} percent off: "
                 "{tau_assumed} seconds instead of {tau_actual}. {advice}",
    "dip_timing": "That dip lasted {dwell} seconds, not {target}. {advice}",
    "dip_not_reset": "The probe hadn't cooled down. It started at {start} degrees, {above} above room temperature. "
                     "Cool it in the cold cup, then dip again.",
    "dip_need_water": "I need the water temperature first. Hold the probe in the hot water for ten seconds.",
    "dip_water": "Got it, the water is {water} degrees. Cool the probe, then dip for {target} seconds.",
    "dip_ok": "Good dip, {dwell} seconds. The time constant is {tau} seconds.",
    "dip_fixed": "That worked. You were {before} seconds off before, and now you're within {after}. "
                 "The time constant is {tau} seconds.",
    "wheels_ok": "The wheels drive toward the lean, so it can catch itself. Say stop when you're done.",
}
OK_ISSUES = ("fixed", "fixed_gap", "fixed_reach", "fixed_sat", "probe_back", "drag_gone",
             "cal_ok", "cal_fixed", "wheels_ok", "dip_ok", "dip_fixed", "dip_water")      # these turn the dashboard green
NOT_A_FIX = ("drag_gone", "cal_ok", "wheels_ok", "dip_ok", "dip_water")       # good news, but not a fix of an earlier mistake
CAL_ISSUES = ("cal_tilted", "cal_sideways", "cal_moved", "cal_rejected_tilt", "cal_sensor", "cal_ok", "cal_fixed", "wheels_ok",
              "dip_late", "dip_early", "dip_timing", "dip_not_reset", "dip_need_water", "dip_water", "dip_ok", "dip_fixed")   # spoken as written


# Gemini models to try, newest first. gemini-2.5-flash was shut down Oct 16, 2026.
GEMINI_GOOD = [None]
GEMINI_DEAD = set()
GEMINI_NO_LEVEL = set()


def gemini_text(data):
    parts = data["candidates"][0]["content"]["parts"]
    return " ".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()


def gemini_models():
    first = [GEMINI_GOOD[0]] if GEMINI_GOOD[0] else []
    wanted = [os.environ.get("GEMINI_MODEL")] if os.environ.get("GEMINI_MODEL") else []
    names = first + wanted + ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-flash-latest"]   # fastest first
    seen = []
    for n in names:
        if n and n not in seen and n not in GEMINI_DEAD:
            seen.append(n)
    return seen


def ask_llm(prompt: str, timeout: float = 7, max_tokens: int = 150, json_mode: bool = False):
    """Send a prompt to Gemini or Claude. Returns the reply text, or None on any problem."""
    try:
        if os.environ.get("GEMINI_API_KEY"):
            # Gemini 3.5 thinks at "medium" by default, which is too slow for live speech
            gen = {"thinkingConfig": {"thinkingLevel": "minimal"}}
            plain_gen = {}
            if json_mode:
                gen["responseMimeType"] = plain_gen["responseMimeType"] = "application/json"
            fast = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}).encode()
            plain = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                **({"generationConfig": plain_gen} if plain_gen else {})}).encode()
            deadline = time.time() + timeout
            for model in gemini_models():
                left = deadline - time.time()
                if left < 1.5:
                    break
                body = plain if model in GEMINI_NO_LEVEL else fast
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                       f"?key={os.environ['GEMINI_API_KEY']}")
                req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=min(left, max(3.0, timeout * 0.6))) as r:
                        data = json.load(r)
                except urllib.error.HTTPError as e:
                    if e.code in (429, 500, 502, 503, 504):    # overloaded: the next model may answer
                        print(f"Gemini {model} busy ({e.code}), trying the next model")
                        continue
                    if e.code == 404:                      # model retired or wrong name, try the next one
                        print(f"Gemini model {model} not found, trying the next one")
                        GEMINI_DEAD.add(model)
                        continue
                    if e.code == 400 and model not in GEMINI_NO_LEVEL:   # model doesn't accept thinkingLevel
                        GEMINI_NO_LEVEL.add(model)
                        req = urllib.request.Request(url, plain, {"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=timeout) as r:
                            data = json.load(r)
                        GEMINI_GOOD[0] = model
                        return gemini_text(data)
                    print("Gemini error:", e.code, e.read()[:300])
                    return None
                except (TimeoutError, OSError) as e:          # slow network or model: try the next one
                    print(f"Gemini {model} too slow ({e}), trying the next model")
                    continue
                GEMINI_GOOD[0] = model
                return gemini_text(data)
            return None
        if os.environ.get("ANTHROPIC_API_KEY"):
            model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
            body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", json.dumps(body).encode(),
                {"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            return data["content"][0]["text"].strip()
    except Exception as e:
        print("AI call failed:", e)
    return None


def ai_rewrite(issue: str, facts: dict, fallback: str) -> str:
    """Ask an LLM for a short spoken explanation. Returns the fallback on any problem."""
    prompt = (
        "You are Kelvin, a friendly lab partner for engineering students. In at most two short "
        "sentences that will be read out loud, explain this lab mistake and the fix in plain words. "
        f"Issue: {issue}. Measurements: {json.dumps(facts)}. Suggested fix already decided: {fallback}"
    )
    return ask_llm(prompt) or fallback


# what Kelvin knows right now, used to answer questions
recent = {"motor": None, "cooling": None, "calib": None, "dip": None, "events": []}


session = {"started": time.time(), "events": [], "mistakes": 0, "fixed": 0, "applied": 0}


def remember_event(lab: str, text: str):
    line = f"{time.strftime('%H:%M:%S')} [{lab}] {text}"
    if lab != "chat":                  # conversation goes in the session log only, so it can't push out readings
        recent["events"].append(line)
        del recent["events"][:-8]
    session["events"].append(line)
    del session["events"][:-200]


# ---------------------------------------------------------------- TA texting (Photon)
# Kelvin only texts the TA when it matters: a student is stuck, a change needs approval,
# a report was sent, or a problem the TA heard about got fixed. Not every diagnosis.

ta = {"open_since": None, "open_issue": "", "open_what": "", "stuck_sent": False, "failed_fixes": 0, "approval": None}


ta_thread = []        # the TA conversation, newest last, so a refreshed dashboard can show it


def ta_event(direction, text, mid, status, why=""):
    """Photon reports a text to or from the TA. Show it on the dashboard (silently) with its delivery status."""
    for e in ta_thread:
        if e["id"] == mid:
            e.update(status=status, why=why)
            break
    else:
        ta_thread.append({"id": mid, "dir": direction, "text": text, "status": status, "why": why,
                          "time": time.strftime("%I:%M %p").lstrip("0")})
        del ta_thread[:-40]
    send({"type": "ta_msg", **next(e for e in ta_thread if e["id"] == mid)})


photon_link.on_event = ta_event


def ta_enabled():
    """Can Kelvin text the TA? Yes if the Photon keys are in .env (or an outside service is set as NOTIFY_URL)."""
    return photon_ready() or bool(NOTIFY_URL)


def notify(event: str, text: str, **extra):
    """Text one event to the TA through Photon. Runs in the background, never blocks, never crashes."""
    body = {"event": event, "station": STATION, "text": text, **extra}
    print(f"[to TA] {event}: {text}")
    if photon_ready():
        def photon_send():
            try:
                print("[Photon]", send_ta_text(text))
            except Exception as e:
                print(f"[Photon] text to TA failed ({type(e).__name__}: {str(e)[:120]})")
        threading.Thread(target=photon_send, daemon=True).start()
        return
    if not NOTIFY_URL:
        return

    def post():
        try:
            req = urllib.request.Request(NOTIFY_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            print("Could not reach the Photon service:", e)
    threading.Thread(target=post, daemon=True).start()


def stuck_watchdog():
    """Text the TA once if a mistake has been open longer than STUCK_SECONDS."""
    while True:
        time.sleep(5)
        t0 = ta["open_since"]
        if t0 and not ta["stuck_sent"] and time.time() - t0 > STUCK_SECONDS:
            ta["stuck_sent"] = True
            mins = round((time.time() - t0) / 60, 1)
            notify("stuck", f"{STATION} has been stuck for {mins:g} minutes: {ta['open_what']}")


# ---------------------------------------------------------------- voice commands
# Kelvin can act, not just talk. Every action goes through do_action(), which checks the numbers
# before touching the robot, and Kelvin's spoken reply is written from what actually happened.

SAFE = {"kp": (0.01, 20.0), "ki": (0.0, 60.0), "kd": (0.0, 5.0)}      # Kelvin applies these on its own
HARD = {"kp": (0.001, 60.0), "ki": (0.0, 200.0), "kd": (0.0, 15.0)}    # past SAFE but inside HARD needs the TA's OK
pending_fix = {"motor": None, "cooling": None, "calib": None, "dip": None}   # the suggested fix waiting for "apply"
open_diag = {"motor": None, "cooling": None, "calib": None, "dip": None}     # the mistake on screen right now, resent when a dashboard (re)connects
undo_stack = []                                  # earlier settings, newest last
stepper = {"low": 30.0, "high": 80.0, "on": True, "seconds": 4.0}   # RPM; our wheel tops out ~130
MEASURE_WORDS = r"\b(half|third|quarter|double|twice|times|percent|more|less|lower|higher|bigger|smaller|bit)\b"


def fmt_fix(f: dict) -> str:
    return ", ".join(f"{k} {float(v):g}" for k, v in f.items())


def apply_settings(new: dict, why: str = "applied"):
    """Send new controller settings to the robot and start checking whether they worked."""
    before = {k: settings[k] for k in new if k in settings}
    undo_stack.append(dict(settings))
    del undo_stack[:-20]
    settings.update({k: float(v) for k, v in new.items() if k in settings})
    if motor_link:
        motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
    motor_watch.arm_verify()
    pending_fix["motor"] = None
    open_diag["motor"] = None                    # the dashboard now shows "testing fix", not the old mistake
    session["applied"] += 1
    remember_event("motor", f"student {why} {fmt_fix(new)}, settings now kp {settings['kp']:g} ki {settings['ki']:g} kd {settings['kd']:g}")
    print("[fix applied]", settings)
    send({"type": "settings", **settings})
    send({"type": "applied", "lab": "motor", "fix": new, "before": before})


def clean_fix(a: dict, limits: dict = SAFE):
    """Pull kp/ki/kd numbers out of an action. Returns (values, problem)."""
    vals = {}
    for k in ("kp", "ki", "kd"):
        if a.get(k) is None:
            continue
        try:
            v = round(float(a[k]), 3)
        except (TypeError, ValueError):
            return None, f"I couldn't understand the number for {k}."
        lo, hi = limits[k]
        if not lo <= v <= hi:
            return None, f"{k} {v:g} is outside the safe range of {lo:g} to {hi:g}, so I left it alone."
        vals[k] = v
    if not vals:
        return None, "I couldn't tell which setting you want to change."
    return vals, None


def ask_ta_approval(vals: dict) -> str:
    """A student asked for settings past the safe range. Hold them and text the TA."""
    ta["approval"] = vals
    remember_event("motor", f"student asked for {fmt_fix(vals)}, outside the safe range; Kelvin asked the TA")
    notify("approval", f"{STATION} wants to set {fmt_fix(vals)}, which is outside the safe range. "
                       "Reply OK to allow it or NO to block it.", fix=vals)
    return f"{fmt_fix(vals)} is outside the safe range, so I asked your TA. I'll apply it if they say okay."


def do_action(a: dict, lab: str, source: str = "student") -> str:
    """Carry out one action and return what Kelvin should say about it.
    source is "student" (voice or typing at the bench) or "ta" (texting through Photon)."""
    t = a.get("type")
    if t in ("approve", "deny"):
        vals = ta["approval"]
        if not vals:
            return "There's nothing waiting for your approval right now."
        ta["approval"] = None
        if t == "deny":
            remember_event("motor", f"TA blocked {fmt_fix(vals)}")
            send({"type": "answer", "question": "", "from": "ta", "text": f"Your TA said no to {fmt_fix(vals)}. Let's try something closer to the suggested fix."})
            return f"Blocked. {STATION} keeps its current settings."
        apply_settings(vals, "applied with TA approval")
        send({"type": "answer", "question": "", "from": "ta", "text": f"Your TA approved it. {fmt_fix(vals)} is on the robot now."})
        return f"Approved. {fmt_fix(vals)} is on the robot at {STATION}. I'll tell you if it causes trouble."
    if t == "apply_fix" and pending_fix["dip"] and (lab == "dip" or not (pending_fix["motor"] or pending_fix["calib"])):
        t = "cue_on"
    if t in ("cue_on", "cue_off"):
        if not dip_link:
            return "The temperature probe for the dip lab isn't connected."
        return dip_watch.set_cue(t == "cue_on")
    if t == "set_water":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the water temperature."
        if not 25 <= v <= 100:
            return f"{v:g} degrees doesn't sound like hot water, so I left it alone."
        return dip_watch.set_water(v, "student")
    if t == "set_dwell":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the time."
        if not 1 <= v <= 30:
            return f"{v:g} seconds is outside 1 to 30, so I left the dip time alone."
        return dip_watch.set_target(v)
    if t == "calibrate" or (t == "apply_fix" and pending_fix["calib"] and (lab == "calib" or not pending_fix["motor"])):
        if not calib_link:
            return "The tilt sensor isn't connected, so I can't calibrate."
        calib_link.write("c")
        calib_watch.started()
        remember_event("calib", "student asked Kelvin to calibrate")
        return "Calibrating. Hold still."
    if t == "arm":
        if not calib_link:
            return "The tilt sensor isn't connected."
        calib_link.write("a")
        calib_watch.armed_at = time.time()
        calib_watch.wheel_votes = 0
        remember_event("calib", "student armed the motor")
        return "Armed. Tilt it and watch the wheels."
    if t == "apply_fix":
        f = pending_fix["motor"]
        if not f:
            return "There's no fix waiting right now. Tell me a setting, like set kp to 1, and I'll line it up."
        apply_settings(f)
        return f"Done. {fmt_fix(f)} is on the robot. I'll watch the next run and tell you if it worked."
    if t == "set_fix":
        vals, problem = clean_fix(a, HARD if source == "ta" else SAFE)
        if problem:
            wide, _ = clean_fix(a, HARD)
            if wide and source == "student" and ta_enabled():
                return ask_ta_approval(wide)
            return problem
        if a.get("apply"):
            apply_settings(vals, "set")
            return f"Done. {fmt_fix(vals)} is on the robot. I'll watch the next run and tell you if it worked."
        pending_fix["motor"] = vals
        send({"type": "fix_update", "lab": "motor", "fix": vals})
        remember_event("motor", f"student changed the suggested fix to {fmt_fix(vals)}")
        return f"Okay, the suggested fix is now {fmt_fix(vals)}. Say apply it when you're ready."
    if t == "undo":
        if not undo_stack:
            return "There's nothing to undo yet."
        prev = undo_stack.pop()
        settings.update(prev)
        if motor_link:
            motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
        motor_watch.verify = False
        remember_event("motor", f"student undid the last change, settings back to kp {settings['kp']:g} ki {settings['ki']:g} kd {settings['kd']:g}")
        send({"type": "settings", **settings})
        send({"type": "undone", "lab": "motor"})
        return f"Undone. We're back to kp {settings['kp']:g}, ki {settings['ki']:g}, kd {settings['kd']:g}."
    if t == "switch_lab":
        want = str(a.get("lab", "")).lower()
        to = "cooling" if want.startswith("cool") else "calib" if want.startswith(("cal", "tilt", "bal")) else \
             "dip" if want.startswith(("dip", "time", "tau")) else "motor"
        send({"type": "action", "action": "switch_lab", "lab": to})
        return {"cooling": "Here's the cooling lab.", "calib": "Here's the calibration lab.",
                "dip": "Here's the time constant lab."}.get(to, "Here's the motor lab.")
    if t == "set_target":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the speed you want."
        if not 5 <= v <= 120:
            return f"{v:g} is outside the safe range of 5 to 120 RPM, so I left the speed alone."
        stepper["high"], stepper["low"], stepper["on"] = v, round(v * 0.4, 1), True
        remember_event("motor", f"student set the target speed to step between {stepper['low']:g} and {v:g}")
        return f"Done. The wheel will now step between {stepper['low']:g} and {v:g}."
    if t == "hold":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the speed you want."
        if not 5 <= v <= 120:
            return f"{v:g} is outside the safe range of 5 to 120 RPM, so I left the speed alone."
        stepper["high"] = stepper["low"] = v
        stepper["on"] = True
        if motor_link:
            motor_link.write(f"S,{v:g}")
        remember_event("motor", f"wheel set to hold a steady {v:g}")
        return f"Holding the wheel at a steady {v:g}. Say set the speed to step it up and down again."
    if t in ("start", "stop") and dip_link and not motor_link and not calib_link:
        return (f"There's no motor in this lab. Dip the probe for {dip_watch.target:g} seconds whenever you're ready, "
                "and I'll time it.")
    if t == "stop":
        stepper["on"] = False
        if calib_link:
            calib_link.write("x")
        if motor_link:
            motor_link.write("S,0")
        remember_event("motor", "student stopped the motor")
        return "Motor stopped. Say start the motor when you want it running again."
    if t == "start":
        stepper["on"] = True
        remember_event("motor", "student restarted the motor")
        return "Starting the motor again."
    if t == "report":
        to_ta = bool(a.get("to_ta")) or source == "ta"
        threading.Thread(target=send_report, args=(to_ta, source == "student"), daemon=True).start()
        if source == "ta":
            return "Writing the report now. It'll be my next text."
        if to_ta:
            return ("Here's your lab report. I'm texting a copy to your TA too." if ta_enabled()
                    else "Here's your lab report. TA texting isn't set up, so save it as a PDF and send it yourself.")
        return "Here's your lab report. You can save it as a PDF."
    return "I'm not able to do that one yet."


TA_WORDS = r"\b(t\.?\s?a\.?|tas|teacher|professor|instructor|teaching assistant)\b"


def quick_intent(q: str, lab: str = "", source: str = "student"):
    """Common commands we can catch instantly, without waiting for the AI."""
    s = q.lower()
    measured = re.search(r"\d", s) or re.search(MEASURE_WORDS, s)
    if source == "ta" and ta["approval"]:
        if re.search(r"^\W*(ok|okay|yes|yep|yeah|approve[d]?|allow(ed)?|go ahead|sure)\b", s):
            return {"type": "approve"}
        if re.search(r"^\W*(no|nope|deny|denied|block|don'?t|do not)\b", s):
            return {"type": "deny"}
    question = re.search(r"^\W*(why|what|how|is|was|did|does|should|explain|tell me)\b", s)
    fixish = re.search(r"\bfix\b|\bapply\b|\bgo ahead\b|\bdo it\b|\btry again\b|\bagain\b|\bredo\b|\bretry\b", s)
    if calib_link and not question:
        if re.search(r"\bdisarm\b", s):
            return {"type": "stop"}
        if re.search(r"\barm\b|\b(enable|turn on|start) (the )?(motors?|wheels?)\b", s):
            return {"type": "arm"}
        if re.search(r"\b(re-?)?calibrat\w*\b|\bcalibration\b", s) or \
                (fixish and lab == "calib" and not pending_fix["motor"] and not re.search(r"\b(report|ta|stop)\b", s)):
            return {"type": "calibrate"}
    if dip_link and not question:
        num = re.search(r"(\d+(\.\d+)?)", s)
        if re.search(r"\b(cue|beep|buzzer|timer)\b", s):
            return {"type": "cue_off" if re.search(r"\b(off|stop|disable|no)\b", s) else "cue_on"}
        if fixish and lab == "dip" and not pending_fix["motor"] and not re.search(r"\bagain\b|\bredo\b|\bretry\b", s):
            return {"type": "cue_on"}
        if num and re.search(r"\bwater\b", s) and re.search(r"\bdegrees?\b|\bis\b|\bset\b", s):
            return {"type": "set_water", "value": float(num.group(1))}
        if num and re.search(r"\bseconds?\b", s) and re.search(r"\b(dip|dwell|target|hold|time)\b", s) and lab == "dip":
            return {"type": "set_dwell", "value": float(num.group(1))}
    if re.search(r"\b(undo|revert|put it back|change it back)\b|\bgo back\b(?!\s+to\b)", s):
        return {"type": "undo"}
    report_words = r"\b(show|give|make|create|generate|send|open|write|get|pull up|need|want|see|text|email|share|forward)\b.*\b(report|summary|recap)\b|\bsummari[sz]e\b"
    if re.search(report_words, s):
        return {"type": "report", "to_ta": bool(re.search(TA_WORDS, s))}
    if re.search(r"\b(send|text|email|forward|share)\b.*\b(it|this|that|results?)\b", s) and re.search(TA_WORDS, s):
        return {"type": "report", "to_ta": True}     # "send this to the TA" with no report open yet
    hold = re.search(r"\b(hold|keep|lock)\b.*?\b(\d+(\.\d+)?)\b", s)
    if hold and not re.search(r"\bk ?[pid]\b", s):
        return {"type": "hold", "value": float(hold.group(2))}
    if re.search(r"^\W*stop\W*$|\bstop (the )?(motor|wheel|robot|it|everything)\b", s):
        return {"type": "stop"}
    if re.search(r"\b(start|resume|restart)\b.*\b(motor|wheel|robot|test|it)\b", s):
        return {"type": "start"}
    rel = re.search(r"\b(third|half|quarter|double|twice)\b", s)
    if rel and re.search(r"\b(do|make|try|use|go with|let's|lets|set|change|instead|cut|drop|lower|raise|apply)\b", s):
        waiting = pending_fix["motor"] or {}
        named = re.search(r"\bk ?([pid])\b", s)            # "half of ki" names the setting
        key = ("k" + named.group(1)) if named else (next(iter(waiting)) if waiting else "kp")
        base = waiting.get(key, settings[key])
        factor = {"third": 1 / 3, "half": 0.5, "quarter": 0.25, "double": 2, "twice": 2}[rel.group(1)]
        return {"type": "set_fix", key: round(base * factor, 3), "apply": bool(re.search(r"\bapply\b", s))}
    m = re.search(r"\b(switch|go|change|move|show|open|take me)\b.*\b(dip|time constant|cooling|cup|temperature|water|motor|wheel|robot|calibration|tilt|balance)\b", s)
    if m and not measured:
        to = "dip" if m.group(2) in ("dip", "time constant") else \
             "cooling" if m.group(2) in ("cooling", "cup", "temperature", "water") else \
             "calib" if m.group(2) in ("calibration", "tilt", "balance") else "motor"
        if to != lab:                     # already on that lab? then it's probably a question, let the AI answer
            return {"type": "switch_lab", "lab": to}
    if re.search(r"\b(apply|go ahead|send it|do it|use (it|that|the fix))\b", s) and not measured:
        return {"type": "apply_fix"}
    if source == "student" and (pending_fix.get(lab) or pending_fix.get("motor")) and not re.search(r"\d", s) \
            and not re.search(r"^\W*(why|what|how|when|where|who|is|does|should|explain)\b", s):
        # a fix is waiting, so "can you fix it", "fix the thing", "yeah let's do that" mean apply it
        if re.search(r"\bfix\b|\bcorrect (it|that|this)\b", s) or \
                re.search(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|sounds good|please)\b", s) or \
                re.search(r"\blet'?s (do|try|lower|raise|change) (it|that|this|the value|the setting)\b", s):
            return {"type": "apply_fix"}
    return None


def backup_intent(q: str):
    """If the AI is unreachable, still catch 'set kp to 1' and 'set the speed to 80'."""
    s = q.lower()
    if not re.search(r"\b(set|change|make|put|use|bump|raise|lower|drop|apply)\b", s):
        return None                       # no command word, so it's a question, not an order
    m = re.search(r"\b(kp|ki|kd|k p|k i|k d)\b\D{0,12}?(\d+(\.\d+)?)", s)
    if m:
        return {"type": "set_fix", m.group(1).replace(" ", ""): float(m.group(2)), "apply": bool(re.search(r"\bapply\b", s))}
    m = re.search(r"\b(speed|target)\b\D{0,12}?(\d+(\.\d+)?)", s)
    if m:
        return {"type": "set_target", "value": float(m.group(2))}
    return None


def parse_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


# The AI may only take an action when the message itself asks for it. This stops "how's it going?"
# from triggering a report just because a report was the last thing in Kelvin's memory.
ACTION_WORDS = {
    "report": r"report|summary|recap|summari",
    "stop": r"\bstop|halt|pause|turn (it )?off|kill",
    "start": r"start|resume|turn (it )?on|go again|run",
    "undo": r"undo|revert|back|previous|before",
    "apply_fix": r"apply|go ahead|do it|use (it|that|the fix)|send it|\byes\b|fix",
    "set_fix": r"\bk ?[pid]\b|gain|setting|third|half|quarter|double|twice|lower|raise|higher|less|more|reduce|increase|decrease|\d",
    "set_target": r"speed|target|rpm|\d",
    "hold": r"speed|hold|steady|keep|\d",
    "switch_lab": r"switch|cooling|cup|temperature|motor|wheel|show|open|lab|go to|calibrat|tilt",
    "calibrate": r"calibrat|again|redo|retry|try|fix|apply|go ahead|do it|\byes\b",
    "arm": r"\barm|enable|turn on|start|wheel|motor|test",
    "cue_on": r"cue|beep|buzz|timer|fix|apply|go ahead|do it|\byes\b|help",
    "cue_off": r"cue|beep|buzz|timer|off|stop",
    "set_water": r"water|\d",
    "set_dwell": r"second|dip|dwell|\d",
}


def status_line() -> str:
    if DEFAULT_LAB == "dip":
        d = dip_watch.last
        dip = (f"Last dip {d['dwell']:.1f} seconds against a {dip_watch.target:g} second target" if d else "No dips yet")
        m, f = session["mistakes"], session["fixed"]
        return f"{STATION}: {m} mistake{'' if m == 1 else 's'} caught, {f} fixed. {dip}. Timing cue {'on' if dip_watch.cue else 'off'}."
    if DEFAULT_LAB == "calib":
        c = calib_watch.last_cal
        cal = (f"Last calibration {c['verdict']}, {abs(c['pitch']):.1f} degrees forward/back and {abs(c['roll']):.1f} sideways"
               if c else "Not calibrated yet")
        m, f = session["mistakes"], session["fixed"]
        return f"{STATION}: {m} mistake{'' if m == 1 else 's'} caught, {f} fixed. {cal}."
    last = next((e for e in reversed(session["events"]) if "[chat]" not in e), "nothing has gone wrong yet")
    last = re.sub(r"^\d\d:\d\d:\d\d \[\w+\] ", "", last)
    m, f = session["mistakes"], session["fixed"]
    return (f"{STATION}: {m} mistake{'' if m == 1 else 's'} caught, {f} fixed. Settings kp "
            f"{settings['kp']:g}, ki {settings['ki']:g}, kd {settings['kd']:g}. Latest: {last}")


def handle_voice(question: str, lab: str, source: str = "student") -> str:
    """Someone talked to Kelvin. source "student" = voice or typing at the bench, "ta" = a text through Photon.
    Do what they asked, or answer the question. Both share one memory, so the TA can ask about the bench."""
    question = question.strip()[:300]
    if source == "student":
        cleaned = strip_echo(question)
        if cleaned != question:
            print(f"[echo] heard Kelvin's own voice in {question!r}; kept {cleaned!r}")
            question = cleaned.strip()
            if len(question.split()) < 2:
                return ""
    if not question:
        return ""
    lab = lab or DEFAULT_LAB
    action = quick_intent(question, lab, source)
    say = None
    if not action:
        who = "the TA, texting from their phone" if source == "ta" else "the student at the bench, speaking out loud"
        context = {
            "station": STATION,
            "lab_on_screen": lab,
            "controller_settings_on_robot": settings,
            "suggested_fix_waiting": pending_fix["motor"],
            "change_waiting_for_ta_approval": ta["approval"],
            "target_speed": ([stepper["low"], stepper["high"]] if stepper["low"] != stepper["high"] else stepper["high"])
                            if stepper["on"] else "motor stopped",
            "motor_latest": recent["motor"],
            "cooling_latest": recent["cooling"],
            "calibration_latest": recent["calib"],
            "time_constant_lab_latest": recent["dip"],
            "minutes_into_session": round((time.time() - session["started"]) / 60, 1),
            "mistakes_caught": session["mistakes"], "fixes_that_worked": session["fixed"],
            "session_log": (session["events"][-30:] if source == "ta" else recent["events"]) or ["nothing wrong yet"],
        }
        prompt = (
            "You are Kelvin, a friendly lab partner watching a student's experiment through live sensor data. "
            "Labs: 'motor' is a robot wheel whose speed is held at a target by the student's controller "
            "(settings kp, ki, kd). 'cooling' is a cup of hot water with a temperature probe; 'expected' is what "
            "Newton's law of cooling predicts. 'calib' is the calibration lab: the student holds a balancing robot "
            "and calibrates its tilt sensor so it learns which way is straight up; pitch is forward/back tilt from "
            "gravity (positive = toward the batteries), roll is sideways tilt; a good calibration is within 4 degrees "
            "forward/back and 5 sideways and held still. 'dip' is the time constant lab: the student dips a room-temperature probe "
            "into hot water for a target number of seconds, reads the temperature, and computes the probe's time "
            "constant tau = -t / ln(1 - theta), where theta is how far the reading got toward the water temperature; "
            "holding it in too long or too short while using the target time in the formula gives a wrong tau. "
            "You measure the water temperature yourself when the student holds the probe in the water for 15 seconds, "
            "so never ask them to type it. There is no motor in the dip lab. The session_log is everything that happened, including what the "
            "student said to you out loud and what the TA texted.\n"
            f"You are talking to {who}.\n"
            "Decide whether they want you to DO something. You can take at most one of these actions:\n"
            '- {"type":"apply_fix"}: send the suggested fix that is waiting to the robot.\n'
            '- {"type":"set_fix","kp":number,"ki":number,"kd":number,"apply":bool}: change the suggested fix. '
            "Include only the settings that change. Relative requests like 'a third of that', 'half', 'a bit lower' "
            "are relative to the suggested fix waiting if there is one, otherwise to the settings on the robot; "
            "work out the actual number. Set apply to true only if they also asked to apply or send it now.\n"
            '- {"type":"undo"}: go back to the previous settings.\n'
            '- {"type":"switch_lab","lab":"motor", "cooling" or "calib"}: show another lab on the bench screen.\n'
            '- {"type":"calibrate"}: calibrate the tilt sensor (the student must hold the robot straight and still).\n'
            '- {"type":"arm"}: turn on the balancing motor so the wheels react to tilt.\n'
            '- {"type":"cue_on"} or {"type":"cue_off"}: turn the dip lab timing beep on or off.\n'
            '- {"type":"set_water","value":number}: set the hot water temperature for the dip lab.\n'
            '- {"type":"set_dwell","value":number}: set the target dip time in seconds.\n'
            '- {"type":"set_target","value":number}: make the wheel step up and down to this speed.\n'
            '- {"type":"hold","value":number}: hold the wheel at one steady speed.\n'
            '- {"type":"stop"} or {"type":"start"}: stop or restart the motor.\n'
            '- {"type":"report","to_ta":bool}: write a summary report of this session; to_ta true texts it to the TA.\n'
            "Only take an action when THIS message clearly asks for it. Greetings, 'ping', and questions like "
            "'how is it going' are questions, so action is null. Things already asked for in the session_log are done; "
            "never repeat them.\n"
            "If they are asking a question or chatting, action is null and you answer in say: at most three "
            "short sentences, plain words, no markdown, use the real numbers from the context when they help. "
            + ("Your answer is sent as a text message. " if source == "ta" else "Your answer is read out loud. ")
            + "You cannot save files, send email, close the dashboard, or do anything not listed above, so never claim to. "
            "If you don't know, say so.\n"
            'Reply with JSON only: {"say": "...", "action": null or one action object}\n'
            f"Live context: {json.dumps(context)}\n"
            f"{'TA' if source == 'ta' else 'Student'} says: {question}"
        )
        reply = parse_json(ask_llm(prompt, timeout=12, max_tokens=250, json_mode=True))
        if reply:
            action = reply.get("action") if isinstance(reply.get("action"), dict) else None
            say = str(reply.get("say") or "").strip() or None
            words = ACTION_WORDS.get(str((action or {}).get("type")))
            if action and words and not re.search(words, question.lower()):
                print(f"[guard] ignored {action} for {question!r}: the message didn't ask for it")
                action, say = None, status_line() if source == "ta" else "Here's where things stand. " + status_line()
        else:
            action = backup_intent(question)
            if not action and source == "ta":           # no AI: still give the TA a useful status text
                say = status_line()
            elif not action:
                waiting = pending_fix.get(lab)
                if waiting:
                    say = ("My connection is slow, so I'll keep it short. The suggested fix is "
                           + ", ".join(f"{k} to {v:g}" for k, v in waiting.items())
                           + ". Say apply it when you're ready.")
                else:
                    say = ("My connection is slow, so I'll keep it short. " + status_line()
                           + " You can still ask me to apply a fix, undo, switch labs, change the speed, or show the report.")
    text = do_action(action, lab, source) if action else (say or "Sorry, I didn't catch that.")
    print(f"[{source}] {question} -> action={action} -> {text}")
    remember_event("chat", f"{'TA texted' if source == 'ta' else 'student said'}: {question!r}. Kelvin: {text}")
    if source == "student":
        send({"type": "answer", "question": question, "text": text})
    elif action and action.get("type") not in ("approve", "deny", "report"):
        send({"type": "answer", "question": question, "from": "ta", "text": text})   # the TA changed the bench
    return text


def build_report():
    """Build a short summary of the session and send it to the dashboard."""
    mins = max(1, round((time.time() - session["started"]) / 60))
    facts = {"minutes": mins, "mistakes_caught": session["mistakes"], "fixes_that_worked": session["fixed"],
             "changes_applied": session["applied"], "settings_now": settings, "log": session["events"][-40:]}
    prompt = (
        "You are Kelvin, a lab partner. Write a short lab report summary for the student in 3 to 5 plain "
        "sentences: what went wrong, what was changed, whether it worked, and one tip for next time. "
        "Use the real numbers from the log. No markdown, no headings, no lists. Only describe what is in "
        f"the log.\nSession: {json.dumps(facts)}"
    )
    summary = ask_llm(prompt, timeout=15, max_tokens=300)
    if not summary:
        m, f = session["mistakes"], session["fixed"]
        summary = (f"In {mins} minute{'s' if mins != 1 else ''}, Kelvin caught {m} mistake{'s' if m != 1 else ''} "
                   f"and confirmed {f} fix{'es' if f != 1 else ''} that worked. The controller settings are now "
                   f"kp {settings['kp']:g}, ki {settings['ki']:g}, kd {settings['kd']:g}.")
    return {
        "type": "report",
        "summary": summary,
        "minutes": mins,
        "settings": settings,
        "mistakes": session["mistakes"],
        "fixed": session["fixed"],
    }


def send_report(to_ta: bool = False, show: bool = True):
    report = build_report()
    if show:
        send(report)
    if to_ta:
        notify(
            "report",
            f"Lab report from {STATION}: {report['summary']}",
            minutes=report["minutes"],
            mistakes=report["mistakes"],
            fixed=report["fixed"],
            settings=report["settings"],
        )
        remember_event("chat", "the lab report was requested for the TA")


def run_stepper():
    """Step the target speed up and down like a lab exercise, so Kelvin can judge each step."""
    high = True
    while True:
        time.sleep(stepper["seconds"])
        if stepper["on"] and motor_link:
            motor_link.write(f"S,{stepper['high'] if high else stepper['low']:g}")
            high = not high


def diagnose(lab: str, issue: str, facts: dict, fix: dict | None = None):
    """Send a diagnosis. The AI call runs in a background thread so nothing blocks."""
    text = TEMPLATES[issue].format(**facts)
    good = issue in OK_ISSUES
    if good:
        if issue not in NOT_A_FIX:                     # drag was physical, so any gain fix still stands
            pending_fix[lab] = None
            session["fixed"] += 1
        elif lab in ("calib", "dip") and issue != "dip_water":
            pending_fix[lab] = None
        if ta["open_since"] and (issue != "drag_gone" or ta["open_issue"] in ("drag", "drag_load")):
            if ta["stuck_sent"]:
                notify("resolved", f"{STATION} is back on track. {text}")
            ta.update(open_since=None, open_issue="", open_what="", stuck_sent=False, failed_fixes=0)
    else:
        session["mistakes"] += 1
        if fix:
            pending_fix[lab] = fix
        if not ta["open_since"]:
            ta.update(open_since=time.time(), open_issue=issue, open_what=text)
        if issue == "not_fixed":
            ta["failed_fixes"] += 1
            if ta["failed_fixes"] >= 2 and not ta["stuck_sent"]:
                ta["stuck_sent"] = True
                notify("stuck", f"{STATION} has tried {ta['failed_fixes']} fixes and the wheel is still "
                                f"overshooting by {facts.get('after')} percent. They may need you.")

    def worker():
        # calibration lines are short and exact, so they're spoken as written, with no wait for the AI
        final = ai_rewrite(issue, facts, text) if not good and issue not in CAL_ISSUES else text
        msg = {"type": "diagnosis", "lab": lab, "issue": "ok" if good else issue, "text": final}
        if fix:
            msg["fix"] = fix
        print(f"[diagnosis] {lab} {issue}: {final}  fix={fix}")
        open_diag[lab] = None if good else msg
        remember_event(lab, final + (" Suggested fix: " + ", ".join(f"{k} to {v}" for k, v in fix.items()) + "." if fix else ""))
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
        self.skip_next = False     # ignore the step that was already running when a fix was applied
        self.last_over = None
        self.last = {}             # the last step's numbers: over, err, reach, sat
        self.fixing = None         # the issue the current fix is meant to solve
        # drag detection: once the wheel has settled at a steady target, watch for something slowing it down
        self.settle_count = 0
        self.settled = False
        self.base_motor = 0.0
        self.drag_count = 0
        self.in_drag = False
        self.recover_count = 0
        self.disturbed = False     # this step had drag in it, so don't judge the student's settings on it
        self.paused_until = 0.0    # ignore the board while it restarts

    def arm_verify(self):
        self.verify = True
        self.skip_next = True

    def add(self, target, speed, motor):
        if time.time() < self.paused_until:
            self.target, self.samples = None, []
            self.settle_count, self.settled, self.drag_count, self.in_drag = 0, False, 0, False
            return
        if self.target is None:
            self.target = target
        if target != self.target:
            self.finish()
            self.start_speed = speed
            self.target = target
            self.samples = []
            self.settle_count, self.settled, self.drag_count = 0, False, 0
            self.disturbed = self.in_drag
        self.samples.append((speed, motor))
        self.watch_drag(speed, motor)

    def watch_drag(self, speed, motor):
        if abs(self.target) < 5:
            return
        err = (self.target - speed) / abs(self.target) * (1 if self.target > 0 else -1)   # + means too slow
        if self.in_drag:
            easy = abs(motor) - self.base_motor < max(20.0, 0.10 * self.base_motor)
            self.recover_count = self.recover_count + 1 if abs(err) < 0.08 and easy else 0
            if self.recover_count >= 25:                 # back on target for half a second
                self.in_drag = False
                self.settled, self.settle_count = True, 25
                diagnose("motor", "drag_gone", {})
            return
        if not self.settled:
            self.settle_count = self.settle_count + 1 if abs(err) < 0.10 else 0
            if self.settle_count >= 25:                  # steady for half a second
                self.settled = True
                self.base_motor = statistics.mean(abs(m) for _, m in self.samples[-25:])
            return
        rise = abs(motor) - self.base_motor
        loaded = rise >= max(40.0, 0.20 * self.base_motor)   # holding speed, but only by pushing 20%+ harder
        if (err > 0.20 or loaded) and rise >= max(15.0, 0.15 * self.base_motor):
            self.drag_count += 1
        else:
            self.drag_count = 0
            if abs(err) < 0.05:                          # slowly track the normal motor power
                self.base_motor = 0.98 * self.base_motor + 0.02 * abs(motor)
        if self.drag_count >= 15:                        # slowed and fighting for 0.3 seconds
            self.in_drag, self.disturbed, self.recover_count = True, True, 0
            power = round(rise / max(self.base_motor, 1.0) * 100)
            if err > 0.10:
                diagnose("motor", "drag", {"drop": round(err * 100), "power": power})
            else:
                diagnose("motor", "drag_load", {"power": power})

    def finish(self):
        if self.skip_next:
            self.skip_next = False
            return
        if self.disturbed:                               # drag made this step meaningless for tuning
            return
        step = self.target - self.start_speed
        if len(self.samples) < 25 or abs(step) < 5 or self.target == 0:
            return
        if len(self.samples) < 0.75 * stepper["seconds"] * 50:
            return                                       # cut short (stopped, or the speed was changed): not a fair test
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

        if self.verify and sign < 0:
            return                                       # this motor coasts down without overshoot; judge the next speed-up
        if self.verify:
            self.verify = False
            before, fixing = self.last, self.fixing or "overshoot"
            self.last = {"over": over, "err": err, "reach": reach, "sat": sat}
            self.last_over = over
            if fixing == "steady_error" and over < OVERSHOOT_ALERT:
                if err < 8:
                    diagnose("motor", "fixed_gap", {"before": before.get("err", err), "after": max(err, 0)})
                    self.flagged, self.fixing = False, None
                    return
            elif fixing == "slow" and over < OVERSHOOT_ALERT:
                if reach >= 85:
                    diagnose("motor", "fixed_reach", {"before": before.get("reach", reach), "after": reach})
                    self.flagged, self.fixing = False, None
                    return
            elif fixing == "saturated" and over < OVERSHOOT_ALERT:
                if sat < 60:
                    diagnose("motor", "fixed_sat", {"before": before.get("sat", sat), "after": sat})
                    self.flagged, self.fixing = False, None
                    return
            elif over < OVERSHOOT_ALERT:
                diagnose("motor", "fixed", {"before": before.get("over", over), "after": over})
                self.flagged, self.fixing = False, None
                return
            elif fixing in ("overshoot", "overshoot_kp", "not_fixed"):
                key = "ki" if settings["ki"] >= 1 else "kp"
                diagnose("motor", "not_fixed", {"after": over, "name": key},
                         {key: round(settings[key] * 0.5, 3)})
                self.fixing = "not_fixed"
                return
            self.flagged = False           # a different problem now: judge this step from scratch below

        self.last = {"over": over, "err": err, "reach": reach, "sat": sat}
        self.last_over = over
        if self.flagged:
            return
        issue, facts, fix = None, {}, None
        if over >= OVERSHOOT_ALERT:
            if settings["ki"] >= 1:     # with integral action, overshoot on this motor comes from ki (we measured it)
                # Measured on our MinSeg (Nov 14): with kp 1, ki 3 still overshot 11-21%, ki 1 overshot 1-6% but
                # settled short, ki 1.5 overshot ~9%. So ki at about 1.5x kp is the one-step fix.
                new_ki = round(max(0.5, min(settings["ki"] / 3, settings["kp"] * 1.5)), 2)
                issue, facts, fix = "overshoot", {"over": over, "old": f"{settings['ki']:g}", "new": f"{new_ki:g}"}, {"ki": new_ki}
            else:
                issue, facts, fix = "overshoot_kp", {"over": over}, {"kp": round(settings["kp"] * 0.35, 3)}
        elif sat >= 60:
            issue, facts, fix = "saturated", {"sat": sat}, {"kp": round(settings["kp"] * 0.66, 3)}
        elif reach < 85:
            issue, facts, fix = "slow", {"reach": reach}, {"kp": round(settings["kp"] * 1.5, 3)}
        elif err >= 8:
            issue, facts, fix = "steady_error", {"err": err}, {"ki": round(max(settings["ki"] * 1.5, 0.5), 3)}
        elif jitter > 40 and settings["kd"] > 0:
            issue, facts, fix = "jitter", {}, {"kd": round(settings["kd"] * 0.5, 3)}
        if issue:
            self.flagged, self.fixing = True, issue
            diagnose("motor", issue, facts, fix)


motor_watch = MotorWatcher()


MOTOR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                         time.strftime("motor_%m%d_%H%M%S.csv"))
_motor_log = {"f": None, "last_t": None}


def log_motor(t, target, speed, motor):
    try:                                   # every reading goes in backend/logs/, for tuning
        if _motor_log["f"] is None:
            os.makedirs(os.path.dirname(MOTOR_LOG), exist_ok=True)
            _motor_log["f"] = open(MOTOR_LOG, "a", buffering=1)
            _motor_log["f"].write("time_ms,target,speed,pwm,kp,ki,kd,flag\n")
        _motor_log["f"].write(f"{t:.0f},{target:g},{speed:g},{motor:g},{settings['kp']:g},{settings['ki']:g},"
                              f"{settings['kd']:g},{open_diag['motor']['issue'] if open_diag.get('motor') else ''}\n")
    except OSError:
        pass


def on_motor_line(parts):
    # T,time_ms,target,speed,motor,tilt
    try:
        t, target, speed, motor = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
        tilt = float(parts[5]) if len(parts) > 5 else 0.0
    except (ValueError, IndexError):
        return
    if _motor_log["last_t"] is not None and t < _motor_log["last_t"] - 1000:
        motor_watch.paused_until = time.time() + 1.5      # the board restarted; don't judge the restart
    _motor_log["last_t"] = t
    log_motor(t, target, speed, motor)
    send({"lab": "motor", "t": t, "target": target, "speed": speed, "motor": motor, "tilt": tilt})
    recent["motor"] = {"target": target, "speed": round(speed, 1), "motor_power": motor}
    motor_watch.add(target, speed, motor)


# ---------------------------------------------------------------- cooling analysis

class CoolingWatcher:
    """Fits Newton's law of cooling to the readings and flags readings that break from it.

    Two checks, so a mistake is caught in seconds instead of half a minute:
      rate:   the reading falls far faster than hot water can cool (probe pulled into air, or into colder water)
      offset: the reading sits well below or above the curve physics predicts
    Recovery is quick too: as soon as the reading climbs back toward the curve, the probe is back."""

    MIN_ABOVE_ROOM = 5.0      # below this the water is too close to room temperature to tell anything
    OFFSET_ALERT = 3.0        # degrees off the predicted curve
    JUMP = 0.06               # degrees per second faster than the probe was cooling a moment ago...
    RATE_FACTOR = 3.0         # ...and at least this many times faster

    def __init__(self):
        self.reset(None, None)

    def reset(self, t, temp):
        self.t0, self.temp0, self.k = t, temp, None
        self.good = []             # (seconds, temp) used for the fit
        self.recent = []           # last few (seconds, temp), for the rate checks
        self.flagged = None        # None, "low" or "high"
        self.fast_count = 0
        self.rise_count = 0
        self.worst_gap = 0.0
        self.settling_until = None  # after the probe goes back in, it lags below the curve for a while; don't nag

    def expected(self, t):
        if self.k is None:
            return None
        return ROOM_TEMP + (self.temp0 - ROOM_TEMP) * math.exp(-self.k * (t - self.t0))

    def fit(self):
        pts = [(t - self.t0, math.log(temp - ROOM_TEMP)) for t, temp in self.good if temp - ROOM_TEMP > 0.5]
        if len(pts) < 8 or self.temp0 - ROOM_TEMP <= 0.5:
            return
        # least squares slope through ln(T - room) against time, anchored at the run's starting reading
        y0 = math.log(self.temp0 - ROOM_TEMP)
        num = sum(t * (y0 - y) for t, y in pts)
        den = sum(t * t for t, _ in pts)
        if den > 0:
            raw = num / den
            # Real cups of water cool slowly (k well under 0.03 per second, even the fast fake cup). Anything faster means the probe
            # wasn't sitting in the water for these readings, so don't trust a curve from them yet.
            self.k = max(raw, 0.0003) if raw < 0.03 else None

    def baseline_rate(self, t):
        """How fast the reading was changing 6 to 20 seconds ago (degrees per second), or None if unknown."""
        pts = [(pt, v) for pt, v in self.recent if t - 20 <= pt <= t - 6]
        if len(pts) < 8 or pts[0][0] < self.t0:
            return None
        n = len(pts)
        mt = sum(p for p, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((p - mt) ** 2 for p, _ in pts)
        return sum((p - mt) * (v - mv) for p, v in pts) / den if den else None

    def slope(self, n):
        """Degrees per second over the last n readings (least squares)."""
        pts = self.recent[-n:]
        if len(pts) < n:
            return None
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        return sum((t - mt) * (v - mv) for t, v in pts) / den if den else None

    def add(self, t, temp):
        self.recent.append((t, temp))
        del self.recent[:-40]

        # A run starts at the hottest reading. While the probe is still heating up (just dropped in hot water,
        # or fresh water poured), keep moving the start forward so the cooling fit only sees cooling.
        if self.t0 is None or (not self.flagged and temp > self.temp0 + 0.5) or temp > self.temp0 + 3.0:
            if self.flagged:
                diagnose("cooling", "probe_back", {})
            recent = self.recent
            self.reset(t, temp)
            self.recent = recent
            return None

        exp = self.expected(t)
        hot_enough = self.temp0 - ROOM_TEMP >= self.MIN_ABOVE_ROOM
        s = self.slope(4)

        settling = self.settling_until is not None
        if settling and exp is not None and abs(exp - temp) < 1.5:
            self.settling_until, settling = None, False           # back on the curve
        elif settling and (t >= self.settling_until or (s is not None and abs(s) < 0.08 and t >= self.settling_until - 35)):
            # The probe has warmed back up and leveled off, but it's not quite on the old curve (the water cooled
            # while the probe was out). Start a fresh curve from here, keeping the cooling rate we already learned.
            self.t0, self.temp0, self.good = t, temp, []
            self.settling_until, settling = None, False
            exp = self.expected(t)

        if not self.flagged and exp is None and s is not None and abs(s) > 0.3:
            # No curve yet and the reading is changing fast: the probe isn't sitting in the water (pulled out early,
            # or still warming up). Throw away what we collected and start the curve once it's calm.
            self.t0, self.temp0, self.good = t, temp, []
            return None

        if not self.flagged:
            if exp is not None and hot_enough:
                gap = exp - temp                                  # + means colder than physics says
                # Compare the probe with itself: how fast was it cooling 6 to 20 seconds ago, while it sat in the
                # water? A probe pulled into air suddenly cools several times faster. This works for warm and very
                # hot water alike, because very hot water cools fast on its own and that's the baseline.
                base = self.baseline_rate(t)
                s5 = self.slope(5)
                too_fast = (base is not None and s5 is not None
                            and base <= 0.01                      # it was cooling or steady, not still warming up
                            and s5 < base - self.JUMP and s5 < self.RATE_FACTOR * min(base, -0.005))
                self.fast_count = self.fast_count + 1 if too_fast and gap > 0.3 else 0
                if self.fast_count >= 3:
                    self.flag("low", t, exp, temp, "probe_fast",
                              {"ratio": max(3, min(50, round(s5 / min(base, -0.005)))), "drop": round(gap, 1)})
                elif settling:
                    pass                                          # still warming back up after going back in
                elif gap >= self.OFFSET_ALERT:
                    self.flag("low", t, exp, temp, "probe_out", {"drop": round(gap)})
                elif -gap >= self.OFFSET_ALERT and s is not None and s < 0.1:   # only once it has stopped climbing
                    self.flag("high", t, exp, temp, "probe_high", {"rise": round(-gap)})
            if not self.flagged and not settling and (exp is None or abs(exp - temp) < 1.5):
                self.good.append((t, temp))
                self.fit()
            return exp

        # flagged: watch for the probe going back in
        if exp is None:
            return exp
        gap = abs(exp - temp)
        self.worst_gap = max(self.worst_gap, gap)
        heading_back = s is not None and ((self.flagged == "low" and s > 0.15) or (self.flagged == "high" and s < -0.15))
        self.rise_count = self.rise_count + 1 if heading_back else 0
        water_rate = (self.k or 0.001) * max(exp - ROOM_TEMP, 0.1)   # how fast the water itself cools right now
        still_falling = s is not None and s < -max(0.04, 2 * water_rate)
        back_on_curve = gap < 1.5 and not still_falling
        if back_on_curve or (self.rise_count >= 2 and gap < 0.6 * self.worst_gap):
            self.flagged = None
            self.fast_count = self.rise_count = 0
            self.settling_until = None if back_on_curve else t + 60
            diagnose("cooling", "probe_back", {})
        return exp

    def flag(self, side, t, exp, temp, issue, facts):
        self.flagged = side
        self.worst_gap = abs(exp - temp)
        self.fast_count = self.rise_count = 0
        diagnose("cooling", issue, facts)


cool_watch = CoolingWatcher()


COOL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                        time.strftime("cooling_%m%d_%H%M%S.csv"))


def on_cooling_line(parts):
    # C,time_ms,temp_c
    try:
        t, temp = float(parts[1]), float(parts[2])
    except (ValueError, IndexError):
        return
    exp = cool_watch.add(t / 1000.0, temp)
    try:                                   # every reading goes in backend/logs/, for tuning and the report
        new = not os.path.exists(COOL_LOG)
        os.makedirs(os.path.dirname(COOL_LOG), exist_ok=True)
        with open(COOL_LOG, "a") as f:
            if new:
                f.write("time_s,temp_c,expected_c,flag\n")
            f.write(f"{t / 1000:.1f},{temp:.2f},{'' if exp is None else round(exp, 2)},{cool_watch.flagged or ''}\n")
    except OSError:
        pass
    msg = {"lab": "cooling", "t": t, "temp": temp}
    if exp is not None:
        msg["expected"] = round(exp, 2)
    send(msg)
    recent["cooling"] = {"temp_c": temp, "expected_c": msg.get("expected"), "seconds": round(t / 1000)}


# ---------------------------------------------------------------- calibration lab

CAL_PITCH_OK = 7.0     # degrees forward/back from straight up that still counts as a good calibration
CAL_ROLL_OK = 8.0      # degrees sideways
CAL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", time.strftime("calib_%m%d_%H%M%S.csv"))


class CalibWatcher:
    """The robot runs minseg_balance.ino. It streams its tilt (B lines) and reports every calibration (CAL lines).
    The board's own check only rejects a calibration past 15 degrees forward/back and never looks sideways,
    so Kelvin judges every calibration again with tighter limits and says what to do."""

    def __init__(self):
        self.last_cal = None          # the latest calibration and Kelvin's verdict
        self.bad_before = None        # how far off the last bad calibration was, for "that worked"
        self.calibrating_since = 0.0
        self.armed_at = 0.0
        self.wheel_votes = 0          # samples where the wheels pushed toward the lean, since arming
        self.wheel_said = False
        self.armed = False
        self.coached = False          # said "that's straight now" once after a bad calibration
        self.straight_since = 0.0

    def started(self):
        self.calibrating_since = time.time()
        self.coached = True           # no coaching while it's calibrating
        send({"type": "calibrating", "lab": "calib"})

    def on_status(self, parts):
        # B,time_ms,pitch,roll,angle,rate,pwm,armed,wheel_dps,calibrated
        try:
            t = float(parts[1])
            pitch, roll, angle, rate = (float(x) for x in parts[2:6])
            pwm, armed, wheel, cal = float(parts[6]), parts[7].strip() == "1", float(parts[8]), parts[9].strip() == "1"
        except (ValueError, IndexError):
            return
        if armed and not self.armed:
            self.wheel_votes, self.wheel_said = 0, False
        self.armed = armed
        offset = self.last_cal["pitch"] if (cal and self.last_cal) else None
        send({"lab": "calib", "ok_pitch": CAL_PITCH_OK, "ok_roll": CAL_ROLL_OK, "t": t, "pitch": pitch, "roll": roll, "angle": angle, "rate": rate, "pwm": pwm,
              "armed": armed, "wheel": wheel, "calibrated": cal, "offset": offset})
        recent["calib"] = {"forward_back_tilt_deg": pitch, "sideways_tilt_deg": roll, "calibrated": cal,
                           "motor_armed": armed, "last_calibration": self.last_cal}

        # Armed and tilted: do the wheels drive toward the lean? (positive wheel speed = the way a positive
        # motor command drives, and the firmware drives toward the lean, so the signs should match)
        if armed and not self.wheel_said and abs(angle) > 3 and abs(wheel) > 40:
            if (wheel > 0) == (angle > 0):
                self.wheel_votes += 1
                if self.wheel_votes >= 8:
                    self.wheel_said = True
                    diagnose("calib", "wheels_ok", {})
            else:
                print(f"[calib] wheels look like they push AWAY from the lean (angle {angle:+.1f}, wheel {wheel:+.0f} deg/s). "
                      "If the real wheels drive toward the lean, the encoder sign in this check is flipped.")

        # After a bad calibration, say once when the robot is finally held straight.
        if not self.coached and not armed and abs(pitch) <= CAL_PITCH_OK - 2 and abs(roll) <= CAL_ROLL_OK - 2:
            if not self.straight_since:
                self.straight_since = time.time()
            elif time.time() - self.straight_since > 0.8:
                self.coached = True
                text = "That's straight now. Say calibrate."
                send({"type": "answer", "question": "", "text": text})
        else:
            self.straight_since = 0.0

    def on_cal(self, parts):
        # CAL,result,mean_pitch,mean_roll,pitch_spread,gyro_spread,gravity
        try:
            result = parts[1].strip()
            pitch, roll, spread, gyro, g = (float(x) for x in parts[2:7])
        except (ValueError, IndexError):
            return
        pdir = "toward the batteries" if pitch > 0 else "away from the batteries"
        facts = {"pitch": f"{abs(pitch):.0f}", "roll": f"{abs(roll):.0f}", "pdir": pdir, "g": f"{g:.2f}",
                 "off": f"{max(abs(pitch), abs(roll)):.0f}"}
        off = max(abs(pitch), abs(roll))
        if result != "ok":
            if g < 0.85 or g > 1.15:
                issue = "cal_sensor"
            elif spread > 1.0 or gyro > 2.0:
                issue = "cal_moved"
                facts["how"] = (f"the angle wobbled {spread:.1f} degrees" if spread > 1.0
                                else f"it was turning at {gyro:.1f} degrees a second")
            else:
                issue = "cal_rejected_tilt"
        elif abs(roll) > CAL_ROLL_OK and abs(roll) - CAL_ROLL_OK >= abs(pitch) - CAL_PITCH_OK:
            issue = "cal_sideways"
        elif abs(pitch) > CAL_PITCH_OK:
            issue = "cal_tilted"
        elif self.bad_before is not None:
            issue = "cal_fixed"
            facts.update(before=f"{self.bad_before:.0f}", after=f"{max(1, round(off)):.0f}")
        else:
            issue = "cal_ok"
        good = issue in OK_ISSUES
        self.last_cal = {"result": result, "pitch": pitch, "roll": roll, "spread": spread, "gyro_spread": gyro,
                         "gravity": g, "verdict": "good" if good else "off"}
        if good:
            self.bad_before = None
        else:
            self.bad_before = max(off, self.bad_before or 0)
            self.coached = False
            self.straight_since = 0.0
            pending_fix["calib"] = {"recalibrate": 1}
        print(f"[calib] board said {result}: pitch {pitch:+.1f}, roll {roll:+.1f}, spread {spread:.2f}, "
              f"gyro {gyro:.2f}, gravity {g:.3f} -> {issue}")
        try:
            new = not os.path.exists(CAL_LOG)
            os.makedirs(os.path.dirname(CAL_LOG), exist_ok=True)
            with open(CAL_LOG, "a") as f:
                if new:
                    f.write("time,board_result,pitch_deg,roll_deg,pitch_spread,gyro_spread,gravity,kelvin\n")
                f.write(f"{time.strftime('%H:%M:%S')},{result},{pitch:.2f},{roll:.2f},{spread:.2f},{gyro:.2f},{g:.3f},{issue}\n")
        except OSError:
            pass
        diagnose("calib", issue, facts)

    def on_note(self, line):
        """The board explains why it won't arm. Say it in plain words."""
        if "Cannot arm" not in line:
            return
        if "calibrate first" in line:
            text = "I can't arm it yet. It needs a calibration first, so say calibrate."
        elif "fault" in line:
            text = "The board has a fault. Press its reset button, then calibrate again."
        else:
            text = "Hold it straight and still, then say arm again."
        send({"type": "answer", "question": "", "text": text})


calib_watch = CalibWatcher()


# ---------------------------------------------------------------- time constant lab (dip)

DIP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", time.strftime("dip_%m%d_%H%M%S.csv"))


class DipWatcher:
    """The Uno runs kelvin_dip.ino. It times every dip of the probe into hot water and reports it on a D line.
    Kelvin judges each dip, explains what a timing mistake does to the time constant, and can turn on the
    board's timing cue (beep and blinking light at the target time) as the fix."""

    TOL = 1.5                       # seconds off the target that still counts as on time
    WARM = 3.0                      # degrees above room temperature at the start that counts as "not cooled"
    offset = 0.0                    # seconds the board's timing reads long (set with --dip-offset)

    def __init__(self):
        self.last_reading = 0.0     # when the last temperature arrived
        self.connected_at = 0.0
        self.silent_warned = False
        self.target = 4.0
        self.water = None
        self.room = None
        self.cue = False
        self.in_water = False
        self.last = None            # the latest finished dip
        self.bad_before = None      # timing error (s) of the last mistimed dip, for "that worked"

    def state(self):
        return {"cue": self.cue, "target": self.target, "tol": self.TOL, "water": self.water, "room": self.room,
                "in_water": self.in_water, "last": self.last}

    def push(self):
        send({"type": "dip_state", "lab": "dip", **self.state()})

    def on_connect(self):
        self.connected_at, self.silent_warned = time.time(), False
        time.sleep(2.5)             # the Uno restarts when the port opens
        dip_link.write(f"T,{self.target:g}")
        if self.water:
            dip_link.write(f"W,{self.water:g}")
        if self.offset:
            dip_link.write(f"O,{self.offset:g}")
        dip_link.write(f"B,{1 if self.cue else 0}")
        print(f"Sent dip settings to the probe board: target {self.target:g} s, water {self.water or 'not set'}, cue {'on' if self.cue else 'off'}")

    def set_cue(self, on):
        self.cue = on
        dip_link.write(f"B,{1 if on else 0}")
        pending_fix["dip"] = None
        remember_event("dip", f"timing cue turned {'on' if on else 'off'}")
        if on:
            send({"type": "dip_cue", "lab": "dip"})
        self.push()
        return (f"Done. I'll beep at {self.target:g} seconds. Dip again and pull out on the beep."
                if on else "The timing cue is off.")

    def set_water(self, v, who="student"):
        self.water = round(v, 1)
        dip_link.write(f"W,{self.water:g}")
        remember_event("dip", f"water temperature set to {self.water:g} C ({who})")
        self.push()
        return f"Okay, the water is {self.water:g} degrees."

    def set_target(self, v):
        self.target = v
        dip_link.write(f"T,{v:g}")
        remember_event("dip", f"target dip time set to {v:g} s")
        self.push()
        return f"Okay, dips should last {v:g} seconds."

    def on_line(self, line):
        line = line.strip()
        if not line or line[0] not in "CEDA" or "," not in line:
            return
        p = line.split(",")
        try:
            if p[0] == "C":
                t, temp = float(p[1]), float(p[2])
                self.last_reading = time.time()
                if self.silent_warned:
                    self.silent_warned = False
                    send({"type": "answer", "question": "", "text": f"The probe is reading again: {temp:.1f} degrees."})
                send({"lab": "dip", "t": t, "temp": temp, "water": self.water, "in_water": self.in_water})
                recent["dip"] = {"probe_temp_c": temp, **self.state()}
            elif p[0] == "E" and p[1] == "IN":
                self.in_water = True
                send({"type": "dip_event", "lab": "dip", "event": "in", "insert_ms": float(p[3]), "target": self.target})
            elif p[0] == "E" and p[1] == "CUE":
                send({"type": "dip_event", "lab": "dip", "event": "cue"})
            elif p[0] == "A":
                print("[probe]", line)
                if p[1] == "NOPROBE" and not self.silent_warned:
                    self.silent_warned = True
                    send({"type": "answer", "question": "", "text": "The probe isn't answering. A probe wire is loose. "
                          "Check red to 5 volts, yellow to pin 2, and black to ground."})
                if p[1] == "CONTACT":
                    print("[dip] water-contact wires detected: dips are now timed exactly")
                if p[1] == "ROOM":
                    self.room = float(p[2])
                    self.push()
            elif p[0] == "D":
                self.on_dip(p)
        except (ValueError, IndexError):
            pass

    def on_dip(self, p):
        # D,trial,insert_ms,remove_ms,dwell_s,T_start,T_read,T_water,theta,tau_assumed,tau_actual,flags
        num = lambda x: None if x.strip().lower() == "nan" else float(x)
        trial, dwell, start, read = int(p[1]), float(p[4]), float(p[5]), float(p[6])
        tau_a, tau = num(p[9]), num(p[10])
        flags = p[11].strip().split("|")
        self.in_water = False
        err = dwell - self.target
        self.last = {"trial": trial, "dwell": dwell, "start": start, "read": read, "tau": tau, "tau_assumed": tau_a,
                     "flags": flags, "error_s": round(err, 2)}
        f = {"dwell": f"{dwell:.1f}", "target": f"{self.target:g}"}
        fix = None
        long_dip = dwell >= max(8.0, 2 * self.target)
        if long_dip and ("NO_TWATER" in flags or "SETTLED" in flags):
            # a long soak is how the student measures the water temperature
            self.water = round(read, 1)
            dip_link.write(f"W,{self.water:g}")
            issue = "dip_water"
            f["water"] = f"{self.water:g}"
        elif "NO_TWATER" in flags:
            issue = "dip_need_water"
        elif abs(err) > self.TOL:          # Kelvin's own tolerance; the board's LATE/EARLY flags use 0.5 s
            late = err > 0
            if self.cue:
                f["advice"] = "Pull it out on the beep."
            else:
                f["advice"] = f"Say fix it and I'll beep at {self.target:g} seconds."
                fix = {"cue": 1}
            if tau and tau_a:
                issue = "dip_late" if late else "dip_early"
                f.update(tau_assumed=f"{tau_a:.1f}", tau_actual=f"{tau:.1f}", pct=f"{abs(tau_a - tau) / tau * 100:.0f}")
            else:
                issue = "dip_timing"
            self.bad_before = abs(err)
        elif self.room is not None and start - self.room > self.WARM:
            issue = "dip_not_reset"
            f.update(start=f"{start:.0f}", above=f"{start - self.room:.0f}", room=f"{self.room:.0f}")
        elif tau is None:
            issue = "dip_need_water" if not self.water else "dip_timing"
            f["advice"] = "The reading had already settled, so try a shorter dip."
        elif self.bad_before is not None:
            issue = "dip_fixed"
            f.update(before=f"{self.bad_before:.1f}", after=f"{abs(err):.1f}", tau=f"{tau:.1f}")
            self.bad_before = None
        else:
            issue = "dip_ok"
            f["tau"] = f"{tau:.1f}"
        self.last["verdict"] = issue
        print(f"[dip] trial {trial}: {dwell:.2f} s (target {self.target:g}), {start:.1f} -> {read:.1f} C, "
              f"tau {tau}, flags {'|'.join(flags)} -> {issue}")
        try:
            new = not os.path.exists(DIP_LOG)
            os.makedirs(os.path.dirname(DIP_LOG), exist_ok=True)
            with open(DIP_LOG, "a") as fh:
                if new:
                    fh.write("time,trial,dwell_s,target_s,t_start,t_read,t_water,tau_assumed,tau_actual,flags,cue,kelvin\n")
                fh.write(f"{time.strftime('%H:%M:%S')},{trial},{dwell:.2f},{self.target:g},{start:.2f},{read:.2f},"
                         f"{self.water or ''},{tau_a or ''},{tau or ''},{'|'.join(flags)},{int(self.cue)},{issue}\n")
        except OSError:
            pass
        self.push()
        diagnose("dip", issue, f, fix)


dip_watch = DipWatcher()


def dip_silence_watchdog():
    """kelvin_dip.ino prints nothing at all when the sensor read fails (a loose probe wire), so say it out loud."""
    while True:
        time.sleep(2)
        w = dip_watch
        if not dip_link or isinstance(dip_link, FakeDip) or w.silent_warned or not w.connected_at:
            continue
        if time.time() - max(w.last_reading, w.connected_at) > 7:
            w.silent_warned = True
            text = ("The probe board is connected, but no temperature is coming in. A probe wire is probably loose. "
                    "Check red to 5 volts, yellow to pin 2, black to ground, and the resistor between red and yellow.")
            print("[dip] no temperature readings for 7 seconds: the sensor read is failing (loose probe wire?)")
            send({"type": "answer", "question": "", "text": text})


# ---------------------------------------------------------------- line routing

def on_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        if line:
            print("[board]", line)
            calib_watch.on_note(line)
        return
    parts = line.split(",")
    if parts[0] == "T":
        on_motor_line(parts)
    elif parts[0] == "C":
        on_cooling_line(parts)
    elif parts[0] == "B":
        calib_watch.on_status(parts)
    elif parts[0] == "CAL":
        calib_watch.on_cal(parts)


# ---------------------------------------------------------------- real boards

class SerialLink:
    """One USB board. Survives being unplugged: says so once, then reconnects when it's plugged back in."""

    def __init__(self, port, baud=115200, on_connect=None, handler=None):
        self.handler = handler or on_line
        import serial
        self.serial, self.port, self.baud, self.on_connect = serial, port, baud, on_connect
        self.ser = serial.Serial(port, baud, timeout=1)
        self.ok = True
        self.lock = threading.Lock()
        threading.Thread(target=self.read_loop, daemon=True).start()
        print(f"Connected to {port}")
        if on_connect:
            threading.Thread(target=on_connect, daemon=True).start()

    def lost(self):
        if self.ok:
            self.ok = False
            print(f"Lost the board on {self.port} (unplugged?). Plug it back in and I'll reconnect.")
        try:
            self.ser.close()
        except Exception:
            pass

    def read_loop(self):
        while True:
            if not self.ok:
                time.sleep(2)
                try:
                    self.ser = self.serial.Serial(self.port, self.baud, timeout=1)
                    self.ok = True
                    print(f"Reconnected to {self.port}")
                    if self.on_connect:
                        threading.Thread(target=self.on_connect, daemon=True).start()
                except Exception:
                    continue
            try:
                raw = self.ser.readline()
                if raw:
                    self.handler(raw.decode(errors="ignore"))
            except Exception:
                self.lost()

    def write(self, text: str):
        if not self.ok:
            return
        with self.lock:
            try:
                self.ser.write((text.strip() + "\n").encode())
            except Exception:
                self.lost()


# ---------------------------------------------------------------- fake boards

class FakeMotor:
    """Behaves like our real MinSeg running minseg_speed.ino: same PI controller on board, same output lines,
    same K and S commands. Plant measured Nov 14: full power ~130 RPM, needs ~95/255 power to start turning."""

    DEAD, TOP, TAU, CPR = 95, 130.0, 0.12, 720

    def __init__(self):
        self.kp, self.ki, self.kd = settings["kp"], settings["ki"], settings["kd"]
        self.target = 0.0
        self.w = 0.0            # true wheel speed, RPM
        self.count = 0.0        # encoder counts
        self.brake = False
        self.integral = 0.0
        threading.Thread(target=self.run, daemon=True).start()

    def write(self, text):
        p = text.strip().split(",")
        try:
            if p[0] == "K" and len(p) == 4:
                ki = float(p[2])
                self.integral = self.integral * self.ki / ki if ki > 0 else 0.0   # same as the firmware
                self.kp, self.ki, self.kd = float(p[1]), ki, float(p[3])
            elif p[0] == "S" and len(p) == 2:
                self.target = max(-120.0, min(120.0, float(p[1])))
                if self.target == 0:
                    self.integral = 0.0
        except ValueError:
            pass

    def run(self):
        t0 = time.time()
        last_err, filt, last_count = 0.0, 0.0, 0
        while True:
            time.sleep(0.02)
            dt = 0.02
            c = int(self.count)
            filt = 0.7 * filt + 0.3 * ((c - last_count) / dt * 60 / self.CPR)
            last_count = c
            err = self.target - filt
            der = (err - last_err) / dt
            last_err = err
            out = self.kp * err + self.ki * self.integral + self.kd * der
            if (out < 255 or err < 0) and (out > -255 or err > 0):
                self.integral += err * dt
            if self.ki > 0:
                self.integral = max(-255 / self.ki, min(255 / self.ki, self.integral))
            if self.target == 0:
                out, self.integral = 0, 0.0
            pwm = int(max(-255, min(255, out)))
            for _ in range(20):                       # motor physics in 1 ms steps
                eff = 0.0 if abs(pwm) < self.DEAD else math.copysign((abs(pwm) - self.DEAD) * self.TOP / (255 - self.DEAD), pwm)
                if self.brake:                        # type 'brake': like pressing an eraser on the wheel
                    eff -= math.copysign(min(abs(eff), 45.0), self.w) if self.w else 0.0
                self.w += (eff - self.w) * (0.001 / self.TAU)
                self.count += self.w / 60 * self.CPR * 0.001
            on_line(f"T,{int((time.time() - t0) * 1000)},{self.target:.1f},{filt:.1f},{pwm},0")


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
            truth = ROOM_TEMP + (80 - ROOM_TEMP) * math.exp(-0.003 * t)   # a mug, sped up a little
            cur += (ROOM_TEMP - cur) * 0.35 if self.out else (truth - cur) * 0.6
            on_line(f"C,{int(t * 1000)},{cur + random.uniform(-0.15, 0.15):.2f}")


class FakeTilt:
    """Behaves like minseg_balance.ino held in someone's hands. Terminal: 'pose 9 0' holds it 9 degrees toward
    the batteries, 'pose 0 12' tips it 12 degrees sideways, 'pose 0 0' is straight up, 'shake' toggles a wobbly hand."""

    def __init__(self):
        self.pitch, self.roll, self.shake = 9.0, 0.0, False
        self.cal, self.offset, self.armed = False, 0.0, False
        self.busy_until = 0.0
        threading.Thread(target=self.run, daemon=True).start()

    def now_pose(self):
        wob = 2.5 if self.shake else 0.25
        return self.pitch + random.gauss(0, wob), self.roll + random.gauss(0, wob)

    def write(self, text):
        c = text.strip().lower()
        if c == "c":
            self.armed = False
            threading.Thread(target=self.calibrate, daemon=True).start()
        elif c == "a":
            if not self.cal:
                on_line("# Cannot arm: send c to calibrate first.")
            elif abs(self.pitch - self.offset) > 4 or self.shake:
                on_line("# Cannot arm: hold upright and still, retry a.")
            else:
                self.armed = True
                on_line("# ARMED. x stops motors.")
        elif c == "x":
            self.armed = False
            on_line("# STOPPED")

    def calibrate(self):
        on_line("# Hold UPRIGHT and STILL.")
        self.busy_until = time.time() + 4
        time.sleep(2)
        ps, rs = [], []
        for _ in range(40):
            p, r = self.now_pose()
            ps.append(p)
            rs.append(r)
            time.sleep(0.05)
        mp, mr, sd = statistics.mean(ps), statistics.mean(rs), statistics.pstdev(ps)
        gyro = 3.0 if self.shake else 0.4
        rejected = abs(mp) > 15 or sd > 1.0 or gyro > 2.0
        on_line(f"CAL,{'rejected' if rejected else 'ok'},{mp:.2f},{mr:.2f},{sd:.2f},{gyro:.2f},1.003")
        if not rejected:
            self.cal, self.offset = True, mp
            on_line("# Calibration OK. Motors OFF.")

    def run(self):
        t0 = time.time()
        while True:
            time.sleep(0.05)
            p, r = self.now_pose()
            angle = (p - self.offset) if self.cal else 0.0
            wheel = 12 * angle if self.armed and abs(angle) > 1 else 0.0
            pwm = int(max(-180, min(180, 8 * angle))) if self.armed else 0
            on_line(f"B,{int((time.time() - t0) * 1000)},{p:.1f},{r:.1f},{angle:.1f},0.0,{pwm},{1 if self.armed else 0},"
                    f"{wheel:.0f},{1 if self.cal else 0}")


class FakeDip:
    """Behaves like kelvin_dip.ino. Terminal: 'dip 6' dips the probe for 6 seconds, 'dip' dips for the target time
    (or pulls out on the beep if the cue is on), 'soak' is a 15 second dip to measure the water."""

    ROOM, WATER, TAU = 22.0, 58.0, 2.4

    def __init__(self):
        self.temp, self.target, self.tw, self.cue = self.ROOM, 4.0, None, False
        self.trial, self.until, self.t_in, self.start, self.cued = 0, 0.0, 0.0, 0.0, False
        self.t0 = time.time()
        threading.Thread(target=self.run, daemon=True).start()

    def ms(self):
        return int((time.time() - self.t0) * 1000)

    def write(self, text):
        p = text.strip().split(",")
        try:
            if p[0] == "W":
                self.tw = float(p[1])
            elif p[0] == "T":
                self.target = float(p[1])
            elif p[0] == "B":
                self.cue = float(p[1]) > 0.5
        except (ValueError, IndexError):
            pass

    def dip(self, seconds):
        if self.until:
            return
        self.trial += 1
        self.t_in, self.start, self.cued = time.time(), self.temp, False
        self.until = self.t_in + seconds
        dip_watch.on_line(f"E,IN,{self.trial},{self.ms()}")

    def run(self):
        dip_watch.on_line(f"A,ROOM,{self.ROOM:.2f}")
        while True:
            time.sleep(0.2)
            now = time.time()
            if self.until:
                self.temp += (self.WATER - self.temp) * (1 - math.exp(-0.2 / self.TAU))
                if self.cue and not self.cued and now - self.t_in >= self.target:
                    self.cued = True
                    dip_watch.on_line(f"E,CUE,{self.trial},{self.ms()}")
                if now >= self.until:
                    dwell, read = now - self.t_in, self.temp
                    flags = []
                    if self.start - self.ROOM > 1.5:
                        flags.append("NOT_RESET")
                    if dwell > self.target + 0.5:
                        flags.append("LATE")
                    elif dwell < self.target - 0.5:
                        flags.append("EARLY")
                    theta = ta = tt = float("nan")
                    if self.tw is None or self.tw <= self.start:
                        flags.append("NO_TWATER")
                    else:
                        theta = (read - self.start) / (self.tw - self.start)
                        if theta <= 0 or theta >= 0.995:
                            flags.append("SETTLED")
                        else:
                            ta, tt = -self.target / math.log(1 - theta), -dwell / math.log(1 - theta)
                    self.until = 0.0
                    dip_watch.on_line(f"D,{self.trial},0,0,{dwell:.2f},{self.start:.2f},{read:.2f},"
                                      f"{self.tw if self.tw is not None else 'nan'},{theta:.3f},{ta:.2f},{tt:.2f},{'|'.join(flags) or 'OK'}")
            else:
                self.temp += (self.ROOM - self.temp) * (1 - math.exp(-0.2 / 1.5))    # sped up so tests don't wait
            dip_watch.on_line(f"C,{self.ms()},{self.temp + random.uniform(-0.1, 0.1):.2f}")


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "settings", **settings}))
    await ws.send_text(json.dumps({"type": "action", "action": "switch_lab", "lab": DEFAULT_LAB}))
    if dip_link:
        await ws.send_text(json.dumps({"type": "dip_state", "lab": "dip", **dip_watch.state()}))
    for lab_name in ("cooling", "motor", "calib", "dip"):          # a refreshed dashboard still shows the mistake that's open
        if open_diag[lab_name]:
            await ws.send_text(json.dumps({**open_diag[lab_name], "fix": pending_fix[lab_name]} if pending_fix[lab_name]
                                          else {k: v for k, v in open_diag[lab_name].items() if k != "fix"}))
    if pending_fix["motor"]:
        await ws.send_text(json.dumps({"type": "fix_update", "lab": "motor", "fix": pending_fix["motor"]}))
    for e in ta_thread:
        await ws.send_text(json.dumps({"type": "ta_msg", "replay": True, **e}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "apply_fix" and isinstance(msg.get("fix"), dict):
                vals, problem = clean_fix(msg["fix"])
                if vals:
                    apply_settings(vals)
                else:
                    send({"type": "answer", "question": "Apply fix", "text": problem})
            elif msg.get("type") == "report_to_ta":    # the student said "send this to the TA" with the report open
                summary = str(msg.get("summary") or "").strip()[:2000]
                if not ta_enabled():
                    text, ok = "TA texting isn't set up yet, so I couldn't send it. You can save it as a PDF instead.", "off"
                elif not summary:
                    text, ok = "The report is still being written. Try again in a second.", "empty"
                else:
                    notify("report", f"Lab report from {STATION}: {summary}", minutes=msg.get("minutes"),
                           mistakes=msg.get("mistakes"), fixed=msg.get("fixed"), settings=settings)
                    remember_event("chat", "the student texted their lab report to the TA")
                    text, ok = "Sent. Your TA has the report.", "ok"
                send({"type": "answer", "question": "Send this to the TA", "text": text, "report_sent": ok})
            elif msg.get("type") == "report":
                threading.Thread(target=send_report, args=(bool(msg.get("to_ta")),), daemon=True).start()
            elif msg.get("type") == "request_ta":
                try:
                    report = await asyncio.to_thread(build_report)
                    status = await asyncio.to_thread(send_ta_report, report["summary"])
                except Exception as exc:
                    print(f"[Photon] TA report failed ({type(exc).__name__})")
                    status = "Could not send the TA report. Check the backend terminal."
                await ws.send_text(json.dumps({"type": "ta_status", "text": status}))
            elif msg.get("type") == "ask":            # {"type":"ask","question":"...","lab":"motor"}
                threading.Thread(target=handle_voice, args=(str(msg.get("question", "")), str(msg.get("lab", ""))),
                                 daemon=True).start()
            elif msg.get("type") == "command" and motor_link:   # e.g. {"type":"command","text":"S,80"}
                motor_link.write(str(msg.get("text", "")))
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.post("/api/lines")
def api_lines(body: dict):
    """Readings from a board plugged into a different laptop (relay.py). Body: {"lines": ["C,1000,71.2", ...]}"""
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list):
        return {"ok": False, "error": "send {\"lines\": [...]}"}
    for line in lines[:500]:
        if isinstance(line, str):
            on_line(line)
    return {"ok": True, "count": len(lines)}


@app.post("/api/ask")
def api_ask(body: dict):
    """Photon sends a text here and texts back Kelvin's reply.
    Body: {"question": "how is station 3 doing?", "from": "ta"}. Reply: {"text": "..."}"""
    question = str(body.get("question") or body.get("text") or "").strip()
    if not question:
        return {"text": "", "error": "no question"}
    source = "student" if str(body.get("from", "ta")).lower() == "student" else "ta"
    return {"text": handle_voice(question, str(body.get("lab") or ""), source), "station": STATION}


@app.get("/ready")
def ready():
    return {"ok": True}


DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "index.html")


@app.get("/")
def dashboard():
    """Serve the dashboard at http://localhost:8000 so the browser remembers the mic permission."""
    return FileResponse(DASHBOARD)


@app.get("/health")
def health():
    return {"ok": True, "settings": settings, "dashboards": len(clients)}


# ---------------------------------------------------------------- main

def lan_ip():
    """This laptop's address on the Wi-Fi or hotspot. Sends nothing, just asks the OS which network it would use."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "this-laptops-ip"


def main():
    global motor_link, calib_link, dip_link, DEFAULT_LAB, ROOM_TEMP, STATION, NOTIFY_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor-port", help="robot COM port, e.g. COM5 or /dev/ttyACM0")
    ap.add_argument("--cooling-port", help="cooling probe COM port, e.g. COM6")
    ap.add_argument("--calib-port", help="robot running minseg_balance.ino, for the calibration lab, e.g. COM17")
    ap.add_argument("--dip-port", help="Uno running kelvin_dip.ino, for the time constant lab, e.g. COM16")
    ap.add_argument("--water-temp", type=float, help="hot water temperature in C for the time constant lab")
    ap.add_argument("--dip-seconds", type=float, default=4.0, help="target dip time in seconds")
    ap.add_argument("--dip-tolerance", type=float, default=DipWatcher.TOL, help="seconds off the target that still counts as on time")
    ap.add_argument("--dip-offset", type=float, default=0.0, help="seconds the probe board's dip timing reads long (measure with a stopwatch)")
    ap.add_argument("--lab", choices=["motor", "cooling", "calib", "dip"], help="lab the dashboard opens on")
    ap.add_argument("--fake", action="store_true", help="simulate both boards")
    ap.add_argument("--kp", type=float, default=settings["kp"])
    ap.add_argument("--ki", type=float, default=settings["ki"])
    ap.add_argument("--kd", type=float, default=settings["kd"])
    ap.add_argument("--low", type=float, default=30, help="low target speed (RPM) for the step test")
    ap.add_argument("--high", type=float, default=80, help="high target speed (RPM) for the step test")
    ap.add_argument("--step-seconds", type=float, default=4, help="seconds between target changes")
    ap.add_argument("--room-temp", type=float, default=ROOM_TEMP, help="room temperature in C, for the cooling lab")
    ap.add_argument("--station", default=STATION, help='name the TA sees in texts, e.g. "Station 3"')
    ap.add_argument("--notify-url", default=NOTIFY_URL, help="Photon service URL that texts the TA")
    args = ap.parse_args()
    ROOM_TEMP, STATION, NOTIFY_URL = args.room_temp, args.station, args.notify_url
    settings.update(kp=args.kp, ki=args.ki, kd=args.kd)
    stepper.update(low=args.low, high=args.high, seconds=args.step_seconds)

    dip_watch.target, dip_watch.water = args.dip_seconds, args.water_temp
    dip_watch.TOL, dip_watch.offset = args.dip_tolerance, args.dip_offset
    cooling = None
    if args.fake:
        dip_link = FakeDip()
        dip_watch.on_connect_fake = True
        dip_link.write(f"T,{dip_watch.target:g}")
        if dip_watch.water:
            dip_link.write(f"W,{dip_watch.water:g}")
        motor_link, cooling, calib_link = FakeMotor(), FakeCooling(), FakeTilt()
        print("Fake boards running. Type 'bad' for a bad controller, 'brake' to press on the wheel,\n"
              "'out'/'in' to move the probe, 'hold 80' for a steady speed, 'ta <message>' to text as the TA.")
    else:
        if args.motor_port:
            def send_settings_after_boot():
                motor_watch.paused_until = time.time() + 4
                time.sleep(2.5)          # the Mega restarts when the port opens; wait for it, then load our settings
                motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
                print(f"Sent settings to the robot: kp {settings['kp']:g}, ki {settings['ki']:g}, kd {settings['kd']:g}")
            motor_link = SerialLink(args.motor_port, on_connect=send_settings_after_boot)

            def stop_wheel():
                try:
                    motor_link.write("S,0")
                    time.sleep(0.1)
                except Exception:
                    pass
            import atexit
            atexit.register(stop_wheel)          # Ctrl+C stops the wheel instead of leaving it spinning
        if args.cooling_port:
            SerialLink(args.cooling_port)
        if args.calib_port:
            calib_link = SerialLink(args.calib_port)
            import atexit
            atexit.register(lambda: calib_link.write("x"))     # Ctrl+C turns the robot's motor off
        if args.dip_port:
            dip_link = SerialLink(args.dip_port, baud=9600, on_connect=dip_watch.on_connect, handler=dip_watch.on_line)
    DEFAULT_LAB = args.lab or ("calib" if args.calib_port else "dip" if args.dip_port and not args.motor_port else "motor")

    def console():
        while True:
            try:
                cmd = input().strip()
            except (EOFError, OSError):
                return                     # no keyboard attached (running in the background)
            if cmd == "brake" and isinstance(motor_link, FakeMotor):
                motor_link.brake = not motor_link.brake
                print("Brake ON (type brake again to release)" if motor_link.brake else "Brake released")
            elif cmd.startswith("hold "):
                print(do_action({"type": "hold", "value": cmd.split()[1]}, "motor"))
            elif cmd.startswith("pose ") and isinstance(calib_link, FakeTilt):
                try:
                    calib_link.pitch, calib_link.roll = (float(v) for v in cmd.split()[1:3])
                    print(f"Holding it {calib_link.pitch:g} degrees forward/back, {calib_link.roll:g} sideways")
                except ValueError:
                    print("Type pose <forward/back degrees> <sideways degrees>, e.g. pose 9 0")
            elif cmd.split()[:1] == ["dip"] and isinstance(dip_link, FakeDip):
                parts = cmd.split()
                dip_link.dip(float(parts[1]) if len(parts) > 1 else dip_link.target)
            elif cmd == "soak" and isinstance(dip_link, FakeDip):
                dip_link.dip(15)
            elif cmd[:2].upper() in ("W,", "T,", "B,", "O,") or cmd.upper() in ("R", "N"):
                if dip_link:
                    dip_link.write(cmd.upper())
            elif cmd == "shake" and isinstance(calib_link, FakeTilt):
                calib_link.shake = not calib_link.shake
                print("Shaky hands ON" if calib_link.shake else "Steady hands")
            elif cmd in ("c", "a", "x") and calib_link:
                calib_link.write(cmd)
            elif cmd.startswith("ta "):
                threading.Thread(target=lambda q=cmd[3:]: print("[reply to TA]", handle_voice(q, "", "ta")),
                                 daemon=True).start()
            elif cmd == "bad" and motor_link:
                settings.update(kp=1.0, ki=40.0, kd=0.0)
                motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
                motor_watch.flagged = False
                send({"type": "settings", **settings})
                print("Loaded a bad controller (kp=1, ki=40): the wheel will overshoot ~40% on each speed-up")
            elif cmd in ("out", "in") and cooling:
                cooling.out = cmd == "out"
            elif cmd in ("stop", "start"):
                print(do_action({"type": cmd}, "motor"))
            elif cmd and motor_link:
                motor_link.write(cmd)    # anything else goes straight to the robot, e.g. S,80
    threading.Thread(target=console, daemon=True).start()
    threading.Thread(target=run_stepper, daemon=True).start()
    threading.Thread(target=stuck_watchdog, daemon=True).start()
    threading.Thread(target=dip_silence_watchdog, daemon=True).start()

    ai = "Gemini" if os.environ.get("GEMINI_API_KEY") else "Claude" if os.environ.get("ANTHROPIC_API_KEY") else None
    print("\n  Dashboard: open http://localhost:8000 in Edge")
    print(f"  AI: {ai} key found" if ai else "  AI: NO KEY SET. Kelvin will use plain templates and can't answer questions well.")
    print(f"  Other laptops reach this one at: http://{lan_ip()}:8000   (for relay.py and the Photon service)")
    ta_how = "on, through Photon iMessage" if photon_ready() else ("on, sending to " + NOTIFY_URL) if NOTIFY_URL else "off (add the PHOTON_ keys to backend/.env)"
    print(f"  TA texts: {ta_how}   Station: {STATION}")
    if photon_ready():                 # the TA can text Kelvin back: questions, "OK" for approvals, "send me the report"
        threading.Thread(target=poll_ta, args=(lambda q: handle_voice(q, "", "ta"),), daemon=True).start()
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
