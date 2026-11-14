"""Run with uvicorn main:app from the backend directory."""
import asyncio
import csv
import json
import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from fastapi.middleware.cors import CORSMiddleware

from controller import FakeRobot, Gains, Sample, assess, diagnose


log = logging.getLogger(__name__)

def configured_gains(name):
    value = os.getenv(name)
    if not value:
        return None
    gains = Gains(*(float(part) for part in value.split(",")))
    if not all(0 <= gain < float("inf") for gain in asdict(gains).values()):
        raise ValueError(f"{name} requires three finite nonnegative gains")
    return gains


def replay_samples(path):
    with Path(path).open(newline="") as stream:
        for row in csv.reader(stream):
            if row and row[0] == "T":
                yield Sample.parse(",".join(row))


class Bench:
    def __init__(self):
        self.mode = os.getenv("BENCH_MODE", "fake")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.samples = deque(maxlen=500)
        self.cursor = 0
        self.trial = []
        self.previous = None
        self.diagnosis = self.proposal = self.verification = None
        self.baseline = None
        self.status = "starting"
        self.serial = None
        self.robot = None
        self.gains = configured_gains("BENCH_INITIAL_GAINS")
        self.limits = configured_gains("BENCH_MAX_GAINS")
        self.ki_step = float(os.getenv("BENCH_KI_STEP", "0"))
        self.ai_url = os.getenv("BENCH_AI_URL")
        self.ai_model = os.getenv("BENCH_AI_MODEL")
        self.ai_key = os.getenv("BENCH_AI_KEY")
        self.ai = ThreadPoolExecutor(max_workers=1, thread_name_prefix="explanation")
        self.ai_job = None
        if self.mode == "fake":
            self.gains = self.gains or Gains(1.8, 0.0, 0.0)
            self.limits = self.limits or Gains(4.0, 1.0, 1.0)
            self.robot = FakeRobot(self.gains)

    def run(self):
        try:
            if self.mode == "serial":
                self.read_serial()
            elif self.mode == "replay":
                self.read_replay()
            elif self.mode == "fake":
                self.set_status("streaming")
                while not self.stop.wait(0.02):
                    self.ingest(self.robot.read())
            else:
                raise ValueError("BENCH_MODE must be fake, replay or serial")
        except Exception:
            log.exception("Telemetry source stopped")
            self.set_status("source_error")

    def set_status(self, value):
        with self.lock:
            self.status = value
            if value != "streaming":
                self.proposal = None

    def read_serial(self):
        import serial

        with serial.Serial(os.environ["BENCH_PORT"], int(os.getenv("BENCH_BAUD", "115200")),
                           timeout=0.25, write_timeout=0.25) as port:
            with self.lock:
                self.serial = port
                self.status = "streaming"
            try:
                pending = bytearray()
                while not self.stop.is_set():
                    chunk = port.read_until(b"\n", size=256)
                    if not chunk:
                        continue
                    pending.extend(chunk)
                    if len(pending) <= 256 and chunk.endswith(b"\n"):
                        try:
                            self.ingest(Sample.parse(pending.decode("ascii")))
                        except ValueError:
                            pass
                    if chunk.endswith(b"\n"):
                        pending.clear()
                    elif len(pending) > 256:
                        pending[:] = b" " * 256
            finally:
                with self.lock:
                    self.serial = None
                    self.proposal = None

    def read_replay(self):
        self.set_status("streaming")
        last_ms = None
        for sample in replay_samples(os.environ["BENCH_REPLAY_FILE"]):
            if last_ms is not None and self.stop.wait(max(0, sample.time_ms - last_ms) / 1000):
                return
            self.ingest(sample)
            last_ms = sample.time_ms
        self.set_status("replay_complete")

    def ingest(self, sample):
        with self.lock:
            self.cursor += 1
            self.samples.append({"seq": self.cursor, **asdict(sample)})
            previous = self.previous
            self.previous = sample
            if previous and sample.time_ms <= previous.time_ms:
                self.trial = []
                self.proposal = self.baseline = self.diagnosis = self.verification = None
                return
            if previous and sample.target != previous.target:
                self.trial = []
                self.proposal = None
                if abs(sample.target) > 1e-9:
                    self.trial = [sample]
                    self.diagnosis = None
            elif self.trial:
                self.trial.append(sample)

            if self.trial and sample.time_ms - self.trial[0].time_ms >= 3000:
                samples, self.trial = self.trial, []
                duration = samples[-1].time_ms - samples[0].time_ms
                max_gap = max(b.time_ms - a.time_ms for a, b in zip(samples, samples[1:]))
                if duration <= 3200 and max_gap <= 200:
                    self.finish_trial(samples)

    def finish_trial(self, samples):
        metrics = assess(samples)
        if self.baseline and self.baseline["target"] == metrics["target"]:
            before = self.baseline["mean_error_pct"]
            after = metrics["mean_error_pct"]
            self.verification = {
                "result": "improved" if after < before - 1 else
                "worse" if after > before + 1 else "similar",
                "before_mean_error_pct": before,
                "after_mean_error_pct": after,
            }
            self.baseline = None
        message, candidate = diagnose(metrics, self.gains, self.ki_step)
        diagnosis_id = uuid4().hex
        self.diagnosis = {"id": diagnosis_id, **metrics, "message": message, "source": "rule"}
        self.proposal = None
        if (self.mode != "replay" and candidate and self.limits and candidate != self.gains
                and all(0 <= gain <= limit for gain, limit in
                        zip(asdict(candidate).values(), asdict(self.limits).values()))):
            self.proposal = {"id": diagnosis_id, "gains": asdict(candidate)}
        if (self.ai_url and self.ai_model and metrics["code"] != "healthy"
                and (self.ai_job is None or self.ai_job.done())):
            self.ai_job = self.ai.submit(self.explain, diagnosis_id, metrics, message, samples[::10])

    def explain(self, diagnosis_id, metrics, message, samples):
        context = {"metrics": metrics, "rule_explanation": message,
                   "samples": [asdict(sample) for sample in samples]}
        payload = {"model": self.ai_model, "messages": [
            {"role": "system", "content": (
                "Explain this wheel-speed diagnosis in one or two plain sentences. "
                "Stick to the supplied measurements and treat the tuning cause as a hypothesis. "
                "Do not suggest new gain values or claim any change has been applied."
            )},
            {"role": "user", "content": json.dumps(context)},
        ]}
        headers = {"Content-Type": "application/json"}
        if self.ai_key:
            headers["Authorization"] = f"Bearer {self.ai_key}"
        try:
            request = Request(self.ai_url, data=json.dumps(payload).encode(), headers=headers)
            with urlopen(request, timeout=8) as response:
                result = json.loads(response.read(65536))
            explanation = result["choices"][0]["message"]["content"].strip()
            if 0 < len(explanation) <= 500:
                with self.lock:
                    if self.diagnosis and self.diagnosis["id"] == diagnosis_id:
                        self.diagnosis.update(message=explanation, source="ai")
        except Exception:
            log.warning("AI explanation unavailable. Using rule explanation.")

    def approve(self, proposal_id):
        with self.lock:
            if not self.proposal or self.proposal["id"] != proposal_id:
                raise HTTPException(409, "Proposal expired or already applied")
            if self.status != "streaming" or (not self.robot and not self.serial):
                raise HTTPException(503, "Robot is disconnected")
            gains = Gains(**self.proposal["gains"])
            self.proposal = None
            if self.robot:
                self.robot.apply(gains)
            else:
                command = gains.command().encode("ascii")
                try:
                    if self.serial.write(command) != len(command):
                        raise OSError("Incomplete gain command")
                except OSError as error:
                    self.gains = None
                    raise HTTPException(503, "Gain write uncertain. Check firmware before retrying.") from error
            self.gains = gains
            self.baseline = self.diagnosis
            self.verification = {"result": "awaiting_next_run"}
            self.trial = []
            return {"status": "sent" if self.mode == "serial" else "applied", "gains": asdict(gains)}

    def snapshot(self, after):
        with self.lock:
            return {
                "mode": self.mode, "status": self.status, "cursor": self.cursor,
                "samples": [sample for sample in self.samples if sample["seq"] > after],
                "diagnosis": self.diagnosis.copy() if self.diagnosis else None,
                "proposal": self.proposal,
                "verification": self.verification,
            }


