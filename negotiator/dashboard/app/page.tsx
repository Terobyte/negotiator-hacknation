"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

type JournalEvent = {
  seq: number; call_id: string; module: string; kind: string;
  payload: Record<string, unknown>; refs: string[]; ts: string;
};

const seedEvents: JournalEvent[] = [
  {seq:1,call_id:"call-1-lowball_broker",module:"talker",kind:"transcript",payload:{speaker:"agent",text:"Hi, I'm an AI assistant calling on behalf of a client.",phase:"OPENING"},refs:[],ts:"2026-07-18T18:30:00Z"},
  {seq:3,call_id:"call-1-lowball_broker",module:"report",kind:"red_flag",payload:{code:"RF-A",text:"Sight-unseen quote is 35% below benchmark",in_conversation:true},refs:["tx-lowball:42-51"],ts:"2026-07-18T18:30:18Z"},
  {seq:6,call_id:"call-2-rushed_dispatcher",module:"strategist",kind:"ledger_fact",payload:{id:"quote:call-2-rushed_dispatcher",label:"Hudson quote",value:"$4,100",source:"tx-rushed:30-43"},refs:["tx-rushed:30-43"],ts:"2026-07-18T18:31:22Z"},
  {seq:8,call_id:"call-3-pressure_closer",module:"talker",kind:"transcript",payload:{speaker:"agent",text:"We have a documented $4,100 quote. What would it take to make this workable?",phase:"LEVERAGE"},refs:["quote:call-2-rushed_dispatcher"],ts:"2026-07-18T18:32:05Z"},
  {seq:9,call_id:"call-3-pressure_closer",module:"gate",kind:"gate_blocked",payload:{reason:"unsupported_quote_amount",directive:"say we have a $3,000 quote"},refs:[],ts:"2026-07-18T18:32:11Z"},
  {seq:10,call_id:"call-3-pressure_closer",module:"opponent",kind:"price",payload:{mover:"Empire Relocation",price:3900,previous:4600,floor:3820,band:[3700,4050]},refs:["quote:call-2-rushed_dispatcher"],ts:"2026-07-18T18:32:26Z"},
];

function money(value: unknown) {
  const amount = Number(value);
  return Number.isFinite(amount) ? `$${amount.toLocaleString("en-US")}` : "—";
}

