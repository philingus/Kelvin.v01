"""BenchMate's wheel-speed telemetry and diagnosis rules."""
from dataclasses import asdict, dataclass
from math import cos, exp, isfinite
from statistics import mean, pstdev


@dataclass(frozen=True)
class Sample:
    time_ms: int
    target: float
    speed: float
    motor: float
    tilt: float

    @classmethod
    def parse(cls, line):
        marker, timestamp, *values = line.strip().split(",")
        if marker != "T" or len(values) != 4:
            raise ValueError("Expected T,time_ms,target,speed,motor,tilt")
        timestamp = int(timestamp)
        values = tuple(map(float, values))
        if timestamp < 0 or not all(map(isfinite, values)):
            raise ValueError("Invalid telemetry values")
        return cls(timestamp, *values)

    def line(self):
        return "T," + ",".join(map(str, asdict(self).values()))


@dataclass(frozen=True)
class Gains:
    kp: float
    ki: float
    kd: float

    def command(self):
        return f"K,{self.kp:.9g},{self.ki:.9g},{self.kd:.9g}\n"


class FakeRobot:
    def __init__(self, gains):
        self.gains = gains
        self.time_ms = 0
        self.run_start = 0

    def apply(self, gains):
        self.gains = gains
        self.run_start = self.time_ms

    def read(self):
        self.time_ms += 20
        # Repeat a step every six seconds so a dashboard can show successive trials.
        elapsed = ((self.time_ms - self.run_start) % 6000) / 1000
        target = 100.0 if elapsed >= 0.5 else 0.0
        t = elapsed - 0.5
        speed = motor = 0.0
        if target:
            if self.gains.kp >= 1.5:
                speed = target * (1 - exp(-1.35 * t) * cos(7 * t))
            else:
                speed = target * (1 - exp(-4.5 * t))
            motor = min(100.0, max(-100.0, 30 + self.gains.kp * (target - speed)))
        return Sample(self.time_ms, target, speed, motor, 0.0)


def assess(samples):
    """Assess the first three seconds after a nonzero target step."""
    target = samples[0].target
    response = [s.speed / target for s in samples]
    overshoot = max(0.0, max(response) - 1)
    tail = [ratio for s, ratio in zip(samples, response)
            if s.time_ms - samples[0].time_ms >= 2500]
    steady_error = 1 - mean(tail)
    mean_error = mean(abs(1 - ratio) for ratio in response)
    band = [1 if ratio > 1.05 else -1 if ratio < 0.95 else 0 for ratio in response]
    signs = [sign for sign in band if sign]
    crossings = sum(a != b for a, b in zip(signs, signs[1:]))
    motor_delta = [b.motor - a.motor for a, b in zip(samples, samples[1:])
                   if b.time_ms - samples[0].time_ms >= 2000]
    motor_range = max(s.motor for s in samples) - min(s.motor for s in samples)
    motor_jitter = pstdev(motor_delta) / motor_range if len(motor_delta) > 1 and motor_range else 0
    code = "healthy"
    if overshoot >= 0.2 and crossings >= 3:
        code = "overshoot_oscillation"
    elif steady_error > 0.1 and tail[-1] - tail[0] > 0.035:
        code = "slow_response"
    elif steady_error > 0.1 and pstdev(tail) < 0.025:
        code = "steady_state_error"
    elif motor_jitter > 0.12 and motor_range > 0:
        code = "motor_jitter"
    elif mean(abs(1 - ratio) for ratio in tail) > 0.1:
        code = "unclassified"
    return {
        "code": code,
        "target": target,
        "overshoot_pct": round(100 * overshoot, 2),
        "steady_error_pct": round(100 * steady_error, 2),
        "mean_error_pct": round(100 * mean_error, 2),
        "crossings": crossings,
    }


def diagnose(metrics, gains, ki_step):
    code = metrics["code"]
    if code == "overshoot_oscillation":
        return (
            f"The wheel overshot by {metrics['overshoot_pct']:g}% and oscillated. "
            "Try lowering proportional gain (Kp) by a third.",
            Gains(gains.kp * 2 / 3, gains.ki, gains.kd) if gains and gains.kp else None,
        )
    if code == "slow_response":
        return (
            "The wheel is still climbing toward target. Try increasing Kp by 20%.",
            Gains(gains.kp * 1.2, gains.ki, gains.kd) if gains and gains.kp else None,
        )
    if code == "steady_state_error":
        return (
            "The wheel settled below target. A small increase in integral gain (Ki) may reduce the offset.",
            Gains(gains.kp, gains.ki + ki_step, gains.kd) if gains and ki_step else None,
        )
    if code == "motor_jitter":
        return (
            "Motor power is jittery. Check encoder noise and consider reducing derivative gain (Kd).",
            Gains(gains.kp, gains.ki, gains.kd * 0.75) if gains and gains.kd else None,
        )
    if code == "unclassified":
        return "The wheel missed its target without matching a supported failure pattern.", None
    return "The wheel settled close to target.", None


if __name__ == "__main__":
    from time import sleep

    robot = FakeRobot(Gains(1.8, 0.0, 0.0))
    try:
        while True:
            print(robot.read().line(), flush=True)
            sleep(0.02)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
