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
OVERSHOOT_ALERT = 25.0     # percent overshoot that counts as a mistake
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
    "probe_fast": "The reading is falling {ratio} times faster than hot water can cool. "
                  "The probe looks like it is out of the water, or in something colder. Put it back in the cup.",
    "probe_high": "Your reading is {rise} degrees above where the cooling water should be. "
                  "Something warmer is touching the probe, or it moved into different water.",
    "probe_back": "The probe is back and the readings are heading back to the curve. I logged the mistake for the report.",
    "drag": "Something is dragging on the wheel. Speed fell {drop} percent below target while motor power went up "
            "{power} percent to fight it. That's a physical problem, not your settings, so don't change the gains. "
            "Check for anything touching the wheel.",
    "drag_gone": "The drag is gone and the wheel is back on target. Your settings were fine the whole time.",
}
OK_ISSUES = ("fixed", "probe_back", "drag_gone")      # these turn the dashboard green


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
            for model in gemini_models():
                body = plain if model in GEMINI_NO_LEVEL else fast
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                       f"?key={os.environ['GEMINI_API_KEY']}")
                req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = json.load(r)
                except urllib.error.HTTPError as e:
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
recent = {"motor": None, "cooling": None, "events": []}


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

SAFE = {"kp": (0.01, 20.0), "ki": (0.0, 20.0), "kd": (0.0, 5.0)}      # Kelvin applies these on its own
HARD = {"kp": (0.001, 60.0), "ki": (0.0, 60.0), "kd": (0.0, 15.0)}    # past SAFE but inside HARD needs the TA's OK
pending_fix = {"motor": None, "cooling": None}   # the suggested fix waiting for "apply"
open_diag = {"motor": None, "cooling": None}     # the mistake on screen right now, resent when a dashboard (re)connects
undo_stack = []                                  # earlier settings, newest last
stepper = {"low": 40.0, "high": 100.0, "on": True, "seconds": 4.0}
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
        to = "cooling" if str(a.get("lab", "")).lower().startswith("cool") else "motor"
        send({"type": "action", "action": "switch_lab", "lab": to})
        return "Here's the cooling lab." if to == "cooling" else "Here's the motor lab."
    if t == "set_target":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the speed you want."
        if not 5 <= v <= 200:
            return f"{v:g} is outside the safe range of 5 to 200, so I left the speed alone."
        stepper["high"], stepper["low"], stepper["on"] = v, round(v * 0.4, 1), True
        remember_event("motor", f"student set the target speed to step between {stepper['low']:g} and {v:g}")
        return f"Done. The wheel will now step between {stepper['low']:g} and {v:g}."
    if t == "hold":
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            return "I didn't catch the speed you want."
        if not 5 <= v <= 200:
            return f"{v:g} is outside the safe range of 5 to 200, so I left the speed alone."
        stepper["high"] = stepper["low"] = v
        stepper["on"] = True
        if motor_link:
            motor_link.write(f"S,{v:g}")
        remember_event("motor", f"wheel set to hold a steady {v:g}")
        return f"Holding the wheel at a steady {v:g}. Say set the speed to step it up and down again."
    if t == "stop":
        stepper["on"] = False
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
        base = (pending_fix["motor"] or {}).get("kp", settings["kp"])
        factor = {"third": 1 / 3, "half": 0.5, "quarter": 0.25, "double": 2, "twice": 2}[rel.group(1)]
        return {"type": "set_fix", "kp": round(base * factor, 3), "apply": bool(re.search(r"\bapply\b", s))}
    m = re.search(r"\b(switch|go|change|move|show|open|take me)\b.*\b(cooling|cup|temperature|water|motor|wheel|robot)\b", s)
    if m and not measured:
        to = "cooling" if m.group(2) in ("cooling", "cup", "temperature", "water") else "motor"
        if to != lab:                     # already on that lab? then it's probably a question, let the AI answer
            return {"type": "switch_lab", "lab": to}
    if re.search(r"\b(apply|go ahead|send it|do it|use (it|that|the fix))\b", s) and not measured:
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
    "switch_lab": r"switch|cooling|cup|temperature|motor|wheel|show|open|lab|go to",
}


def status_line() -> str:
    last = next((e for e in reversed(session["events"]) if "[chat]" not in e), "nothing has gone wrong yet")
    last = re.sub(r"^\d\d:\d\d:\d\d \[\w+\] ", "", last)
    m, f = session["mistakes"], session["fixed"]
    return (f"{STATION}: {m} mistake{'' if m == 1 else 's'} caught, {f} fixed. Settings kp "
            f"{settings['kp']:g}, ki {settings['ki']:g}, kd {settings['kd']:g}. Latest: {last}")


