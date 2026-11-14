# Kelvin

An AI lab partner that watches a student's experiment through live sensor data, catches mistakes while they happen, explains them out loud, applies the fix when the student says so, and checks whether the fix worked. When a student is stuck, Kelvin texts the TA.

Built at HackPrinceton Fall 2026 by Phil, Monika, Gissell and Nirmit.

## The problem

In an intro engineering lab, one TA covers a room of students. When a student's controller overshoots or a sensor reading goes wrong, they stare at a graph, guess at numbers, and wait for the TA. Kelvin gives every bench a partner that knows what the graph means.

## What it does

**Motor control lab (MinSeg robot on a stand).** The student's PID controller holds a wheel at a target speed. Kelvin judges every speed step:

| Kelvin sees | Kelvin says | Fix it proposes |
|---|---|---|
| Overshoot of 25% or more | kp is too high | kp to about a third |
| Wheel never reaches target | kp is too low | kp up by half |
| Settles below target | needs integral action | more ki |
| Motor maxed out most of the step | too aggressive, or low battery | kp down |
| Speed drops at a steady target while motor power climbs | something physical is dragging on the wheel | no gain change, remove the drag |

After a fix is applied, Kelvin skips the step that was already running and judges the next full step, then says whether it worked. If it didn't, it proposes a smaller kp.

**Cooling lab (DS18B20 probe in hot water).** Kelvin fits Newton's law of cooling to the readings as they come in and draws the predicted curve. If the reading falls 6 °C below the prediction, it says the probe is probably out of the water, and it says when the readings match again.

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

Put the AI key in `backend/.env` (this file is in `.gitignore` and must never be committed):

```
GEMINI_API_KEY=your-key
NOTIFY_URL=http://localhost:3000/kelvin-event
```

`NOTIFY_URL` is optional. Leave it out to turn TA texts off.

**No hardware (simulated boards):**

```
python server.py --fake
```

Open http://localhost:8000 in Microsoft Edge. Terminal commands while it runs: `bad` loads a bad controller, `brake` presses on the simulated wheel, `out`/`in` moves the simulated probe, `hold 80` holds a steady speed, `ta how is it going?` texts Kelvin as the TA.

**Real hardware, two laptops.** The robot plugs into the laptop running Kelvin. The cooling probe plugs into a second laptop that forwards its readings:

```
# laptop 1 (robot)
python server.py --motor-port COM5 --station "Station 3" --room-temp 22

# laptop 2 (cooling probe). Use the address laptop 1 prints.
python relay.py --port COM6 --to http://192.168.x.x:8000
```

Close the Arduino Serial Monitor first, since only one program can use a USB port at a time. If laptop 2 can't reach laptop 1, put both on one phone hotspot (venue Wi-Fi often blocks laptops from talking to each other) and allow Python through the Windows firewall.

## How it fits together

```
MinSeg (Arduino Mega) --USB-->  server.py  <--WebSocket-->  dashboard (frontend/index.html)
DS18B20 (Arduino) --USB--> relay.py --HTTP-->  |           voice in/out in the browser
                                               |--HTTP--> Gemini API (explanations, answers, reports)
                                               |<--HTTP--> Photon service --> TA's iMessage
```

Detection is rule-based in `server.py`, so it is fast and predictable. The LLM only rewrites explanations in plain words, answers questions, and picks at most one action from a fixed list. Every action goes through one function that validates the numbers, and Kelvin's spoken confirmation is written from what actually happened, not from what the model claimed.

### Serial protocol

```
robot  -> laptop  (50/s):  T,time_ms,target,speed,motor,tilt
probe  -> laptop  (1/s):   C,time_ms,temp_c
laptop -> robot:           K,kp,ki,kd     and     S,target_speed
lines starting with # are notes and are ignored
```

### HTTP endpoints (server.py)

| Endpoint | Used by | Body |
|---|---|---|
| `GET /` | browser | serves the dashboard |
| `WS /ws` | dashboard | live readings, diagnoses, answers |
| `POST /api/ask` | Photon service | `{"question": "...", "from": "ta"}` returns `{"text": "..."}` |
| `POST /api/lines` | relay.py | `{"lines": ["C,1000,71.2", ...]}` |
| `GET /health` | anyone | status check |

Kelvin POSTs TA events to `NOTIFY_URL` as `{"event": "stuck" | "approval" | "report" | "resolved", "station": "...", "text": "..."}`.

## Repo layout

- `backend/server.py`: Kelvin's backend (serial, detection, voice commands, AI, TA texting, dashboard server)
- `backend/relay.py`: forwards a board on a second laptop to Kelvin
- `backend/test_ai.py`: times one Gemini call per model, to pick the fastest on the venue Wi-Fi
- `backend/main.py`, `backend/controller.py`: an earlier REST version of the motor backend (see `backend/README.md`); the demo runs `server.py`
- `frontend/index.html`: the dashboard, a single file
- `firmware/minseg/`: MinSeg wheel speed sketch
- `firmware/cooling/kelvin_cooling/`: DS18B20 cooling probe sketch

## Built with

We used these libraries and services:

- [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [websockets](https://websockets.readthedocs.io/) for the backend
- [pySerial](https://pyserial.readthedocs.io/) for USB serial
- [Google Gemini API](https://ai.google.dev/) for explanations, answers, voice command parsing and reports
- [Photon 2.0](https://photon.codes/beta) for iMessage texting with the TA
- Web Speech API (built into Edge and Chrome) for speech recognition and the spoken voice
- Arduino libraries: [OneWire](https://github.com/PaulStoffregen/OneWire), [DallasTemperature](https://github.com/milesburton/Arduino-Temperature-Control-Library)
- Google Fonts: Fraunces, Caveat, IBM Plex Sans, IBM Plex Mono
- MinSeg robot kit (Arduino Mega 2560)

We used AI coding assistants while building.
