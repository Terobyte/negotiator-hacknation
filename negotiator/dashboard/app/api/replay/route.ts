function backend() {
  const url = process.env.NEGOTIATOR_API;
  const token = process.env.DASHBOARD_BEARER_TOKEN;
  if (!url || !token) throw new Error("NEGOTIATOR_API and DASHBOARD_BEARER_TOKEN are required");
  return {url: url.replace(/\/$/, ""), token};
}

export async function GET(request: Request) {
  const denied = await authorizeWorkspaceRequest(request);
  if (denied) return denied;
  const {url, token} = backend();
  const incoming = new URL(request.url);
  const source = incoming.searchParams.get("source") === "journal" ? "journal" : "full_run.jsonl";
  const response = await fetch(`${url}/api/journal/replay?fixture=${encodeURIComponent(source)}&after_seq=${encodeURIComponent(incoming.searchParams.get("after_seq") ?? "0")}`, {headers:{Authorization:`Bearer ${token}`,Origin:incoming.origin}});
  return new Response(response.body, {status:response.status,headers:{"Content-Type":"application/json","Cache-Control":"no-store"}});
}
import { authorizeWorkspaceRequest } from "../_auth";
