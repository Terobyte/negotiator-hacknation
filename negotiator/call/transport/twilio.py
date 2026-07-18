from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncIterator, Callable, Mapping

SAMPLE_RATE, CHANNELS, ENCODING = 8_000, 1, "audio/x-mulaw"
_SID = re.compile(r"^(?:AC|CA|MZ|RE)[0-9a-fA-F]{32}$")


@dataclass(frozen=True, slots=True)
class TwilioMediaFrame:
    stream_sid: str; payload: bytes; sequence_number: int; chunk: int; timestamp_ms: int


@dataclass(frozen=True, slots=True)
class TwilioStart:
    stream_sid: str; call_sid: str; tracks: tuple[str, ...]
    custom_parameters: Mapping[str, str]; media_format: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RecordingMetadata:
    call_sid: str; recording_sid: str; recording_url: str; channels: int = 2
    track: str = "both"; start_time: str | None = None

    def __post_init__(self) -> None:
        if not _valid_sid(self.call_sid, "CA") or not _valid_sid(self.recording_sid, "RE"):
            raise ValueError("valid CA/RE SIDs are required")
        if not self.recording_url.startswith("https://"):
            raise ValueError("recording URL must use HTTPS")
        if self.channels != 2 or self.track != "both":
            raise ValueError("recordings must preserve both speakers in dual channel")

    def citation_url(self, offset_sec: float) -> str:
        if offset_sec < 0: raise ValueError("offset must be non-negative")
        return f"{self.recording_url}#t={offset_sec:g}"


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    recording_channels: str = "dual"
    recording_track: str = "both"
    callback_events: tuple[str, ...] = ("completed",)

    def as_twilio_params(self, callback_url: str) -> dict[str, str]:
        if not callback_url.startswith("https://"): raise ValueError("callback URL must use HTTPS")
        return {"Record": "true", "RecordingChannels": self.recording_channels,
                "RecordingTrack": self.recording_track, "RecordingStatusCallback": callback_url,
                "RecordingStatusCallbackEvent": " ".join(self.callback_events)}


def call_creation_config(*, to: str, from_: str, stream_url: str, recording_callback_url: str,
                         custom_parameters: Mapping[str,str] | None = None) -> dict[str,Any]:
    """Build the exact Twilio call/TwiML seam with dual-channel recording enabled."""
    if not stream_url.startswith("wss://"): raise ValueError("media stream URL must use WSS")
    if not to or not from_: raise ValueError("to/from numbers are required")
    params="".join(f'<Parameter name="{escape(str(k))}" value="{escape(str(v))}" />' for k,v in (custom_parameters or {}).items())
    twiml=f'<Response><Connect><Stream url="{escape(stream_url)}">{params}</Stream></Connect></Response>'
    return {"to":to,"from_":from_,"twiml":twiml,**RecordingRequest().as_twilio_params(recording_callback_url)}


HttpPost = Callable[[str, bytes, Mapping[str, str], float], Mapping[str, Any]]


