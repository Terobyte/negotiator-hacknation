# Negotiator War Room

Local-first vinext dashboard for the negotiation journal. Set
server-only `NEGOTIATOR_API` and `DASHBOARD_BEARER_TOKEN` to connect the
authenticated replay, journal WebSocket ticket, and whisper BFF routes. The
browser never receives the shared bearer token. `NEXT_PUBLIC_NEGOTIATOR_WS`
may contain the non-secret public WSS origin. Without backend settings, the
bundled fixture remains available for an offline demo.

Every BFF route also requires an identity proxy to inject
`oai-authenticated-user-email`, a Unix timestamp in
`oai-authenticated-user-timestamp`, and a hex HMAC-SHA256 signature in
`oai-authenticated-user-signature`. The signed message is
`<timestamp>.<lowercase-email>` using server-only
`DASHBOARD_IDENTITY_HMAC_SECRET`; signatures expire after five minutes. The
email must also belong to the comma-separated `DASHBOARD_ALLOWED_EMAILS`
allowlist. Missing secrets, stale signatures, and empty allowlists fail closed.

`npm run dev`, `npm run lint`, and `npm test` cover local development and the
production render. Hosting metadata is retained for Sites, but deployment is
not part of this repository workflow.

The backend requires `DASHBOARD_BEARER_TOKEN`; `TWILIO_AUTH_TOKEN` is required
only when live Twilio is explicitly enabled with
`NEGOTIATOR_LIVE_ENABLED=true`. The estimator webhook separately
requires `ELEVENLABS_WEBHOOK_SECRET` as a Bearer credential.
`DASHBOARD_ALLOWED_ORIGINS` is a comma-separated allowlist; localhost origins
are used only when it is unset. Live provider IDs, API keys, a public HTTPS/WSS
origin, and a verified Twilio number remain deployment-time credentials.
