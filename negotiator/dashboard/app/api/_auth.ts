const USER_HEADER = "oai-authenticated-user-email";
const SIGNATURE_HEADER = "oai-authenticated-user-signature";
const TIMESTAMP_HEADER = "oai-authenticated-user-timestamp";

function hex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left: string, right: string): boolean {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index++) difference |= (left.charCodeAt(index) || 0) ^ (right.charCodeAt(index) || 0);
  return difference === 0;
}

export async function authorizeWorkspaceRequest(request: Request): Promise<Response | null> {
  const email = request.headers.get(USER_HEADER)?.trim().toLowerCase();
  const signature = request.headers.get(SIGNATURE_HEADER)?.trim().toLowerCase();
  const timestamp = request.headers.get(TIMESTAMP_HEADER)?.trim();
  const secret = process.env.DASHBOARD_IDENTITY_HMAC_SECRET ?? "";
  const allowed = new Set((process.env.DASHBOARD_ALLOWED_EMAILS ?? "")
    .split(",").map(value => value.trim().toLowerCase()).filter(Boolean));
  if (!email) return Response.json({detail:"workspace identity required"},{status:401});
  if (!secret) return Response.json({detail:"DASHBOARD_IDENTITY_HMAC_SECRET is not configured"},{status:503});
  const issuedAt = Number(timestamp);
  if (!timestamp || !Number.isFinite(issuedAt) || Math.abs(Date.now() / 1000 - issuedAt) > 300 || !signature) {
    return Response.json({detail:"signed workspace identity required"},{status:401});
  }
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name:"HMAC",hash:"SHA-256"}, false, ["sign"]);
  const expected = hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${timestamp}.${email}`)));
  if (!constantTimeEqual(signature, expected)) return Response.json({detail:"invalid workspace identity signature"},{status:401});
  if (!allowed.size) return Response.json({detail:"DASHBOARD_ALLOWED_EMAILS is not configured"},{status:503});
  if (!allowed.has(email)) return Response.json({detail:"workspace membership denied"},{status:403});
  return null;
}

export function requireSameOrigin(request: Request): Response | null {
  const origin = request.headers.get("origin");
  return origin === new URL(request.url).origin ? null : Response.json({detail:"same-origin required"},{status:403});
}