class TwilioCallsClient:
    """Small injectable Twilio Calls API client used by the market runner."""
    def __init__(self, account_sid: str, auth_token: str, *, post: HttpPost | None = None, timeout_s: float = 10.0) -> None:
        if not _valid_sid(account_sid, "AC") or not auth_token:
            raise ValueError("valid Twilio account SID and auth token are required")
        self.account_sid, self.auth_token = account_sid, auth_token
        self.timeout_s, self._post = timeout_s, post or _post_form

    def create_call(self, *, to: str, from_: str, stream_url: str, recording_callback_url: str,
                    custom_parameters: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        config = call_creation_config(to=to, from_=from_, stream_url=stream_url,
            recording_callback_url=recording_callback_url, custom_parameters=custom_parameters)
        form = {
            "To": config["to"], "From": config["from_"], "Twiml": config["twiml"],
            "Record": config["Record"], "RecordingChannels": config["RecordingChannels"],
            "RecordingTrack": config["RecordingTrack"],
            "RecordingStatusCallback": config["RecordingStatusCallback"],
            "RecordingStatusCallbackEvent": config["RecordingStatusCallbackEvent"],
        }
        body = urllib.parse.urlencode(form).encode()
        credentials = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        headers = {"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"}
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Calls.json"
        result = self._post(url, body, headers, self.timeout_s)
        if not _valid_sid(str(result.get("sid") or ""), "CA"):
            raise RuntimeError("Twilio Calls API returned no valid call SID")
        return result


def recording_citation(metadata: RecordingMetadata, *, event_timestamp_ms: int, stream_start_timestamp_ms: int = 0) -> str:
    """Convert a media/journal timestamp into a stable recording Media Fragment."""
    if event_timestamp_ms < stream_start_timestamp_ms: raise ValueError("event precedes recording start")
    return metadata.citation_url((event_timestamp_ms-stream_start_timestamp_ms)/1000)


class TwilioSignatureValidator:
    """Twilio-compatible HMAC-SHA1 request signature validator."""
    def __init__(self, auth_token: str) -> None:
        if not auth_token: raise ValueError("Twilio auth token is required")
        self._token = auth_token.encode()

    def signature(self, url: str, params: Mapping[str, str] | None = None) -> str:
        payload = url + "".join(key + str(value) for key, value in sorted((params or {}).items()))
        digest = hmac.new(self._token, payload.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    def validate(self, url: str, signature: str | None, params: Mapping[str, str] | None = None) -> bool:
        return bool(signature) and hmac.compare_digest(self.signature(url, params), signature)


class Lifecycle(StrEnum):
    NEW="new"; CONNECTED="connected"; STARTED="started"; STOPPED="stopped"


@dataclass(slots=True)
class TwilioFrameSerializer:
    sample_rate: int = SAMPLE_RATE; channels: int = CHANNELS; encoding: str = ENCODING
    state: Lifecycle = Lifecycle.NEW; stream_sid: str | None = None; call_sid: str | None = None
    last_sequence: int = 0; last_chunk: int = 0; last_timestamp: int = -1
    pending_marks: set[str] = field(default_factory=set)

    def parse(self, raw: str | bytes) -> TwilioStart | TwilioMediaFrame | dict[str, Any]:
        message = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        event = message.get("event")
        if event == "connected":
            self._require(Lifecycle.NEW)
            if message.get("protocol")!="Call" or message.get("version")!="1.0.0": raise ValueError("unsupported Twilio protocol version")
            self.state = Lifecycle.CONNECTED; return dict(message)
        if event == "start":
            self._require(Lifecycle.CONNECTED)
            start = message.get("start") or {}; media = start.get("mediaFormat") or {}
            stream_sid = str(start.get("streamSid") or message.get("streamSid") or "")
            call_sid = str(start.get("callSid") or "")
            if str(message.get("streamSid") or "") != stream_sid: raise ValueError("top-level and start stream SID mismatch")
            if not _valid_sid(stream_sid, "MZ") or not _valid_sid(call_sid, "CA"): raise ValueError("valid stream/call SIDs required")
            if media.get("encoding") != ENCODING or int(media.get("sampleRate",0)) != SAMPLE_RATE or int(media.get("channels",0)) != CHANNELS:
                raise ValueError("Twilio stream must be mono audio/x-mulaw at 8000 Hz")
            start_sequence=int(message.get("sequenceNumber",0))
            if start_sequence != 1: raise ValueError("start sequence number must be exactly one")
            self.last_sequence=start_sequence
            tracks=tuple(map(str,start.get("tracks",())))
            if "inbound" not in tracks: raise ValueError("bidirectional stream must include inbound track")
            self.stream_sid, self.call_sid, self.state = stream_sid, call_sid, Lifecycle.STARTED
            return TwilioStart(stream_sid, call_sid, tracks,
                {str(k):str(v) for k,v in (start.get("customParameters") or {}).items()}, dict(media))
        self._require(Lifecycle.STARTED)
        self._match_stream(message)
        seq = self._advance_sequence(message)
        if event == "media":
            media = message.get("media") or {}; chunk=int(media.get("chunk",0)); timestamp=int(media.get("timestamp",-1))
            if chunk != self.last_chunk+1 or timestamp < self.last_timestamp: raise ValueError("media chunk must advance by one and timestamp be monotonic")
            self.last_chunk, self.last_timestamp = chunk, timestamp
            payload=base64.b64decode(media.get("payload") or "",validate=True)
            if not payload: raise ValueError("empty media payload")
            return TwilioMediaFrame(self.stream_sid or "",payload,seq,chunk,timestamp)
        if event == "mark":
            name=str((message.get("mark") or {}).get("name") or "")
            if not name or name not in self.pending_marks: raise ValueError("unknown mark acknowledgement")
            self.pending_marks.remove(name); return dict(message)
        if event == "dtmf":
            if str((message.get("dtmf") or {}).get("track") or "")!="inbound_track": raise ValueError("DTMF must identify inbound_track")
            digit=str((message.get("dtmf") or {}).get("digit") or "")
            if len(digit)!=1 or digit not in "0123456789*#ABCD": raise ValueError("invalid DTMF digit")
            return dict(message)
        if event == "stop":
            if str((message.get("stop") or {}).get("callSid") or "")!=self.call_sid: raise ValueError("stop call SID mismatch")
            self.pending_marks.clear();self.state=Lifecycle.STOPPED;return dict(message)
        raise ValueError(f"unsupported Twilio event: {event!r}")

    def media(self, stream_sid: str, payload: bytes) -> str:
        self._outbound_sid(stream_sid)
        if not payload: raise ValueError("audio payload is required")
        return _json({"event":"media","streamSid":stream_sid,"media":{"payload":base64.b64encode(payload).decode()}})

    def clear(self, stream_sid: str) -> str:
        self._outbound_sid(stream_sid); self.pending_marks.clear()
        return _json({"event":"clear","streamSid":stream_sid})

    def mark(self, stream_sid: str, name: str) -> str:
        self._outbound_sid(stream_sid)
        if not name or name in self.pending_marks: raise ValueError("mark name must be unique and nonempty")
        self.pending_marks.add(name)
        return _json({"event":"mark","streamSid":stream_sid,"mark":{"name":name}})

    def _require(self, state: Lifecycle) -> None:
        if self.state is not state: raise ValueError(f"invalid lifecycle: {self.state} cannot accept event requiring {state}")
    def _match_stream(self, message: Mapping[str,Any]) -> None:
        if message.get("streamSid") != self.stream_sid: raise ValueError("stream SID changed mid-stream")
    def _advance_sequence(self, message: Mapping[str,Any]) -> int:
        seq=int(message.get("sequenceNumber",0))
        if seq != self.last_sequence+1: raise ValueError("sequence number must advance by exactly one")
        self.last_sequence=seq; return seq
    def _outbound_sid(self, sid: str) -> None:
        if self.state is not Lifecycle.STARTED or sid != self.stream_sid: raise ValueError("outbound frame requires active matching stream")


class FastAPIWebsocketTransport:
    def __init__(self, websocket: Any, serializer: TwilioFrameSerializer | None=None) -> None:
        self.websocket=websocket; self.serializer=serializer or TwilioFrameSerializer(); self.start:TwilioStart|None=None; self._accepted=False; self._start_yielded=False
    async def begin(self) -> TwilioStart:
        """Accept and authenticate the Twilio lifecycle through `start` before routing the call."""
        if self.start is not None:return self.start
        if not self._accepted:await self.websocket.accept();self._accepted=True
        connected=self.serializer.parse(await self.websocket.receive_text())
        if not isinstance(connected,dict) or connected.get("event")!="connected":raise ValueError("connected event must precede start")
        started=self.serializer.parse(await self.websocket.receive_text())
        if not isinstance(started,TwilioStart):raise ValueError("start event must follow connected")
        self.start=started;return started
    async def receive(self) -> AsyncIterator[TwilioMediaFrame|dict[str,Any]]:
        started=await self.begin()
        if not self._start_yielded:self._start_yielded=True;yield {"event":"start","start":started}
        while self.serializer.state is not Lifecycle.STOPPED:
            event=self.serializer.parse(await self.websocket.receive_text())
            if isinstance(event,TwilioStart):raise ValueError("duplicate start event")
            yield event
    async def frames(self) -> AsyncIterator[TwilioMediaFrame|dict[str,Any]]:
        async for event in self.receive(): yield event
    async def send_audio(self,payload:bytes) -> None:
        await self.websocket.send_text(self.serializer.media(self.serializer.stream_sid or "",payload))
    async def mark(self,name:str) -> None:
        await self.websocket.send_text(self.serializer.mark(self.serializer.stream_sid or "",name))
    async def interrupt(self) -> None:
        await self.websocket.send_text(self.serializer.clear(self.serializer.stream_sid or ""))
    async def close(self) -> None:
        if self.serializer.state is Lifecycle.STARTED:
            try:await self.interrupt()
            except Exception:pass
        try:await self.websocket.close(code=1000)
        except Exception:pass


def _valid_sid(value:str,prefix:str)->bool: return value.startswith(prefix) and bool(_SID.fullmatch(value))
def _json(value:Mapping[str,Any])->str: return json.dumps(value,separators=(",",":"))


def _post_form(url: str, body: bytes, headers: Mapping[str, str], timeout_s: float) -> Mapping[str, Any]:
    request=urllib.request.Request(url,data=body,headers=dict(headers),method="POST")
    try:
        with urllib.request.urlopen(request,timeout=timeout_s) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail=exc.read(1024).decode(errors="replace")
        raise RuntimeError(f"Twilio Calls API failed with HTTP {exc.code}: {detail}") from exc


def smoke()->dict[str,Any]:
    mz="MZ"+"1"*32;ca="CA"+"2"*32
    s=TwilioFrameSerializer(); s.parse(_json({"event":"connected","protocol":"Call","version":"1.0.0"}))
    s.parse(_json({"event":"start","streamSid":mz,"sequenceNumber":"1","start":{"streamSid":mz,"callSid":ca,"tracks":["inbound"],"mediaFormat":{"encoding":ENCODING,"sampleRate":8000,"channels":1}}}))
    frame=s.parse(_json({"event":"media","streamSid":mz,"sequenceNumber":"2","media":{"chunk":"1","timestamp":"0","payload":base64.b64encode(b"x"*160).decode()}}))
    s.mark(mz,"done")
    s.parse(_json({"event":"mark","streamSid":mz,"sequenceNumber":"3","mark":{"name":"done"}}))
    return {"ok":isinstance(frame,TwilioMediaFrame),"sample_rate":8000,"recording_channels":2}

def main()->None:
    p=argparse.ArgumentParser(); p.add_argument("--smoke",action="store_true"); a=p.parse_args()
    if not a.smoke:p.error("use --smoke")
    print(json.dumps(smoke(),sort_keys=True))
if __name__=="__main__":main()
