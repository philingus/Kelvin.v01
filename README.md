# Kelvin

An AI lab partner that watches a student's experiment through live sensor data, catches mistakes while they happen, explains them out loud, applies the fix when the student says so, and checks whether the fix worked. When a student is stuck, Kelvin texts the TA.

Built at HackPrinceton Fall 2026 by Phil, Monika, Gissell and Nirmit.


## Calibration lab (main demo)

The robot runs `firmware/minseg/minseg_balance/minseg_balance.ino` and is held in the student's hands. Before a balancing robot can balance, its tilt sensor has to learn which way is straight up. The board's own check only rejects a calibration past 15 degrees forward or back and never checks sideways, so Kelvin judges every calibration again:

| Mistake | How Kelvin knows |
|---|---|
| Calibrated while leaning | Average forward/back tilt over 4 degrees |
| Calibrated while tipped sideways | Average sideways tilt over 5 degrees |
| Moved while calibrating | The board rejected it: the angle wobbled over 1 degree, or it was turning |

Say "Kelvin, calibrate" to calibrate, "arm" to turn the motor on, "stop" to turn it off. After a bad calibration, "fix it" or the Calibrate again button recalibrates, and Kelvin reports how far off it was before and after. Every calibration is logged to `backend/logs/calib_*.csv`.

```
python server.py --calib-port COM17
```

Simulator: `python server.py --fake --lab calib`, then type `pose 9 0` (lean), `pose 0 12` (sideways), `pose 0 0` (straight), `shake`.

## The problem

In an intro engineering lab, one TA covers a room of students. When a student's controller overshoots or a sensor reading goes wrong, they stare at a graph, guess at numbers, and wait for the TA. Kelvin gives every bench a partner that knows what the graph means.

## What it does

**Motor control lab (MinSeg robot on a stand).** The student's PID controller holds a wheel at a target speed (RPM). Kelvin steps the target up and down and judges every step:

| Kelvin sees | Kelvin says | Fix it proposes |
|---|---|---|
| Overshoot of 20% or more, with integral action on | ki is too high for kp, so it keeps pushing after the wheel reaches speed | ki down to about 1.5 times kp (measured on our motor) |
| Overshoot of 20% or more, no integral action | kp is too high | kp to about a third |
| Wheel never reaches target | kp is too low | kp up by half |
| Settles below target | needs integral action | more ki |
| Motor maxed out most of the step | too aggressive, or low battery | kp down |
| Speed drops at a steady target while motor power climbs | something physical is dragging on the wheel | no gain change, remove the drag |
| Speed holds, but only because motor power jumped 20% or more | something is loading the wheel | no gain change, remove the load |

After a fix is applied, Kelvin skips the step that was already running and judges the next full speed-up, then says whether it worked. If it didn't, it proposes cutting the same setting in half again.

**Cooling lab (DS18B20 probe in hot water).** Kelvin fits Newton's law of cooling to the readings as they come in and draws the predicted curve. It runs two checks so a mistake is caught in seconds:

- **Rate:** the reading starts falling at least 3 times faster than the probe was cooling a moment ago. Hot water can't do that, so the probe is out of the water or in something colder.
- **Offset:** the reading sits 3 °C or more below or above the predicted curve.

It says when the readings head back to the curve. Every reading from both labs is saved to `backend/logs/` as CSV.

**Talk to it.** Click the mic once and say "Kelvin, ..." Examples: "apply it", "can we do a third of that instead", "undo", "hold the speed at 80", "switch to cooling", "give me my report", "send my report to the TA". Kelvin checks every number against a safe range before anything reaches the robot.

**TA texting (Photon iMessage).** The student talks to Kelvin at the bench, the TA texts Kelvin from their phone, and both share one memory. Kelvin texts the TA only when it matters:

- a mistake has been open for 2 minutes, or two fixes in a row failed
- a student asks for settings outside the safe range (the TA replies OK or NO)
- a student sends their lab report
- a problem the TA was told about got fixed

The TA can also text questions ("how is station 3 doing?") or commands ("stop the motor").

## Run it

Needs Python 3.10+.

```
cd backend
pip install -r requirements.txt
```

Put the keys in `backend/.env` (this file is in `.gitignore` and must never be committed):

```
GEMINI_API_KEY=your-key
PHOTON_API_KEY=your-photon-key
PHOTON_PROJECT_ID=your-project-id
PHOTON_SENDER_ID=the-agent-sender-id
PHOTON_TA_SPACE_ID=the-conversation-with-the-TA
```

The four `PHOTON_` lines are optional. Leave them out and Kelvin runs with TA texts off. `backend/photon/photon_test.py` prints the sender and space IDs for a new Photon project.

**No hardware (simulated boards):**

```
python server.py --fake
```

Open http://localhost:8000 in Microsoft Edge. Terminal commands while it runs: `bad` loads a bad controller, `brake` presses on the simulated wheel, `out`/`in` moves the simulated probe, `hold 80` holds a steady speed, `ta how is it going?` texts Kelvin as the TA.

