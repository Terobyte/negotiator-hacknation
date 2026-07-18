import asyncio
import base64
import json

from negotiator.call.transport.el_ws import (
    decode_agent_event,
    encode_initiation,
    encode_pong,
    encode_user_audio,
    smoke,
)


def test_protocol_encoders_and_decoders() -> None:
    audio = json.loads(encode_user_audio(b"pcm"))
    assert base64.b64decode(audio["user_audio_chunk"]) == b"pcm"
    initiation = json.loads(encode_initiation({"role": "broker"}, {}))
    assert initiation["type"] == "conversation_initiation_client_data"
    assert initiation["dynamic_variables"]["role"] == "broker"
    assert json.loads(encode_pong(12)) == {"type": "pong", "event_id": 12}

    response = decode_agent_event(
        json.dumps({"type": "agent_response", "agent_response_event": {"agent_response": "Hi"}})
    )
    assert response.text == "Hi"
    interruption = decode_agent_event(
        json.dumps({"type": "interruption", "interruption_event": {"event_id": "i-1"}})
    )
    assert interruption.event_id == "i-1"


def test_network_free_smoke() -> None:
    result = asyncio.run(smoke())
    assert result == {"ok": True, "sent_messages": 3}
