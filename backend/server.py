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
from photon.ta_report import send_ta_report
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
    recent["events"].append(line)
    del recent["events"][:-8]
    session["events"].append(line)
    del session["events"][:-200]


# ---------------------------------------------------------------- voice commands
# Kelvin can act, not just talk. Every action goes through do_action(), which checks the numbers
# before touching the robot, and Kelvin's spoken reply is written from what actually happened.

SAFE = {"kp": (0.01, 20.0), "ki": (0.0, 20.0), "kd": (0.0, 5.0)}
pending_fix = {"motor": None, "cooling": None}   # the suggested fix waiting for "apply"
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
    session["applied"] += 1
    remember_event("motor", f"student {why} {fmt_fix(new)}, settings now kp {settings['kp']:g} ki {settings['ki']:g} kd {settings['kd']:g}")
    print("[fix applied]", settings)
    send({"type": "settings", **settings})
    send({"type": "applied", "lab": "motor", "fix": new, "before": before})


def clean_fix(a: dict):
    """Pull kp/ki/kd numbers out of an action. Returns (values, problem)."""
    vals = {}
    for k in ("kp", "ki", "kd"):
        if a.get(k) is None:
            continue
        try:
            v = round(float(a[k]), 3)
        except (TypeError, ValueError):
            return None, f"I couldn't understand the number for {k}."
        lo, hi = SAFE[k]
        if not lo <= v <= hi:
            return None, f"{k} {v:g} is outside the safe range of {lo:g} to {hi:g}, so I left it alone."
        vals[k] = v
    if not vals:
        return None, "I couldn't tell which setting you want to change."
    return vals, None


def do_action(a: dict, lab: str) -> str:
    """Carry out one action and return what Kelvin should say about it."""
    t = a.get("type")
    if t == "apply_fix":
        f = pending_fix["motor"]
        if not f:
            return "There's no fix waiting right now. Tell me a setting, like set kp to 1, and I'll line it up."
        apply_settings(f)
        return f"Done. {fmt_fix(f)} is on the robot. I'll watch the next run and tell you if it worked."
    if t == "set_fix":
        vals, problem = clean_fix(a)
        if problem:
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
        threading.Thread(target=send_report, daemon=True).start()
        return "Here's your lab report. You can save it as a PDF."
    return "I'm not able to do that one yet."


def quick_intent(q: str, lab: str = ""):
    """Common commands we can catch instantly, without waiting for the AI."""
    s = q.lower()
    measured = re.search(r"\d", s) or re.search(MEASURE_WORDS, s)
    if re.search(r"\b(undo|revert|put it back|change it back)\b|\bgo back\b(?!\s+to\b)", s):
        return {"type": "undo"}
    if re.search(r"\b(show|give|make|create|generate|send|open|write|get|pull up|need|want|see)\b.*\b(report|summary|recap)\b|\bsummari[sz]e\b", s):
        return {"type": "report"}
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