**Real hardware.** Flash the two sketches first (Arduino IDE):

| Board | Sketch | Notes |
|---|---|---|
| MinSeg (Arduino Mega 2560) | `firmware/minseg/minseg_speed/minseg_speed.ino` | Motor port 1 (D44/D45), encoder on D19/D18. Power from the robot's own batteries with both the Battery and Driver switches ON. Nothing else plugged in. |
| Cooling probe (Arduino Uno) | `firmware/cooling/kelvin_cooling/kelvin_cooling.ino` | DS18B20 data wire on the pin set in `PROBE_PIN`, with a 4.7k resistor from that pin to 5V. Needs the OneWire and DallasTemperature libraries. |

Then, with both boards on one laptop:

```
python server.py --motor-port COM17 --cooling-port COM16 --room-temp 20 --station "Station 3"
```

Use your own COM port numbers (Arduino IDE, Tools > Port). Add `--kp 1 --ki 40` to start with a badly tuned controller. Close the Arduino Serial Monitor first, since only one program can use a USB port at a time. Ctrl+C stops the wheel. If a board is unplugged, Kelvin says so and reconnects when it's back.

**Two laptops (optional).** A board on a second laptop can forward its readings:

```
python relay.py --port COM6 --to http://192.168.x.x:8000
```

Use the address the first laptop prints at startup. If the second laptop can't reach it, put both on one phone hotspot (venue Wi-Fi often blocks laptops from talking to each other) and allow Python through the Windows firewall.

## How it fits together

```
MinSeg (Arduino Mega) --USB-->  server.py  <--WebSocket-->  dashboard (frontend/index.html)
DS18B20 (Arduino) --USB--> relay.py --HTTP-->  |           voice in/out in the browser
                                               |--HTTP--> Gemini API (explanations, answers, reports)
                                               |<--HTTPS--> Photon 2.0 API <--> TA's iMessage
```

The speed controller itself runs on the robot (`minseg_speed.ino`, a PI/PID loop at 50 Hz with anti-windup). Kelvin changes its settings over USB. Detection is rule-based in `server.py`, so it is fast and predictable. The LLM only rewrites explanations in plain words, answers questions, and picks at most one action from a fixed list. Every action goes through one function that validates the numbers, and Kelvin's spoken confirmation is written from what actually happened, not from what the model claimed.

### Serial protocol

```
robot  -> laptop  (50/s):  T,time_ms,target_rpm,speed_rpm,motor_pwm,0
probe  -> laptop  (1/s):   C,time_ms,temp_c
laptop -> robot:           K,kp,ki,kd     and     S,target_rpm
lines starting with # are notes and are ignored
```

### HTTP endpoints (server.py)

| Endpoint | Used by | Body |
|---|---|---|
| `GET /` | browser | serves the dashboard |
| `WS /ws` | dashboard | live readings, diagnoses, answers |
| `POST /api/ask` | any other client | `{"question": "...", "from": "ta"}` returns `{"text": "..."}` |
| `POST /api/lines` | relay.py | `{"lines": ["C,1000,71.2", ...]}` |
| `GET /health` | anyone | status check |

TA texting goes through the Photon 2.0 Python SDK in `backend/photon/ta_report.py`: Kelvin sends messages, checks that each one was delivered, and polls for the TA's replies. The dashboard's "TA texts" tab shows both directions with delivery status.

## Repo layout

- `backend/server.py`: Kelvin's backend (serial, detection, voice commands, AI, TA texting, dashboard server)
- `backend/photon/ta_report.py`: sends and receives TA texts through Photon
- `backend/relay.py`: forwards a board on a second laptop to Kelvin
- `backend/test_ai.py`: times one Gemini call per model, to pick the fastest on the venue Wi-Fi
- `backend/main.py`, `backend/controller.py`: a REST version of the motor backend we built first at the event (see `backend/README.md`); the demo runs `server.py`
- `frontend/index.html`: the dashboard, a single file
- `firmware/minseg/minseg_speed/`: MinSeg wheel speed controller. `minseg_test/` is a one-shot wiring check.
- `backend/logs/`: CSV recordings of each run (not committed)
- `firmware/cooling/kelvin_cooling/`: DS18B20 cooling probe sketch

## Built with

We used these libraries and services:

- [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [websockets](https://websockets.readthedocs.io/) for the backend
- [pySerial](https://pyserial.readthedocs.io/) for USB serial
- [Google Gemini API](https://ai.google.dev/) for explanations, answers, voice command parsing and reports
- [Photon 2.0](https://photon.codes/beta) and its Python SDK (`photonhq-api`) for iMessage texting with the TA
- Web Speech API (built into Edge and Chrome) for speech recognition and the spoken voice
- Arduino libraries: [OneWire](https://github.com/PaulStoffregen/OneWire), [DallasTemperature](https://github.com/milesburton/Arduino-Temperature-Control-Library)
- Google Fonts: Fraunces, Caveat, IBM Plex Sans, IBM Plex Mono
- MinSeg robot kit (Arduino Mega 2560)

We used AI coding assistants while building.
