from __future__ import annotations

import argparse, asyncio, base64, hashlib, hmac, json, logging, os, tempfile, time, uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl
import yaml

from negotiator.call.firewall import sanitize_transcript
from negotiator.call.gate import HonestyGate, PrivateTerms
from negotiator.call.stt import DeepgramConfig
from negotiator.call.transport.twilio import FastAPIWebsocketTransport, RecordingMetadata, TwilioCallsClient, TwilioMediaFrame, TwilioSignatureValidator, TwilioStart
from negotiator.brain.fsm import NegotiationFSM
from negotiator.core.bus import EventBus
from negotiator.core.contracts import BusEvent, CallCard, CallOutcome, CallStatus, JournalEvent, LedgerFact
from negotiator.core.journal import Journal
from negotiator.product.market import PlannedCall, build_call_plan, outcome_evidence, supervise_call_async
from negotiator.product.report import build_report, load_records

ROOT=Path(__file__).parent; CONFIG_ROOT=ROOT/"negotiator/config/verticals"; FIXTURE_ROOT=ROOT/"negotiator/fixtures"
ModeHook=Callable[[],Awaitable[bool]|bool]; ModeRunner=Callable[[PlannedCall,tuple[LedgerFact,...]],Awaitable[CallOutcome|Mapping[str,Any]|None]]

@dataclass(frozen=True,slots=True)
class RuntimeConfig:
    vertical:str; compatibility:str; live_enabled:bool; sim_enabled:bool; tts_cache_enabled:bool
    recording_fallback_enabled:bool; stt_watchdog_s:float; prewarm_timeout_s:float; fixture:Path

class DegradationRouter:
    ORDER=("live","sim","cache","recording")
    def __init__(self,config:RuntimeConfig,hooks:Mapping[str,ModeHook]|None=None)->None:self.config=config;self.hooks=dict(hooks or {})
    def enabled_modes(self)->tuple[str,...]:
        flags=(self.config.live_enabled,self.config.sim_enabled,self.config.tts_cache_enabled,self.config.recording_fallback_enabled)
        return tuple(mode for mode,enabled in zip(self.ORDER,flags,strict=True) if enabled)
    async def prewarm(self)->dict[str,bool]:
        async def one(mode:str)->tuple[str,bool]:
            try:return mode,True if mode not in self.hooks else bool(await asyncio.wait_for(_resolve(self.hooks[mode]()),self.config.prewarm_timeout_s))
            except Exception:return mode,False
        return dict(await asyncio.gather(*(one(mode) for mode in self.enabled_modes())))
    async def select(self,availability:Mapping[str,bool]|None=None)->str:
        for mode in self.enabled_modes():
            if availability is not None:
                if availability.get(mode):return mode
            elif mode not in self.hooks or await _resolve(self.hooks[mode]()):return mode
        raise RuntimeError("no negotiation transport or fallback is available")

@dataclass(frozen=True,slots=True)
class DraftTurn:
    text:str; card:CallCard; facts:tuple[LedgerFact,...]=(); private_terms:PrivateTerms=PrivateTerms()

@dataclass(frozen=True,slots=True)
class UntrustedTranscript:
    text:str; suspicious:bool; reasons:tuple[str,...]; authoritative:bool=False

@dataclass(frozen=True,slots=True)
class TalkerContext:
    transcript:UntrustedTranscript; directives:tuple[UntrustedTranscript,...]=()

class LiveSession(Protocol):
    def frames(self)->AsyncIterator[TwilioMediaFrame|Mapping[str,Any]]:...
    async def send_audio(self,audio:bytes)->None:...
    async def mark(self,name:str)->None:...
    async def interrupt(self)->None:...

