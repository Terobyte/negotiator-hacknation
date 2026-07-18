import { authorizeWorkspaceRequest, requireSameOrigin } from "../_auth";

export async function POST(request: Request) {
  const denied = authorizeWorkspaceRequest(request) ?? requireSameOrigin(request);
  if (denied) return denied;
  const url = process.env.NEGOTIATOR_API?.replace(/\/$/, "");
  const token = process.env.DASHBOARD_BEARER_TOKEN;
  if (!url || !token) return Response.json({detail:"server integration is not configured"},{status:503});
  const response = await fetch(`${url}/api/whisper`, {method:"POST",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:await request.text()});
  return new Response(response.body,{status:response.status,headers:{"Content-Type":"application/json","Cache-Control":"no-store"}});
}
