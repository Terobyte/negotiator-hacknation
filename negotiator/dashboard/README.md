# Negotiator War Room

Local-first vinext dashboard for the negotiation journal. Set
server-only `NEGOTIATOR_API` and `DASHBOARD_BEARER_TOKEN` to connect the
authenticated replay, journal WebSocket ticket, and whisper BFF routes. The
browser never receives the shared bearer token. `NEXT_PUBLIC_NEGOTIATOR_WS`
may contain the non-secret public WSS origin. Without backend settings, the
bundled fixture remains available for an offline demo.

Every BFF route also requires the Sites-injected
`oai-authenticated-user-email` header and membership in the server-only,
comma-separated `DASHBOARD_ALLOWED_EMAILS` allowlist. An empty allowlist fails
closed. Direct deployments must put an identity-aware proxy in front of the
app that strips any client-supplied copy of that header before setting it.

`npm run dev`, `npm run lint`, and `npm test` cover local development and the
production render. Hosting metadata is retained for Sites, but deployment is
not part of this repository workflow.

The backend requires `DASHBOARD_BEARER_TOKEN` and `TWILIO_AUTH_TOKEN`.
`DASHBOARD_ALLOWED_ORIGINS` is a comma-separated allowlist; localhost origins
are used only when it is unset. Live provider IDs, API keys, a public HTTPS/WSS
origin, and a verified Twilio number remain deployment-time credentials.