@dataclass(frozen=True,slots=True)
class LiveAdapters:
    stt:Callable[[bytes],Awaitable[str|None]]
    talker:Callable[[TalkerContext,PlannedCall,tuple[LedgerFact,...]],Awaitable[DraftTurn]]
    tts:Callable[[Any],Awaitable[bytes]]
    finalize:Callable[[PlannedCall,tuple[str,...]],Awaitable[CallOutcome|None]]
    arbiter:Callable[[TwilioMediaFrame|Mapping[str,Any]],Awaitable[str]]=lambda _frame:_async_value("continue")
    regenerate:Callable[[TalkerContext,PlannedCall,tuple[LedgerFact,...],str],Awaitable[DraftTurn]]|None=None


@dataclass(slots=True)
class PendingTwilioCall:
    planned:PlannedCall; evidence:tuple[LedgerFact,...]; future:asyncio.Future[CallOutcome|Mapping[str,Any]|None]
    connected:asyncio.Future[None]; provider_call_sid:str|None=None; claimed:bool=False
    live_task:asyncio.Task[Any]|None=None; shutdown:Callable[[],Awaitable[None]]|None=None


class PendingTwilioCalls:
    """Joins one outbound Calls API request to its authenticated inbound Media Stream."""
    def __init__(self)->None:self._calls:dict[str,PendingTwilioCall]={}
    def register(self,planned:PlannedCall,evidence:tuple[LedgerFact,...])->PendingTwilioCall:
        if planned.call_id in self._calls:raise RuntimeError(f"call already pending: {planned.call_id}")
        loop=asyncio.get_running_loop();context=PendingTwilioCall(planned,evidence,loop.create_future(),loop.create_future());self._calls[planned.call_id]=context;return context
    def bind_provider(self,context:PendingTwilioCall,call_sid:str)->None:
        if context.provider_call_sid not in (None,call_sid):raise RuntimeError("provider call SID changed")
        context.provider_call_sid=call_sid
    def claim(self,start:TwilioStart)->PendingTwilioCall:
        call_id=str(start.custom_parameters.get("call_id") or "");context=self._calls.get(call_id)
        if context is None:raise LookupError("no pending outbound call matches Twilio start")
        if context.claimed:raise RuntimeError("Twilio stream already claimed")
        self.bind_provider(context,start.call_sid);context.claimed=True;return context
    def attach_live(self,context:PendingTwilioCall,task:asyncio.Task[Any],shutdown:Callable[[],Awaitable[None]])->None:
        if not context.claimed or context.live_task is not None:raise RuntimeError("pending call cannot attach live task")
        context.live_task,context.shutdown=task,shutdown
        if not context.connected.done():context.connected.set_result(None)
    def complete(self,context:PendingTwilioCall,result:CallOutcome|Mapping[str,Any]|None)->None:
        self._calls.pop(context.planned.call_id,None)
        if not context.future.done():context.future.set_result(result)
    def fail(self,context:PendingTwilioCall,exc:BaseException)->None:
        self._calls.pop(context.planned.call_id,None)
        if not context.future.done():context.future.set_exception(exc)
    async def abort(self,context:PendingTwilioCall)->None:
        self._calls.pop(context.planned.call_id,None)
        if not context.connected.done():context.connected.cancel()
        if not context.future.done():context.future.cancel()
        if context.shutdown is not None:
            try:await asyncio.wait_for(context.shutdown(),2)
            except BaseException:pass
        task=context.live_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel();await asyncio.gather(task,return_exceptions=True)

@dataclass(slots=True)
class Composition:
    config:RuntimeConfig; vertical_config:dict[str,Any]; bus:EventBus; journal:Journal
    degradation:DegradationRouter; gate:HonestyGate
    stt_config:DeepgramConfig
    active_calls:set[str]=field(default_factory=lambda:{"call-1-lowball_broker","call-2-rushed_dispatcher","call-3-pressure_closer"})
    directive_queues:dict[str,deque[UntrustedTranscript]]=field(default_factory=dict)
    def whisper(self,call_id:str,directive:str)->BusEvent:
        if call_id not in self.active_calls:raise ValueError("call_id is not active")
        if not 1<=len(directive)<=500:raise ValueError("directive length must be 1..500")
        decision=sanitize_transcript(directive)
        envelope=UntrustedTranscript(decision.sanitized,decision.suspicious,decision.reasons)
        self.directive_queues.setdefault(call_id,deque(maxlen=20)).append(envelope)
        event=BusEvent(call_id=call_id,module="dashboard",kind="client_directive",
            payload={"text":decision.sanitized,"suspicious":decision.suspicious,"authoritative":False,"reasons":decision.reasons})
        self.bus.publish(event);return event
    def take_directives(self,call_id:str)->tuple[UntrustedTranscript,...]:
        queue=self.directive_queues.setdefault(call_id,deque(maxlen=20));items=tuple(item for item in queue if not item.suspicious);queue.clear();return items

