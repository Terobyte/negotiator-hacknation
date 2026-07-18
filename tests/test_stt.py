import asyncio
import json

from negotiator.call.stt import DeepgramConfig, DeepgramStream, decode_deepgram_message


class FakeSocket:
    def __init__(self) -> None:
        self.sent = []
        self.closed = False
        self.response = json.dumps(
            {
                "type": "Results",
                "is_final": True,
                "speech_final": True,
                "start": 1.0,
                "duration": 0.5,
                "channel": {"alternatives": [{"transcript": "hello mover", "confidence": 0.98}]},
            }
        )

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return self.response

    async def close(self):
        self.closed = True


def test_phonecall_url_and_network_free_stream_lifecycle() -> None:
    async def run() -> None:
        sockets = []
        connections = []

        async def connector(url, headers):
            socket = FakeSocket()
            sockets.append(socket)
            connections.append((url, headers))
            return socket

        clock_value = [10.0]
        stream = DeepgramStream(
            DeepgramConfig(api_key="fake", watchdog_s=2),
            connector=connector,
            clock=lambda: clock_value[0],
        )
        assert await stream.connect() == 1
        assert "model=nova-2-phonecall" in connections[0][0]
        assert "sample_rate=8000" in connections[0][0]
        assert connections[0][1] == {"Authorization": "Token fake"}
        await stream.send_audio(b"pcm")
        transcript = await stream.receive()
        assert transcript and transcript.text == "hello mover" and transcript.speech_final
        clock_value[0] = 13.0
        assert stream.watchdog_expired()
        assert await stream.reconnect() == 2
        assert sockets[0].closed
        await stream.close()

    asyncio.run(run())


def test_non_result_and_empty_result_are_ignored() -> None:
    assert decode_deepgram_message('{"type":"Metadata"}') is None
    assert decode_deepgram_message(
        '{"type":"Results","channel":{"alternatives":[{"transcript":""}]}}'
    ) is None
