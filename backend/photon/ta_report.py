import os

def send_ta_report(summary: str):
    from uuid import uuid4

    from photon_api import Photon
    from photon_api.generated.models import SendMessageRequest, SendTextContent
    from photon_api.rpc_generated import SendMessageHeader, SendMessageInput, SendMessagePath

    names = ("PHOTON_API_KEY", "PHOTON_PROJECT_ID", "PHOTON_TA_SPACE_ID", "PHOTON_SENDER_ID")
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        return "Missing " + ", ".join(missing)

    with Photon(headers={"Authorization": f"Bearer {os.environ['PHOTON_API_KEY']}"}) as photon:
        photon.projects.send_message(
            SendMessageInput(
                path=SendMessagePath(id=os.environ["PHOTON_PROJECT_ID"]),
                headers=SendMessageHeader.model_validate({"idempotency-key": str(uuid4())}),
                body=SendMessageRequest(
                    spaceId=os.environ["PHOTON_TA_SPACE_ID"],
                    senderId=os.environ["PHOTON_SENDER_ID"],
                    content=SendTextContent(
                        type="text", format="plain", text=f"Kelvin lab report\n\n{summary}"
                    ),
                ),
            )
        )
    return "Photon accepted the report for delivery."