class CallOrchestrator:
    """Per-call composition: prewarm, deterministic fallback, pipeline, and outcome publication."""
    def __init__(self,state:Composition,runners:Mapping[str,ModeRunner]|None=None)->None:
        self.state=state;self.runners=dict(runners or {});self.runners.setdefault("recording",self._recorded_runner)
    async def run_call(self,planned:PlannedCall,evidence:tuple[LedgerFact,...],runner_overrides:Mapping[str,ModeRunner]|None=None)->CallOutcome:
        outcome:CallOutcome|None=None; mode="unavailable"
        try:
            runners={**self.runners,**dict(runner_overrides or {})}
            availability=await self.state.degradation.prewarm()
            availability={name:ready and name in runners for name,ready in availability.items()}
            for candidate in self.state.degradation.enabled_modes():
                if not availability.get(candidate):continue
                mode=candidate
                try:
                    raw=await runners[candidate](planned,evidence)
                    candidate_outcome=raw if isinstance(raw,CallOutcome) else CallOutcome.model_validate(raw) if raw else None
                    if candidate_outcome is None:raise RuntimeError("runner returned no outcome")
                    if candidate_outcome.call_id!=planned.call_id or candidate_outcome.mover_id!=planned.mover_id or (candidate_outcome.quote and candidate_outcome.quote.mover_id!=planned.mover_id):raise ValueError("runner returned an outcome for a different planned call")
                    outcome=candidate_outcome;break
                except Exception as exc:
                    self.state.bus.publish(BusEvent(call_id=planned.call_id,module="app",kind="mode_error",payload={"mode":candidate,"error":type(exc).__name__}))
            if outcome is None:raise RuntimeError("all ready call modes failed")
        except Exception as exc:
            self.state.bus.publish(BusEvent(call_id=planned.call_id,module="app",kind="call_error",payload={"mode":mode,"error":type(exc).__name__}))
        finally:
            outcome=outcome or CallOutcome(call_id=planned.call_id,mover_id=planned.mover_id,status=CallStatus.HANGUP,transcript_ref=f"journal:{planned.call_id}")
            self.state.bus.publish(BusEvent(call_id=planned.call_id,module="market",kind="call_outcome",payload={"mode":mode,"outcome":outcome.model_dump(mode="json")}))
        return outcome
    async def _recorded_runner(self,planned:PlannedCall,_evidence:tuple[LedgerFact,...])->CallOutcome|None:
        for record in load_records(FIXTURE_ROOT/"three_calls.json"):
            if record.outcome.call_id==planned.call_id or record.outcome.mover_id==planned.mover_id:return record.outcome
        return None
    async def run_plan(self,businesses:Sequence[Any],demo_number_map:Mapping[str,str]|None=None)->tuple[CallOutcome,...]:
        configured_map=self.state.vertical_config.get("demo_number_map") if demo_number_map is None else demo_number_map
        plan=build_call_plan(businesses,configured_map); outcomes=[]; evidence=[]
        for planned in plan:
            outcome=await supervise_call_async(planned,lambda p=planned:self.run_call(p,tuple(evidence)),journal_tail=lambda cid:[e for e in self.state.journal.replay() if e.call_id==cid])
            outcomes.append(outcome); fact=outcome_evidence(outcome)
            if fact:evidence.append(fact)
        return tuple(outcomes)
    def make_live_runner(self,session:LiveSession,adapters:LiveAdapters)->ModeRunner:
        async def run(planned:PlannedCall,evidence:tuple[LedgerFact,...])->CallOutcome|None:
            transcripts=[];fsm:NegotiationFSM|None=None
            async for frame in session.frames():
                signal=await adapters.arbiter(frame)
                if signal=="barge_in":await session.interrupt()
                if not isinstance(frame,TwilioMediaFrame):continue
                raw=await adapters.stt(frame.payload)
                if not raw:continue
                clean=sanitize_transcript(raw);transcripts.append(clean.sanitized)
                self.state.bus.publish(BusEvent(call_id=planned.call_id,module="firewall",kind="transcript",payload={"text":clean.sanitized,"suspicious":clean.suspicious,"authoritative":False}))
                context=TalkerContext(UntrustedTranscript(clean.sanitized,clean.suspicious,clean.reasons),self.state.take_directives(planned.call_id))
                turn=await adapters.talker(context,planned,evidence)
                if fsm is None:fsm=NegotiationFSM(turn.card.phase)
                else:fsm.transition(turn.card.phase,full_estimate=bool(evidence))
                decision=self.state.gate.evaluate(draft=turn.text,card=turn.card,ledger_facts=turn.facts,private_terms=turn.private_terms)
                if decision.verdict=="block":
                    self.state.bus.publish(BusEvent(call_id=planned.call_id,module="gate",kind="gate_blocked",payload={"reason":decision.reason}))
                    if decision.stall is not None:await _speak(session,adapters,decision.stall)
                    if adapters.regenerate is not None:
                        regenerated=await adapters.regenerate(context,planned,evidence,decision.reason)
                        retry=self.state.gate.evaluate(draft=regenerated.text,card=regenerated.card,ledger_facts=regenerated.facts,private_terms=regenerated.private_terms)
                        if retry.approved is not None:await _speak(session,adapters,retry.approved)
                elif decision.approved is not None:await _speak(session,adapters,decision.approved)
            outcome=await adapters.finalize(planned,tuple(transcripts))
            (fsm or NegotiationFSM()).finish()
            return outcome
        return run


