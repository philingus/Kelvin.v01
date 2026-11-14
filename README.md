# Kelvin.v01

# Kelvin

An AI lab partner that catches mistakes while an experiment is running.

## Data format

```
Robot -> laptop (50 lines/sec):  T,time_ms,target,speed,motor,tilt
Cooling cup -> laptop (1/sec):   C,time_ms,temp_c
Laptop -> robot:                 K,kp,ki,kd     and     S,target_speed
```

Backend -> dashboard over WebSocket ws://localhost:8000/ws, one JSON per message:

```
{"lab":"motor","t":1234,"target":100,"speed":96.2,"motor":140,"tilt":0.4}
{"lab":"cooling","t":1234,"temp":71.3}
{"type":"diagnosis","lab":"motor","issue":"overshoot","text":"...","fix":{"kp":1.2,"ki":0.1,"kd":0}}
```

Dashboard -> backend:

```
{"type":"apply_fix","fix":{"kp":1.2,"ki":0.1,"kd":0}}
```

## Folders

- firmware/minseg: MinSeg robot code (Monika)
- firmware/cooling: cooling cup code (Nirmit)
- backend: Python server (Gissell)
- frontend: dashboard (Phil)