def handle_voice(question: str, lab: str, source: str = "student") -> str:
    """Someone talked to Kelvin. source "student" = voice or typing at the bench, "ta" = a text through Photon.
    Do what they asked, or answer the question. Both share one memory, so the TA can ask about the bench."""
    question = question.strip()[:300]
    if not question:
        return ""
    lab = lab or "motor"
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
            "minutes_into_session": round((time.time() - session["started"]) / 60, 1),
            "mistakes_caught": session["mistakes"], "fixes_that_worked": session["fixed"],
            "session_log": (session["events"][-30:] if source == "ta" else recent["events"]) or ["nothing wrong yet"],
        }
        prompt = (
            "You are Kelvin, a friendly lab partner watching a student's experiment through live sensor data. "
            "Labs: 'motor' is a robot wheel whose speed is held at a target by the student's controller "
            "(settings kp, ki, kd). 'cooling' is a cup of hot water with a temperature probe; 'expected' is what "
            "Newton's law of cooling predicts. The session_log is everything that happened, including what the "
            "student said to you out loud and what the TA texted.\n"
            f"You are talking to {who}.\n"
            "Decide whether they want you to DO something. You can take at most one of these actions:\n"
            '- {"type":"apply_fix"}: send the suggested fix that is waiting to the robot.\n'
            '- {"type":"set_fix","kp":number,"ki":number,"kd":number,"apply":bool}: change the suggested fix. '
            "Include only the settings that change. Relative requests like 'a third of that', 'half', 'a bit lower' "
            "are relative to the suggested fix waiting if there is one, otherwise to the settings on the robot; "
            "work out the actual number. Set apply to true only if they also asked to apply or send it now.\n"
            '- {"type":"undo"}: go back to the previous settings.\n'
            '- {"type":"switch_lab","lab":"motor" or "cooling"}: show the other lab on the bench screen.\n'
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
            + "You cannot save files, send email, or do anything not listed above, so never claim to. "
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
                say = ("I can't reach my AI right now. I can still apply a fix, undo, switch labs, "
                       "change the speed, or show the report.")
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
        if issue != "drag_gone":                       # drag was physical, so any gain fix still stands
            pending_fix[lab] = None
            session["fixed"] += 1
        if ta["open_since"] and (issue != "drag_gone" or ta["open_issue"] == "drag"):
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
        final = ai_rewrite(issue, facts, text) if not good else text
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
        # drag detection: once the wheel has settled at a steady target, watch for something slowing it down
        self.settle_count = 0
        self.settled = False
        self.base_motor = 0.0
        self.drag_count = 0
        self.in_drag = False
        self.recover_count = 0
        self.disturbed = False     # this step had drag in it, so don't judge the student's settings on it

    def arm_verify(self):
        self.verify = True
        self.skip_next = True

    def add(self, target, speed, motor):
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
            self.recover_count = self.recover_count + 1 if abs(err) < 0.08 else 0
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
        if err > 0.20 and rise >= max(15.0, 0.15 * self.base_motor):
            self.drag_count += 1
        else:
            self.drag_count = 0
            if abs(err) < 0.05:                          # slowly track the normal motor power
                self.base_motor = 0.98 * self.base_motor + 0.02 * abs(motor)
        if self.drag_count >= 15:                        # slowed and fighting for 0.3 seconds
            self.in_drag, self.disturbed, self.recover_count = True, True, 0
            power = round(rise / max(self.base_motor, 1.0) * 100)
            diagnose("motor", "drag", {"drop": round(err * 100), "power": power})

    def finish(self):
        if self.skip_next:
            self.skip_next = False
            return
        if self.disturbed:                               # drag made this step meaningless for tuning
            return
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
    FAST_RATE = 0.25          # degrees per second; hot water never cools this fast, a probe in air does
    RATE_FACTOR = 5.0         # ...or this many times faster than physics predicts

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
        del self.recent[:-10]

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
                physics_rate = self.k * max(exp - ROOM_TEMP, 0.1)   # how fast the water should be cooling now
                too_fast = s is not None and -s > max(self.FAST_RATE, self.RATE_FACTOR * physics_rate)
                self.fast_count = self.fast_count + 1 if too_fast and gap > 0.7 else 0
                if self.fast_count >= 2:
                    self.flag("low", t, exp, temp, "probe_fast",
                              {"ratio": max(2, round(-s / max(physics_rate, 0.01))), "drop": round(gap, 1)})
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
        heading_back = s is not None and ((self.flagged == "low" and s > 0.25) or (self.flagged == "high" and s < -0.25))
        self.rise_count = self.rise_count + 1 if heading_back else 0
        still_falling = s is not None and s < -0.1
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
    recent["cooling"] = {"temp_c": temp, "expected_c": msg.get("expected"), "seconds": round(t / 1000)}


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
        self.brake = False
        threading.Thread(target=self.run, daemon=True).start()

    def write(self, text):
        p = text.strip().split(",")
        if p[0] == "K" and len(p) == 4:
            self.kp, self.ki, self.kd = float(p[1]), float(p[2]), float(p[3])
        elif p[0] == "S" and len(p) == 2:
            self.target = float(p[1])

    def run(self):
        t0 = time.time()
        while True:
            time.sleep(0.02)
            wn, zeta = 7.0, max(0.08, min(1.0, 1.0 / max(self.kp, 0.01)))
            self.vel += (wn * wn * (self.target - self.speed) - 2 * zeta * wn * self.vel) * 0.02
            self.speed += self.vel * 0.02
            if self.brake:                 # type 'brake' in the terminal: like pressing an eraser on the wheel
                self.vel -= 30.0 * self.speed * 0.02
            sp = self.speed + random.uniform(-0.4, 0.4)
            effort = 4.0 * (self.target - self.speed) if self.brake else 0.0   # the controller fighting back
            motor = max(-255, min(255, sp * 2 + self.vel * 0.4 + effort))
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
            truth = ROOM_TEMP + (80 - ROOM_TEMP) * math.exp(-0.003 * t)   # a mug, sped up a little
            cur += (ROOM_TEMP - cur) * 0.35 if self.out else (truth - cur) * 0.6
            on_line(f"C,{int(t * 1000)},{cur + random.uniform(-0.15, 0.15):.2f}")


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "settings", **settings}))
    for lab_name in ("cooling", "motor"):          # a refreshed dashboard still shows the mistake that's open
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
    global motor_link, ROOM_TEMP, STATION, NOTIFY_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--motor-port", help="robot COM port, e.g. COM5 or /dev/ttyACM0")
    ap.add_argument("--cooling-port", help="cooling probe COM port, e.g. COM6")
    ap.add_argument("--fake", action="store_true", help="simulate both boards")
    ap.add_argument("--kp", type=float, default=settings["kp"])
    ap.add_argument("--ki", type=float, default=settings["ki"])
    ap.add_argument("--kd", type=float, default=settings["kd"])
    ap.add_argument("--low", type=float, default=40, help="low target speed for the step test")
    ap.add_argument("--high", type=float, default=100, help="high target speed for the step test")
    ap.add_argument("--step-seconds", type=float, default=4, help="seconds between target changes")
    ap.add_argument("--room-temp", type=float, default=ROOM_TEMP, help="room temperature in C, for the cooling lab")
    ap.add_argument("--station", default=STATION, help='name the TA sees in texts, e.g. "Station 3"')
    ap.add_argument("--notify-url", default=NOTIFY_URL, help="Photon service URL that texts the TA")
    args = ap.parse_args()
    ROOM_TEMP, STATION, NOTIFY_URL = args.room_temp, args.station, args.notify_url
    settings.update(kp=args.kp, ki=args.ki, kd=args.kd)
    stepper.update(low=args.low, high=args.high, seconds=args.step_seconds)

    cooling = None
    if args.fake:
        motor_link, cooling = FakeMotor(), FakeCooling()
        print("Fake boards running. Type 'bad' for a bad controller, 'brake' to press on the wheel,\n"
              "'out'/'in' to move the probe, 'hold 80' for a steady speed, 'ta <message>' to text as the TA.")
    else:
        if args.motor_port:
            motor_link = SerialLink(args.motor_port)
        if args.cooling_port:
            SerialLink(args.cooling_port)

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
            elif cmd.startswith("ta "):
                threading.Thread(target=lambda q=cmd[3:]: print("[reply to TA]", handle_voice(q, "", "ta")),
                                 daemon=True).start()
            elif cmd == "bad" and motor_link:
                settings.update(kp=6.0)
                motor_link.write(f"K,{settings['kp']},{settings['ki']},{settings['kd']}")
                motor_watch.flagged = False
                send({"type": "settings", **settings})
                print("Loaded a bad controller (kp=6)")
            elif cmd in ("out", "in") and cooling:
                cooling.out = cmd == "out"
            elif cmd in ("stop", "start"):
                print(do_action({"type": cmd}, "motor"))
            elif cmd and motor_link:
                motor_link.write(cmd)    # anything else goes straight to the robot, e.g. S,80
    threading.Thread(target=console, daemon=True).start()
    threading.Thread(target=run_stepper, daemon=True).start()
    threading.Thread(target=stuck_watchdog, daemon=True).start()

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