def make_twilio_outbound_runner(client:TwilioCallsClient,pending:PendingTwilioCalls,*,from_number:str,stream_url:str,
                                recording_callback_url:str,connect_timeout_s:float=20.0,
                                max_call_duration_s:float=600.0)->ModeRunner:
    """Launch a real market call, then resolve its provider callback into the core outcome."""
    if connect_timeout_s<=0 or max_call_duration_s<=0:raise ValueError("Twilio timeouts must be positive")
    async def run(planned:PlannedCall,evidence:tuple[LedgerFact,...])->CallOutcome|Mapping[str,Any]|None:
        context=pending.register(planned,evidence)
        try:
            created=await asyncio.to_thread(client.create_call,to=planned.dial_phone,from_=from_number,
                stream_url=stream_url,recording_callback_url=recording_callback_url,
                custom_parameters={"call_id":planned.call_id,"mover_id":planned.mover_id})
            pending.bind_provider(context,str(created["sid"]))
            await asyncio.wait_for(asyncio.shield(context.connected),connect_timeout_s)
            return await asyncio.wait_for(asyncio.shield(context.future),max_call_duration_s)
        except BaseException:
            await pending.abort(context);raise
    return run

def load_vertical(name:str="moving")->tuple[dict[str,Any],RuntimeConfig]:
    raw=yaml.safe_load((CONFIG_ROOT/f"{name}.yaml").read_text());compat=str(raw.get("contract_compatibility",{}).get("profile",""))
    if compat!="moving-v1":raise ValueError(f"vertical {name} is not compatible with moving-v1 contracts")
    r=raw["runtime"]
    live_override=os.getenv("NEGOTIATOR_LIVE_ENABLED")
    live_enabled=bool(r["live_enabled"]) if live_override is None else live_override.strip().casefold() in {"1","true","yes","on"}
    return raw,RuntimeConfig(str(raw["vertical"]),compat,live_enabled,bool(r["sim_enabled"]),bool(r["tts_cache_enabled"]),bool(r["recording_fallback_enabled"]),float(r["stt_watchdog_s"]),float(r["prewarm_timeout_s"]),FIXTURE_ROOT/str(r["recording_fixture"]))

