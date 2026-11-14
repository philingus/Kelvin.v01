# Kelvin.v01 backend

Copy this `backend/` folder into the root of Kelvin.v01. Start in fake mode so the React dashboard can connect before the MinSeg is ready.

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app
```

Open http://127.0.0.1:8000/docs to try the routes. Use a single Uvicorn worker so one process owns the robot.

## Dashboard contract

- Poll `GET /api/state?after=0` every 100–200 ms. Append the returned `samples` and pass the returned `cursor` as `after` on the next poll. Reset the cursor on backend restart. Each sample has `seq`, `time_ms`, `target`, `speed`, `motor` and `tilt`. The server holds the latest 500 samples.
- Render `diagnosis.message` and show the approve button when `proposal` is present. A proposal contains `id` and `gains` with `kp`, `ki` and `kd`. A diagnosis appears about three seconds after a target step. An optional AI explanation can update the message for the same diagnosis ID.
- On click, call `POST /api/proposals/{id}/approve` with an empty body. A 409 means the proposal expired or was consumed. Refresh state. Display `verification.result` after the next three-second run at the same target. It compares mean absolute tracking error as a percentage of target.
- CORS defaults to localhost and 127.0.0.1 on port 5173. For another frontend origin, set `BENCH_FRONTEND_ORIGINS` to a comma-separated list.

## Fake robot and replay

Fake mode automatically repeats a speed step every six seconds. It starts with an oscillating response. Approve the proposal to see the next simulated run settle and the verification update. The fake robot uses scripted curves to exercise the demo flow.

Capture fake telemetry with `python controller.py > run.csv` and stop it after several seconds with Ctrl+C. Run a saved log with:

```sh
BENCH_MODE=replay BENCH_REPLAY_FILE=run.csv python -m uvicorn main:app
```

Replay accepts recorded `T,time_ms,target,speed,motor,tilt` lines and uses their original time offsets. Include telemetry from before the target step so the backend can detect the change. Replay runs once and ends with `replay_complete`.

## USB integration

```sh
python -m serial.tools.list_ports
BENCH_MODE=serial BENCH_PORT=/dev/cu.usbmodemXXXX BENCH_BAUD=115200 python -m uvicorn main:app
```

The reader expects newline-terminated ASCII telemetry at roughly 50 Hz. It uses the plan's `T,time_ms,target,speed,motor,tilt` format. Hardware proposals require `BENCH_INITIAL_GAINS=kp,ki,kd` set to the firmware's actual starting gains and `BENCH_MAX_GAINS=kp,ki,kd` set to the team's verified upper limits. To enable a Ki proposal, also set `BENCH_KI_STEP` to a positive increment in the firmware's gain units.

Approval writes `K,kp,ki,kd\n`. The draft firmware protocol has no acknowledgment, so serial mode reports `sent` after the write. Trigger the next same-target step through the firmware to verify the response. The backend currently evaluates wheel-speed steps only.

## Optional AI explanation

Set `BENCH_AI_URL` to a chat-completions endpoint, `BENCH_AI_MODEL` to its model ID and `BENCH_AI_KEY` in your local shell if the endpoint requires it. The request includes diagnosis metrics and recent telemetry. The rule-based message appears immediately and remains available when the AI endpoint fails. Voice and webcam integration are pending.

## Add to Git

From your local Kelvin.v01 checkout, after copying the folder:

```sh
git add backend/
git diff --cached --stat
git commit -m "Add wheel-speed backend"
```