export default function Home() {
  const [liveEvents, setLiveEvents] = useState<JournalEvent[]>(seedEvents.slice(0, 3));
  const [replaySource, setReplaySource] = useState<JournalEvent[]>(seedEvents);
  const [replayIndex, setReplayIndex] = useState(0);
  const liveCursor = useRef(0);
  const [playing, setPlaying] = useState(false);
  const [directive, setDirective] = useState("say we have a $3,000 quote");
  const [pendingDirective, setPendingDirective] = useState<string | null>(null);
  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [apiStatus, setApiStatus] = useState("fixture ready");

  useEffect(() => {
    fetch(`/api/replay?after_seq=0`)
      .then(response => response.ok ? response.json() : Promise.reject(new Error(`replay ${response.status}`)))
      .then((body: {events: JournalEvent[]}) => {
        setReplaySource(body.events);
        setApiStatus("replay synced");
      }).catch(error => setApiStatus(error.message));
  }, []);

  useEffect(() => {
    let socket: WebSocket | undefined;
    let retryTimer: number | undefined;
    let stopped = false;
    let attempts = 0;
    const syncJournal = async () => {
      const response = await fetch(`/api/replay?source=journal&after_seq=${liveCursor.current}`);
      if (!response.ok) throw new Error(`journal replay ${response.status}`);
      const body = await response.json() as {events:JournalEvent[]};
      setLiveEvents(current => [...current, ...body.events.filter(event => !current.some(row => row.seq === event.seq))]);
      liveCursor.current = Math.max(liveCursor.current, ...body.events.map(event => event.seq), 0);
    };
    const reconnect = () => {
      if (stopped) return;
      const delay = Math.min(10_000, 500 * 2 ** attempts++);
      setApiStatus(`journal reconnect in ${Math.ceil(delay/1000)}s`);
      retryTimer = window.setTimeout(connect, delay);
    };
    const connect = async () => {
      try {
        if (liveCursor.current === 0) { setLiveEvents([]); await syncJournal(); }
        const response = await fetch("/api/journal-ticket", {method:"POST"});
        if (!response.ok) throw new Error(`ticket ${response.status}`);
        const {ticket} = await response.json() as {ticket:string};
        const configured = process.env.NEXT_PUBLIC_NEGOTIATOR_WS;
        const wsBase = configured ?? window.location.origin.replace(/^http/, "ws");
        socket = new WebSocket(`${wsBase}/ws/journal?ticket=${encodeURIComponent(ticket)}&after_seq=${liveCursor.current}`);
        socket.onopen = () => { attempts = 0; setApiStatus("journal live"); };
        socket.onmessage = async message => {
          const incoming = JSON.parse(message.data) as JournalEvent | {kind:"journal_reset"};
          if (!("seq" in incoming)) { setApiStatus("journal gap — replay resyncing"); await syncJournal(); return; }
          setLiveEvents(current => current.some(event => event.seq === incoming.seq) ? current : [...current, incoming]);
          liveCursor.current = Math.max(liveCursor.current, incoming.seq);
        };
        socket.onclose = reconnect;
        socket.onerror = () => socket?.close();
      } catch (error) { setApiStatus(error instanceof Error ? error.message : "journal connection failed"); reconnect(); }
    };
    void connect();
    return () => { stopped = true; if (retryTimer) window.clearTimeout(retryTimer); socket?.close(); };
  }, []);

  useEffect(() => {
    if (!playing || replayIndex >= replaySource.length) return;
    const timer = window.setTimeout(() => setReplayIndex(index => index + 1), 700);
    return () => window.clearTimeout(timer);
  }, [playing, replayIndex, replaySource.length]);

  const events = playing ? replaySource.slice(0, replayIndex) : liveEvents;
  const callIds = Array.from(new Set(events.map(event => event.call_id).filter(Boolean)));
  const activeCallId = selectedCallId && callIds.includes(selectedCallId) ? selectedCallId : callIds.at(0) ?? null;
  const calls = callIds.map((callId, index) => {
    const rows = events.filter(event => event.call_id === callId);
    const latestPrice = [...rows].reverse().find(event => event.kind === "price")?.payload.price;
    const mover = [...rows].reverse().find(event => typeof event.payload.mover === "string")?.payload.mover;
    const flags = rows.filter(event => event.kind === "red_flag").length;
    return {callId, order:String(index + 1).padStart(2,"0"), mover:String(mover ?? callId.replace(/^call-\d+-/, "").replaceAll("_", " ")), price:money(latestPrice), detail:flags ? `${flags} red flag${flags === 1 ? "" : "s"}` : `${rows.length} events`};
  });
  const visibleEvents = activeCallId ? events.filter(event => event.call_id === activeCallId) : events;

  const transcripts = visibleEvents.filter(e => e.kind === "transcript");
  const ledger = visibleEvents.filter(e => e.kind === "ledger_fact");
  const price = [...visibleEvents].reverse().find(e => e.kind === "price");
  const blocked = visibleEvents.filter(e => e.kind === "gate_blocked").length;
  const phase = String(transcripts.at(-1)?.payload.phase ?? "DISCOVERY");
  async function whisper(event: FormEvent) {
    event.preventDefault();
    const value = directive.trim();
    if (!value || pendingDirective) return;
    setPendingDirective(value);
    try {
      if (!activeCallId) throw new Error("select a call before sending a directive");
      const response = await fetch(`/api/whisper`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({call_id:activeCallId,directive:value})});
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `whisper ${response.status}`);
      setApiStatus("directive acknowledged");
    } catch (error) { setApiStatus(error instanceof Error ? error.message : "directive failed"); }
    finally { setPendingDirective(null); }
  }

  return <main>
    <header className="topbar">
      <div><span className="eyebrow">NATION / NEGOTIATOR</span><h1>Live negotiation war room</h1></div>
      <div className="status"><span className="pulse" aria-hidden="true"/> {apiStatus.toUpperCase()} <b>742ms</b></div>
    </header>

    <section className="hero-grid" aria-label="Negotiation overview">
      <div className="call-stack panel">
        <div className="panel-title"><span>CALL ORDER</span><small>outsider → favorite</small></div>
        {calls.map(call => <button onClick={()=>setSelectedCallId(call.callId)} className={`call ${call.callId===activeCallId ? "active" : ""}`} key={call.callId}>
          <b>{call.order}</b><span><strong>{call.mover}</strong><small>{call.callId}</small></span><span className="call-price">{call.price}<small>{call.detail}</small></span>
        </button>)}
      </div>

      <div className="trajectory panel">
        <div className="panel-title"><span>PRICE TRAJECTORY</span><small>Empire Relocation · live estimate</small></div>
        <div className="price-head"><strong>{price ? money(price.payload.price) : "$4,600"}</strong><span>−$700 <small>after cross-call evidence</small></span></div>
        <div className="chart" role="img" aria-label="Price fell from 4600 dollars to 3900 dollars; estimated floor band 3700 to 4050 dollars">
          <div className="band"><span>estimated floor band</span></div><div className="line"><i/><i/><i/><i/></div>
          <div className="axis"><span>$4.6k anchor</span><span>$4.1k cited</span><span>$3.9k offer</span></div>
        </div>
      </div>

      <aside className="score panel">
        <div className="panel-title"><span>TRUST GATE</span><small>fail-closed</small></div>
        <div className="blocked"><strong>{blocked}</strong><span>BLUFF<br/>BLOCKED</span></div>
        <dl><div><dt>Private leaks</dt><dd>0</dd></div><div><dt>Supported claims</dt><dd>7</dd></div><div><dt>Disclosure</dt><dd className="ok">verified</dd></div></dl>
      </aside>
    </section>

    <section className="work-grid">
      <div className="transcript panel">
        <div className="panel-title"><span>LIVE TRANSCRIPT</span><span className="phase">{phase}</span></div>
        <div className="transcript-body" aria-live="polite">
          {transcripts.map(event => <article key={event.seq} className={event.payload.speaker === "agent" ? "agent" : "them"}>
            <small>{String(event.payload.speaker).toUpperCase()} · {new Date(event.ts).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"})}</small>
            <p>{String(event.payload.text)}</p>{event.refs.length>0 && <a href="#ledger">evidence: {event.refs[0]}</a>}
          </article>)}
          {!transcripts.length && <p className="empty">Start fixture replay to reconstruct the recorded call.</p>}
        </div>
        <div className="replay"><button onClick={()=>{setReplayIndex(0);setPlaying(true)}}>▶ Replay full run</button><button onClick={()=>setPlaying(false)}>Live</button><span>{events.length}/{playing ? replaySource.length : liveEvents.length} journal events</span></div>
      </div>

      <div className="intel">
        <section className="panel ledger" id="ledger"><div className="panel-title"><span>EVIDENCE LEDGER</span><small>provenance required</small></div>
          {ledger.length ? ledger.map(e=><div className="fact" key={e.seq}><span>QUOTE</span><strong>{String(e.payload.value)}</strong><small>{String(e.payload.source)}</small></div>) : <div className="fact"><span>BENCHMARK</span><strong>$3,200–$6,200</strong><small>config:moving.yaml</small></div>}
          <div className="fact"><span>TACTIC</span><strong>Artificial deadline</strong><small>pressure · 94%</small></div>
        </section>
        <section className="panel whisper"><div className="panel-title"><span>JUDGE WHISPER</span><small>routes through gate</small></div>
          <form onSubmit={whisper}><label htmlFor="directive">Client directive</label><textarea id="directive" maxLength={500} value={directive} onChange={e=>setDirective(e.target.value)}/><button disabled={Boolean(pendingDirective)}>Queue core event</button></form>
          <p className={apiStatus === "directive acknowledged" ? "queued" : ""}>{apiStatus === "directive acknowledged" ? "Directive journaled — no direct speech bypass." : "The strategist may use it; the honesty gate still decides."}</p>
        </section>
      </div>

      <aside className="right-rail">
        <section className="panel latency"><div className="panel-title"><span>LATENCY</span><small>mouth → ear</small></div><strong>742<span>ms</span></strong><div className="meter"><i/></div><p>SIM target ≤800ms</p><ul><li>VAD <b>84</b></li><li>LLM TTFT <b>517</b></li><li>TTS <b>141</b></li></ul></section>
        <section className="panel discovery"><div className="panel-title"><span>DISCOVERY</span><small>Google Places</small></div><ol><li>Atlantic Moving Co <b>mapped</b></li><li>Hudson Van Lines <b>mapped</b></li><li>Empire Relocation <b>mapped</b></li></ol></section>
      </aside>
    </section>

    <footer><span>OFFLINE FIXTURE · DUAL-CHANNEL RECORDING</span><span>LIVE → SIM → CACHE → RECORDING</span><span>3/3 OUTCOMES GUARANTEED</span></footer>
  </main>;
}
