# Deploying to Railway

This app is a long-lived FastAPI service with persistent WebSockets and a background
reminder loop, so it needs a container host (Railway, Render, Fly.io), **not** a serverless
platform like Vercel. The included `Dockerfile` and `railway.json` make it a Docker deploy.

## Steps

1. **Push the repo to GitHub** (already the case here).

2. **Create the Railway project**
   - railway.app → **New Project** → **Deploy from GitHub repo** → pick this repo.
   - Railway detects the `Dockerfile` and builds it.

3. **Add Postgres**
   - In the project: **New** → **Database** → **PostgreSQL**.
   - It exposes a `DATABASE_URL`. On the app service, add a variable
     `DATABASE_URL = ${{Postgres.DATABASE_URL}}` (Railway's reference syntax).
   - The app applies its own migrations (incl. the `btree_gist` extension) and seeds
     `config.yaml` + `trailer_config.yaml` on boot — nothing to run manually.

4. **Get the public domain**
   - App service → **Settings → Networking → Generate Domain** → e.g.
     `clinic-agent-production.up.railway.app`.

5. **Set environment variables** (app service → **Variables**). At minimum:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | `TELNYX_API_KEY` / `TELNYX_PUBLIC_KEY` / `TELNYX_NUMBER` | your Telnyx values |
   | `STREAM_SECRET` | a long random string |
   | `PUBLIC_STREAM_URL` | `wss://<your-domain>/telnyx/stream/<STREAM_SECRET>` |
   | `AI_PROVIDER` | `ai_studio` (or `vertex` for production) |
   | `GEMINI_API_KEY` | your Gemini key |
   | `SLOT_TOKEN_SECRET` | a long random string |
   | `DASHBOARD_PASSWORD` | the console login password |
   | `TRAILER_CONFIG` | `trailer_config.yaml` (enables the trailer agent) |
   | `GEMINI_VOICE` / `TRAILER_GEMINI_VOICE` | e.g. `Aoede` / `Puck` |

   (Optional tuning: `VAD_SILENCE_MS`, `VAD_END_SENSITIVITY`, `AGENT_INTERRUPTIBLE`,
   `AI_ENABLE_AFFECTIVE_DIALOG`. See `.env.example`.)

   > `PUBLIC_STREAM_URL` depends on the domain from step 4, so set it (and redeploy) after
   > the domain exists.

6. **Point Telnyx at the domain** (portal):
   - Voice Call Control application webhook → `https://<your-domain>/telnyx/webhook`; assign
     your number to it.
   - Messaging profile webhook → `https://<your-domain>/telnyx/messaging`.

7. **Verify**: open `https://<your-domain>/health` (should be `{"status":"ok",...}`) and
   `https://<your-domain>/login`.

## Notes

- **Health check** is `/health` (configured in `railway.json`).
- **Cost**: Railway is usage-based (an always-on service + Postgres); it is not free
  indefinitely.
- **Compliance**: `AI_PROVIDER=ai_studio` is **not** BAA-eligible and Railway is not a
  HIPAA environment — do not put real patient traffic on this setup. Use `vertex` + an
  appropriate hosting/BAA posture for production.
- **The media WebSocket** is guarded only by `STREAM_SECRET` (Telnyx frames are unsigned) —
  keep that secret strong and out of logs.