def compose(*,vertical:str="moving",journal_path:str|Path|None=None,hooks:Mapping[str,ModeHook]|None=None)->Composition:
    raw,config=load_vertical(vertical);bus=EventBus();journal=Journal(journal_path or Path(".runtime/journal.jsonl"));journal.attach(bus)
    stt_config=DeepgramConfig(watchdog_s=config.stt_watchdog_s)
    return Composition(config,raw,bus,journal,DegradationRouter(config,hooks),HonestyGate(stall_phrases=raw["voss"]["stalls"]),stt_config)

def create_api(composition:Composition|None=None,*,dashboard_token:str|None=None,twilio_validator:TwilioSignatureValidator|None=None,
               allowed_origins:Iterable[str]|None=None,orchestrator:CallOrchestrator|None=None,
               live_adapters_factory:Callable[[PlannedCall,FastAPIWebsocketTransport],LiveAdapters]|None=None,
               planned_call_resolver:Callable[[TwilioStart],PlannedCall]|None=None,enable_twilio:bool|None=None,
               public_base_url:str|None=None,pending_twilio_calls:PendingTwilioCalls|None=None)->Any:
    try:from fastapi import FastAPI,HTTPException,Request,WebSocket
    except ImportError as exc:raise RuntimeError("install negotiator[webhook]") from exc
    globals()["Request"] = Request
    globals()["WebSocket"] = WebSocket
    state=composition or compose(vertical=os.getenv("NEGOTIATOR_VERTICAL","moving"));token=dashboard_token or os.getenv("DASHBOARD_BEARER_TOKEN")
    if not token:raise ValueError("DASHBOARD_BEARER_TOKEN is required")
    live_required=state.config.live_enabled if enable_twilio is None else enable_twilio
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN","")
    validator=twilio_validator or (TwilioSignatureValidator(twilio_token) if twilio_token else None)
    if live_required and validator is None:raise ValueError("TWILIO_AUTH_TOKEN is required in live mode")
    configured_origins={item.strip() for item in os.getenv("DASHBOARD_ALLOWED_ORIGINS","").split(",") if item.strip()}
    using_default_origins=allowed_origins is None and not configured_origins
    origins=set(allowed_origins or configured_origins or {"http://localhost:3000","http://127.0.0.1:3000"});rate:dict[str,deque[float]]={}
    if using_default_origins:logging.getLogger(__name__).warning("DASHBOARD_ALLOWED_ORIGINS is not configured; localhost-only defaults are active")
    if live_required and not (orchestrator and live_adapters_factory and (pending_twilio_calls or planned_call_resolver)):raise ValueError("live Twilio requires orchestrator, LiveAdapters factory, and pending-call registry or PlannedCall resolver")
    canonical=(public_base_url or os.getenv("PUBLIC_BASE_URL","")).rstrip("/")
    if live_required and not canonical.startswith("https://"):raise ValueError("PUBLIC_BASE_URL must be canonical HTTPS in live mode")
    api=FastAPI(title="Negotiator War Room",docs_url=None,redoc_url=None)
    if origins:
        from fastapi.middleware.cors import CORSMiddleware
        api.add_middleware(CORSMiddleware,allow_origins=sorted(origins),allow_credentials=True,allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type"])
    def auth(headers:Mapping[str,str])->None:
        import hmac
        supplied=headers.get("authorization","").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied,token):raise HTTPException(401,"unauthorized")
    def origin(headers:Mapping[str,str])->None:
        value=headers.get("origin")
        if not value or value not in origins:raise HTTPException(403,"origin denied")
    def issue_ticket()->str:
        payload=f"journal:{int(time.time())+30}";signature=hmac.new(token.encode(),payload.encode(),hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload.encode()+b"."+signature).decode().rstrip("=")
    def valid_ticket(ticket:str)->bool:
        try:
            raw=base64.urlsafe_b64decode(ticket+"="*(-len(ticket)%4));payload,signature=raw.split(b".",1);scope,expiry=payload.decode().split(":",1)
            return scope=="journal" and int(expiry)>=int(time.time()) and hmac.compare_digest(signature,hmac.new(token.encode(),payload,hashlib.sha256).digest())
        except Exception:return False
    @api.post("/api/journal-ticket")
    async def journal_ticket(request:Request)->dict[str,Any]:auth(request.headers);origin(request.headers);return {"ticket":issue_ticket(),"expires_in":30}
    @api.get("/api/journal/replay")
    async def replay_journal(request:Request,fixture:str="full_run.jsonl",after_seq:int=0)->dict[str,Any]:
        auth(request.headers);origin(request.headers)
        if fixture=="journal":rows=[row.model_dump(mode="json") for row in await asyncio.to_thread(state.journal.replay)]
        else:
            path=FIXTURE_ROOT/Path(fixture).name
            rows=[JournalEvent.model_validate_json(line).model_dump(mode="json") for line in path.read_text().splitlines() if line]
        return {"mode":"replay","events":[row for row in rows if row["seq"]>after_seq][-500:]}
    @api.websocket("/ws/journal")
    async def journal_socket(websocket:WebSocket)->None:
        try:
            if not valid_ticket(str(websocket.query_params.get("ticket") or "")) or websocket.headers.get("origin") not in origins:return await websocket.close(code=4401)
            after=max(0,int(websocket.query_params.get("after_seq","0")));await websocket.accept();queue:asyncio.Queue[None]=asyncio.Queue(maxsize=128);loop=asyncio.get_running_loop()
            def push(event:BusEvent)->None:
                def put()->None:
                    if queue.full():queue.get_nowait()
                    queue.put_nowait(None)
                loop.call_soon_threadsafe(put)
            unsubscribe=state.bus.subscribe_all(push)
            try:
                backlog=await asyncio.to_thread(state.journal.replay)
                initial=[r for r in backlog if r.seq>after][-500:]
                if initial and initial[0].seq>after+1:await websocket.send_json({"kind":"journal_reset","after_seq":after,"resume_seq":initial[0].seq})
                for row in initial:await websocket.send_text(row.model_dump_json())
                cursor=initial[-1].seq if initial else after
                while True:
                    await queue.get()
                    while True:
                        try:queue.get_nowait()
                        except asyncio.QueueEmpty:break
                    rows=await asyncio.to_thread(state.journal.replay)
                    fresh=[row for row in rows if row.seq>cursor][-500:]
                    if fresh and fresh[0].seq>cursor+1:await websocket.send_json({"kind":"journal_reset","after_seq":cursor,"resume_seq":fresh[0].seq})
                    for row in fresh:await websocket.send_text(row.model_dump_json())
                    if fresh:cursor=fresh[-1].seq
            finally:unsubscribe()
        except Exception:return
    @api.post("/api/whisper")
    async def whisper(request:Request)->dict[str,Any]:
        auth(request.headers);origin(request.headers);now=time.monotonic();bucket=rate.setdefault(token,deque())
        while bucket and now-bucket[0]>10:bucket.popleft()
        if len(bucket)>=5:raise HTTPException(429,"rate limit")
        bucket.append(now);payload=await request.json()
        try:event=state.whisper(str(payload.get("call_id","")),str(payload.get("directive","")))
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        return {"accepted":True,"seq_hint":len(state.journal.replay()),"event":event.model_dump(mode="json")}
    @api.websocket("/ws/twilio")
    async def twilio_socket(websocket:WebSocket)->None:
        if not live_required:return await websocket.close(code=4403)
        assert validator is not None
        signature=websocket.headers.get("x-twilio-signature")
        signed_url=(canonical.replace("https://","wss://")+"/ws/twilio") if canonical else str(websocket.url)
        if canonical and websocket.url.query:signed_url+=f"?{websocket.url.query}"
        if not validator.validate(signed_url,signature):return await websocket.close(code=4401)
        transport=FastAPIWebsocketTransport(websocket)
        assert orchestrator and live_adapters_factory
        started=await transport.begin()
        if pending_twilio_calls is not None:
            context=pending_twilio_calls.claim(started);adapters=live_adapters_factory(context.planned,transport)
            pending_twilio_calls.attach_live(context,asyncio.current_task(),transport.close)
            try:
                result=await orchestrator.make_live_runner(transport,adapters)(context.planned,context.evidence)
                pending_twilio_calls.complete(context,result)
            except BaseException as exc:
                pending_twilio_calls.fail(context,exc);raise
        else:
            assert planned_call_resolver is not None
            planned=planned_call_resolver(started);adapters=live_adapters_factory(planned,transport)
            await orchestrator.run_call(planned,(),{"live":orchestrator.make_live_runner(transport,adapters)})
    @api.post("/api/twilio/recording")
    async def recording_callback(request:Request)->dict[str,Any]:
        if validator is None:raise HTTPException(503,"Twilio callbacks are not configured")
        body=(await request.body()).decode();params=dict(parse_qsl(body));signature=request.headers.get("x-twilio-signature")
        signed_url=(canonical+"/api/twilio/recording") if canonical else str(request.url)
        if not validator.validate(signed_url,signature,params):raise HTTPException(401,"invalid Twilio signature")
        if params.get("RecordingChannels")!="2" or (params.get("RecordingTrack") is not None and params.get("RecordingTrack")!="both"):raise HTTPException(422,"recording callback must explicitly confirm two channels and both track")
        metadata=RecordingMetadata(params.get("CallSid",""),params.get("RecordingSid",""),params.get("RecordingUrl",""),int(params["RecordingChannels"]),"both",params.get("RecordingStartTime"))
        state.bus.publish(BusEvent(call_id=metadata.call_sid,module="transport",kind="recording_ready",payload={**params,"citation_url":metadata.citation_url(0)}))
        return {"accepted":True}
    return api