@asynccontextmanager
async def lifespan(app):
    bench = app.state.bench = Bench()
    reader = threading.Thread(target=bench.run, name="telemetry", daemon=True)
    reader.start()
    try:
        yield
    finally:
        bench.stop.set()
        reader.join(timeout=1)
        bench.ai.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="BenchMate Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("BENCH_FRONTEND_ORIGINS",
                            "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/state")
def state(after: int = 0):
    return app.state.bench.snapshot(after)


@app.post("/api/proposals/{proposal_id}/approve")
def approve(proposal_id: str):
    return app.state.bench.approve(proposal_id)


async def receive_fixes(websocket: WebSocket, bench: Bench):
    while True:
        try:
            message = await websocket.receive_json()
        except json.JSONDecodeError:
            continue

        if not isinstance(message, dict) or message.get("type") != "apply_fix":
            continue

        proposal = bench.snapshot(0)["proposal"]
        if proposal is None or message.get("fix") != proposal["gains"]:
            continue

        try:
            await asyncio.to_thread(bench.approve, proposal["id"])
        except HTTPException as error:
            log.warning("Apply Fix rejected: %s", error.detail)

@app.websocket("/ws")
async def dashboard_stream(websocket: WebSocket):
    await websocket.accept()
    bench = websocket.app.state.bench
    cursor = bench.snapshot(0)["cursor"]
    last_diagnosis_id = None
    last_verification = None
    receiver = asyncio.create_task(receive_fixes(websocket, bench))

    try:
        while True:
            if receiver.done():
                receiver.result()

            state = bench.snapshot(cursor)
            for sample in state["samples"]:
                await websocket.send_json({
                    "lab": "motor",
                    "t": sample["time_ms"],
                    "target": sample["target"],
                    "speed": sample["speed"],
                    "motor": sample["motor"],
                    "tilt": sample["tilt"],
                })
            cursor = state["cursor"]

            diagnosis = state["diagnosis"]
            proposal = state["proposal"]

            # Check for overshoot
            verification = state["verification"]
            if verification != last_verification:
                last_verification = verification
                if verification and verification["result"] != "awaiting_next_run":
                    result = verification["result"]
                    before = verification["before_mean_error_pct"]
                    after = verification["after_mean_error_pct"]
                    summary = {
                        "improved": "The fix helped.",
                        "similar": "The next run was about the same.",
                        "worse": "Tracking got worse after the fix.",
                    }[result]

                    await websocket.send_json({
                        "type": "diagnosis",
                        "lab": "motor",
                        "issue": "tracking_error" if result == "worse" else "ok",
                        "text": (
                            f"{summary} Average tracking error went "
                            f"from {before:.1f}% to {after:.1f}%."
                        ),
                        "fix": proposal["gains"] if proposal else None,
                    })
                    if diagnosis:
                        last_diagnosis_id = diagnosis["id"]
            if (
                diagnosis
                and diagnosis["code"] == "overshoot_oscillation"
                and diagnosis["id"] != last_diagnosis_id
            ):
                await websocket.send_json({
                    "type": "diagnosis",
                    "lab": "motor",
                    "issue": "overshoot",
                    "text": diagnosis["message"],
                    "fix": proposal["gains"] if proposal else None,
                })
                last_diagnosis_id = diagnosis["id"]

            await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        pass
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)