def handle_voice(question: str, lab: str):
    """A student spoke or typed to Kelvin. Do what they asked, or answer the question."""
    question = question.strip()[:300]
    if not question:
        return
    action = quick_intent(question, lab)
    say = None
    if not action:
        context = {
            "lab_on_screen": lab,
            "controller_settings_on_robot": settings,
            "suggested_fix_waiting": pending_fix["motor"],
            "target_speed_steps_between": [stepper["low"], stepper["high"]] if stepper["on"] else "motor stopped",
            "motor_latest": recent["motor"],
            "cooling_latest": recent["cooling"],
            "what_kelvin_noticed_so_far": recent["events"] or ["nothing wrong yet"],
        }
        prompt = (
            "You are Kelvin, a friendly lab partner watching a student's experiment through live sensor data. "
            "Labs: 'motor' is a robot wheel whose speed is held at a target by the student's controller "
            "(settings kp, ki, kd). 'cooling' is a cup of hot water with a temperature probe; 'expected' is what "
            "Newton's law of cooling predicts.\n"
            "Decide whether the student wants you to DO something. You can take at most one of these actions:\n"
            '- {"type":"apply_fix"}: send the suggested fix that is waiting to the robot.\n'
            '- {"type":"set_fix","kp":number,"ki":number,"kd":number,"apply":bool}: change the suggested fix. '
            "Include only the settings that change. Relative requests like 'a third of that', 'half', 'a bit lower' "
            "are relative to the suggested fix waiting if there is one, otherwise to the settings on the robot; "
            "work out the actual number. Set apply to true only if they also asked to apply or send it now.\n"
            '- {"type":"undo"}: go back to the previous settings.\n'
            '- {"type":"switch_lab","lab":"motor" or "cooling"}: show the other lab.\n'
            '- {"type":"set_target","value":number}: change the target speed the wheel steps up to.\n'
            '- {"type":"stop"} or {"type":"start"}: stop or restart the motor.\n'
            '- {"type":"report"}: show a written summary report of this session.\n'
            "If they are asking a question or chatting, action is null and you answer in say: at most three "
            "short sentences that will be read out loud, plain words, no markdown, use the live numbers when "
            "they help. You cannot save files, send emails, or do anything not listed above, so never claim to. "
            "If you don't know, say so.\n"
            'Reply with JSON only: {"say": "...", "action": null or one action object}\n'
            f"Live context: {json.dumps(context)}\n"
            f"Student says: {question}"
        )
        reply = parse_json(ask_llm(prompt, timeout=12, max_tokens=250, json_mode=True))
        if reply:
            action = reply.get("action") if isinstance(reply.get("action"), dict) else None
            say = str(reply.get("say") or "").strip() or None
        else:
            action = backup_intent(question)
            if not action:
                say = ("I can't reach my AI right now. I can still apply a fix, undo, switch labs, "
                       "change the speed, or show your report.")
    text = do_action(action, lab) if action else (say or "Sorry, I didn't catch that.")
    print(f"[voice] {question} -> action={action} -> {text}")
    send({"type": "answer", "question": question, "text": text})


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


def send_report():
    send(build_report())


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
    if issue in ("fixed", "probe_back"):
        pending_fix[lab] = None
        session["fixed"] += 1
    else:
        session["mistakes"] += 1
        if fix:
            pending_fix[lab] = fix

    def worker():
        final = ai_rewrite(issue, facts, text) if issue not in ("fixed", "probe_back") else text
        msg = {"type": "diagnosis", "lab": lab,
               "issue": "ok" if issue in ("fixed", "probe_back") else issue, "text": final}
        if fix:
            msg["fix"] = fix
        print(f"[diagnosis] {lab} {issue}: {final}  fix={fix}")
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
        self.samples.append((speed, motor))

    def finish(self):
        if self.skip_next:
            self.skip_next = False
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
    if pending_fix["motor"]:
        await ws.send_text(json.dumps({"type": "fix_update", "lab": "motor", "fix": pending_fix["motor"]}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "apply_fix" and isinstance(msg.get("fix"), dict):
                vals, problem = clean_fix(msg["fix"])
                if vals:
                    apply_settings(vals)
                else:
                    send({"type": "answer", "question": "Apply fix", "text": problem})
            elif msg.get("type") == "report":
                threading.Thread(target=send_report, daemon=True).start()
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

def main():
    global motor_link
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
    args = ap.parse_args()
    settings.update(kp=args.kp, ki=args.ki, kd=args.kd)
    stepper.update(low=args.low, high=args.high, seconds=args.step_seconds)

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
            elif cmd in ("stop", "start"):
                print(do_action({"type": cmd}, "motor"))
            elif cmd and motor_link:
                motor_link.write(cmd)    # anything else goes straight to the robot, e.g. S,80
    threading.Thread(target=console, daemon=True).start()
    threading.Thread(target=run_stepper, daemon=True).start()

    ai = "Gemini" if os.environ.get("GEMINI_API_KEY") else "Claude" if os.environ.get("ANTHROPIC_API_KEY") else None
    print("\n  Dashboard: open http://localhost:8000 in Edge")
    print(f"  AI: {ai} key found" if ai else "  AI: NO KEY SET. Kelvin will use plain templates and can't answer questions well.")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()