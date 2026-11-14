"""
Photon: Kelvin and the TA over iMessage.

send_ta_report(summary)  -> text the lab report to the TA        (Send to TA button)
send_ta_text(text)       -> text anything to the TA              (stuck alerts, approvals, replies)
poll_ta(answer)          -> watch for the TA's texts, answer each one in the same conversation

Settings come from backend/.env (never committed):
    PHOTON_API_KEY, PHOTON_PROJECT_ID, PHOTON_TA_SPACE_ID, PHOTON_SENDER_ID
Run photon/photon_test.py once to find PHOTON_TA_SPACE_ID and PHOTON_SENDER_ID.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

NEEDED = ("PHOTON_API_KEY", "PHOTON_PROJECT_ID", "PHOTON_TA_SPACE_ID", "PHOTON_SENDER_ID")

# server.py sets this so the dashboard can show every TA text and whether it was delivered.
# Called as on_event(direction "in"/"out", text, id, status, reason).
on_event = None


def _emit(*args):
    if on_event:
        try:
            on_event(*args)
        except Exception:
            pass


def photon_missing():
    return [name for name in NEEDED if not os.getenv(name)]


def photon_ready():
    """True when every Photon setting is present, so Kelvin can text the TA."""
    return not photon_missing()


def _client():
    from photon_api import Photon
    return Photon(headers={"Authorization": f"Bearer {os.environ['PHOTON_API_KEY']}"})


def _send(photon, text, reply_to=None):
    from photon_api.generated.models import SendMessageRequest, SendTextContent
    from photon_api.rpc_generated import SendMessageHeader, SendMessageInput, SendMessagePath

    body = dict(
        spaceId=os.environ["PHOTON_TA_SPACE_ID"],
        senderId=os.environ["PHOTON_SENDER_ID"],
        content=SendTextContent(type="text", format="plain", text=text),
    )
    if reply_to:
        body["parentMessageId"] = reply_to
    local = "k" + uuid4().hex[:10]
    _emit("out", text, local, "sending", "")
    try:
        sent = photon.projects.send_message(
            SendMessageInput(
                path=SendMessagePath(id=os.environ["PHOTON_PROJECT_ID"]),
                headers=SendMessageHeader.model_validate({"idempotency-key": str(uuid4())}),
                body=SendMessageRequest(**body),
            )
        )
    except Exception as e:
        _emit("out", text, local, "failed", type(e).__name__)
        raise
    _emit("out", text, local, "accepted", "")
    try:
        mid = _unwrap(sent).id
        threading.Thread(target=_check_delivery, args=(mid, text, local), daemon=True).start()
    except Exception:
        pass


def _unwrap(x):
    while hasattr(x, "root") and not hasattr(x, "id"):
        x = x.root
    return x


def _check_delivery(mid, text, local="", wait=8):
    """'Accepted' only means Photon queued it. Ask a few seconds later whether the phone actually got it,
    and once more if it's still on its way."""
    from photon_api.rpc_generated import GetMessageInput, GetMessagePath
    for delay in (wait, wait):
        time.sleep(delay)
        try:
            with _client() as photon:
                m = _unwrap(photon.projects.get_message(GetMessageInput(path=GetMessagePath(id=os.environ["PHOTON_PROJECT_ID"], messageId=mid))))
            status = str(getattr(m, "status", "unknown"))
            failure = getattr(m, "failure", None)
            why = type(_unwrap(failure)).__name__.replace("Failure", "") if failure else ""
            print(f"[Photon] text to TA \"{text[:40]}...\" is {status}" + (f" because {why}" if why else ""))
            _emit("out", text, local, status, why)
            if status in ("delivered", "failed"):
                return
        except Exception as e:
            print(f"[Photon] couldn't check delivery ({type(e).__name__})")
            return


def send_ta_text(text: str):
    """Text the TA. Returns a short status for the screen or the terminal."""
    missing = photon_missing()
    if missing:
        return "Missing " + ", ".join(missing)
    with _client() as photon:
        _send(photon, text)
    return "Photon accepted the message for delivery."


def send_ta_report(summary: str):
    status = send_ta_text(f"Kelvin lab report\n\n{summary}")
    return status.replace("the message", "the report")


def poll_ta(answer, every=2.0):
    """Forever: fetch new texts the TA sent to Kelvin, pass each to answer(text), and text back what it returns.
    answer() runs Kelvin's usual brain (the same one the bench voice uses), so both share one memory."""
    from photon_api.generated.models import TextContent

    seen = set()
    since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    print("[Photon] watching for TA texts")
    while True:
        try:
            with _client() as photon:
                while True:
                    # same query as photon_test.py, which Photon accepts; we pick out the TA's conversation below
                    query = {"createdSince": since, "direction": "received", "order": "asc", "pageSize": 100,
                             "platform": "imessage"}
                    for m in _pages(photon, query):
                        if m.id in seen:
                            continue
                        seen.add(m.id)
                        if getattr(m, "status", None) != "received" or m.sentBy.root.kind != "human":
                            continue
                        if str(m.space.root.id) != os.environ["PHOTON_TA_SPACE_ID"]:
                            continue            # someone other than the TA texted Kelvin
                        if not isinstance(m.content, TextContent) or not m.content.text.strip():
                            continue
                        text = m.content.text.strip()
                        print(f"[Photon] TA texted: {text}")
                        _emit("in", text, m.id, "received", "")
                        reply = answer(text)
                        if reply:
                            _send(photon, reply, reply_to=m.id)
                    time.sleep(every)
        except Exception as e:          # bad Wi-Fi or a Photon hiccup: wait and keep going
            detail = getattr(e, "raw_body", "") or ""
            print(f"[Photon] watching TA texts failed ({type(e).__name__}: {str(e)[:120]} {detail[:300]}), retrying in 5 s")
            if type(e).__name__ == "ValidationError":
                # a message this SDK version can't read (an iMessage tapback, say) would fail every retry,
                # so skip past it: only look at texts from now on
                since = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            time.sleep(5)


def _pages(photon, query):
    from photon_api.rpc_generated import ListMessagesInput

    query = dict(query)
    while True:
        page = photon.projects.list_messages(
            ListMessagesInput.model_validate({"path": {"id": os.environ["PHOTON_PROJECT_ID"]}, "query": query})
        )
        for item in page.items:
            yield item.root.root
        token = page.nextPageToken
        if not isinstance(token, str):
            return
        query["pageToken"] = token
