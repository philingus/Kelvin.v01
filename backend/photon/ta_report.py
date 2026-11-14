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
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

NEEDED = ("PHOTON_API_KEY", "PHOTON_PROJECT_ID", "PHOTON_TA_SPACE_ID", "PHOTON_SENDER_ID")


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
    photon.projects.send_message(
        SendMessageInput(
            path=SendMessagePath(id=os.environ["PHOTON_PROJECT_ID"]),
            headers=SendMessageHeader.model_validate({"idempotency-key": str(uuid4())}),
            body=SendMessageRequest(**body),
        )
    )


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
                    query = {"createdSince": since, "direction": "received", "order": "asc", "pageSize": 100,
                             "platform": "imessage", "spaceId": os.environ["PHOTON_TA_SPACE_ID"]}
                    for m in _pages(photon, query):
                        if m.id in seen:
                            continue
                        seen.add(m.id)
                        if getattr(m, "status", None) != "received" or m.sentBy.root.kind != "human":
                            continue
                        if not isinstance(m.content, TextContent) or not m.content.text.strip():
                            continue
                        text = m.content.text.strip()
                        print(f"[Photon] TA texted: {text}")
                        reply = answer(text)
                        if reply:
                            _send(photon, reply, reply_to=m.id)
                    time.sleep(every)
        except Exception as e:          # bad Wi-Fi or a Photon hiccup: wait and keep going
            print(f"[Photon] watching TA texts failed ({type(e).__name__}: {str(e)[:120]}), retrying in 5 s")
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