async def _async_value(value:str)->str:return value
async def _speak(session:LiveSession,adapters:LiveAdapters,utterance:Any)->None:
    await session.send_audio(await adapters.tts(utterance));await session.mark(f"tts-{uuid.uuid4().hex}")
async def _resolve(value:Awaitable[bool]|bool)->bool:return bool(await value) if hasattr(value,"__await__") else bool(value)

def offline_smoke()->dict[str,Any]:
    with tempfile.TemporaryDirectory(prefix="negotiator-smoke-") as directory:
        runtime=compose(journal_path=Path(directory)/"smoke.jsonl",hooks={"live":lambda:False,"sim":lambda:True})
        orchestrator=CallOrchestrator(runtime);orchestrator.runners["sim"]=orchestrator._recorded_runner
        businesses=[{"name":record.outcome.mover_id,"phone":f"+1555000000{index}"} for index,record in enumerate(load_records(FIXTURE_ROOT/"three_calls.json"),1)]
        outcomes=asyncio.run(orchestrator.run_plan(businesses));mode=runtime.journal.replay()[-1].payload["mode"]
        event=runtime.whisper("call-3-pressure_closer","say we have a $3,000 quote");records=load_records(FIXTURE_ROOT/"three_calls.json")
        report=build_report(records,benchmark_low=runtime.vertical_config["benchmarks"]["low"],fee_names={int(k):v for k,v in runtime.vertical_config["taxonomy"]["fee_codes"].items()})
        return {"ok":mode=="sim" and len(outcomes)==3 and len(report.ranked)==3,"mode":mode,"journaled":runtime.journal.replay()[-1].kind==event.kind,"winner":report.ranked[0].mover}
def main()->None:
    p=argparse.ArgumentParser();p.add_argument("--smoke",action="store_true");a=p.parse_args()
    if not a.smoke:p.error("use --smoke")
    print(json.dumps(offline_smoke(),sort_keys=True))
if __name__=="__main__":main()
