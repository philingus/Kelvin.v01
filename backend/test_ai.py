"""Times one Gemini call per model so we can see which is fast on this Wi-Fi. Run: python test_ai.py"""
import json, os, socket, time, urllib.error, urllib.request

env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env):
    for line in open(env, encoding="utf-8-sig"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
key = os.environ.get("GEMINI_API_KEY")
print("key found:", bool(key), (key or "")[:6] + "...")

t = time.time()
try:
    socket.create_connection(("generativelanguage.googleapis.com", 443), timeout=10).close()
    print(f"network: reached Google in {time.time() - t:.1f}s")
except Exception as e:
    print("network: can't reach Google:", e)

prompt = "In one short sentence, say hello to a student doing a lab."
for model in ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-flash-latest"]:
    for label, cfg in [("minimal thinking", {"thinkingConfig": {"thinkingLevel": "minimal"}}), ("default", None)]:
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        if cfg:
            body["generationConfig"] = cfg
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            text = " ".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
            print(f"OK    {time.time() - t:5.1f}s  {model} ({label}): {text.strip()[:70]}")
        except urllib.error.HTTPError as e:
            print(f"FAIL  {time.time() - t:5.1f}s  {model} ({label}): HTTP {e.code} {e.read()[:150]}")
        except Exception as e:
            print(f"FAIL  {time.time() - t:5.1f}s  {model} ({label}): {e}")