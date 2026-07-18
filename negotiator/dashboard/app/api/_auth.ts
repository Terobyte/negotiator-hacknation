const USER_HEADER = "oai-authenticated-user-email";

export function authorizeWorkspaceRequest(request: Request): Response | null {
  const email = request.headers.get(USER_HEADER)?.trim().toLowerCase();
  const allowed = new Set((process.env.DASHBOARD_ALLOWED_EMAILS ?? "")
    .split(",").map(value => value.trim().toLowerCase()).filter(Boolean));
  if (!email) return Response.json({detail:"workspace identity required"},{status:401});
  if (!allowed.size) return Response.json({detail:"DASHBOARD_ALLOWED_EMAILS is not configured"},{status:503});
  if (!allowed.has(email)) return Response.json({detail:"workspace membership denied"},{status:403});
  return null;
}

export function requireSameOrigin(request: Request): Response | null {
  const origin = request.headers.get("origin");
  return origin === new URL(request.url).origin ? null : Response.json({detail:"same-origin required"},{status:403});
}
