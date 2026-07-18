import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request("http://localhost/", {headers:{accept:"text/html"}}),
    {ASSETS:{fetch:async()=>new Response("Not found",{status:404})}},
    {waitUntil(){},passThroughOnException(){}});
}

test("renders the complete local war room", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Negotiator War Room<\/title>/i);
  for (const label of ["PRICE TRAJECTORY","TRUST GATE","LIVE TRANSCRIPT","EVIDENCE LEDGER","JUDGE WHISPER","LATENCY","DISCOVERY"]) assert.match(html, new RegExp(label));
  assert.match(html, /Hi, I(?:&#x27;|')m an AI assistant calling on behalf of a client\./);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Your site is taking shape/);
});

test("client uses authenticated replay, cursor WebSocket, and acknowledged whisper", async () => {
  const [source,replayProxy,whisperProxy,ticketProxy,auth] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/replay/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/whisper/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/journal-ticket/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/_auth.ts", import.meta.url), "utf8"),
  ]);
  assert.match(source, /\/api\/replay\?after_seq=/);
  assert.match(source, /\/ws\/journal\?ticket=/);
  assert.doesNotMatch(source, /NEXT_PUBLIC_DASHBOARD_TOKEN|Bearer \$\{apiToken\}|\?token=/);
  assert.match(source, /\/api\/whisper/);
  assert.match(source, /maxLength=\{500\}/);
  assert.match(source, /replaySource\.slice\(0, replayIndex\)/);
  assert.match(source, /2 \*\* attempts/);
  assert.match(source, /liveCursor/);
  for (const proxy of [replayProxy,whisperProxy,ticketProxy]) assert.match(proxy,/process\.env\.DASHBOARD_BEARER_TOKEN/);
  assert.match(auth,/oai-authenticated-user-email/);
  assert.match(auth,/DASHBOARD_ALLOWED_EMAILS/);
  for (const proxy of [replayProxy,whisperProxy,ticketProxy]) assert.match(proxy,/authorizeWorkspaceRequest/);
});
