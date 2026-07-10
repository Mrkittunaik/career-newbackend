# CareerOS Backend

A FastAPI + MongoDB backend built to match the API your `careeros-site` frontend
already expects (`js/api/*.js`, `js/ws/dashboardSocket.js`, `payment/payment.js`).
There was no backend in the repo before this — this is a new service, not an
edit of an existing one.

## Stack
- **FastAPI** (async, auto docs at `/docs`)
- **MongoDB** via Motor (async driver)
- **JWT** auth (python-jose) + bcrypt password hashing
- **WebSocket** endpoint for the live dashboard
- Stubs (with clear wiring instructions) for **Google Sign-In**, **Gmail OAuth**, and **Razorpay** — these return a `501` with an explanatory message until you add real API keys, instead of failing silently.

## Setup

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: at minimum set MONGO_URL and JWT_SECRET

# Local MongoDB — easiest option is Docker:
docker run -d -p 27017:27017 --name careeros-mongo mongo:7

uvicorn app.main:app --reload --port 8000
```

API is now live at `http://localhost:8000/api/v1`, matching `API_BASE` in
`js/api/client.js`. Interactive docs: `http://localhost:8000/docs`.

Open the frontend (e.g. `python3 -m http.server 5500` from the site root, or
just open `index.html`) and it should talk to this backend directly — no
frontend changes were needed.

## What's fully working right now
- Signup / login / JWT auth (`/auth/*`)
- Profile: about paragraph, document upload (file or link), delete (`/profile*`)
- Job requests, applications list (search/status filter), daily limit (`/jobs*`)
- Bot sessions + HR contacts (`/sessions`, `/hr-contacts`)
- Settings: bot token regeneration, storage mode, AI provider (`/settings*`)
- Live dashboard WebSocket (`/ws/dashboard`) with a `ConnectionManager` ready
  for a bot/worker process to push `bot_status`, `job_progress_update`,
  `hr_contact_added`, `daily_counter_update`, `application_reply_received`
  events (see `app/routers/ws.py`)

## What needs your API keys to go fully live
Each of these already has real, working code behind it — they just need
credentials in `.env` (see comments in `.env.example` for where to get them):

| Feature | Env vars | File |
|---|---|---|
| Google Sign-In | `GOOGLE_CLIENT_ID` | `app/routers/auth.py` |
| Gmail OAuth + reply scanning | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REDIRECT_URI` | `app/services/gmail.py` |
| Razorpay checkout | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | `app/services/razorpay_client.py` |

Until those are set, the relevant endpoints return HTTP 501 with a message
telling you what's missing — the frontend already displays these gracefully
(e.g. `payment.js` shows "Checkout isn't connected yet").

One thing you'll still need to build separately: the actual **job-scanning /
auto-apply bot**. This backend only stores job requests (`POST /jobs/request`
writes to a `job_requests` collection) and exposes results (`GET /jobs`,
`/sessions`, `/hr-contacts`) — a worker process should read queued requests,
do the scanning/applying, and write back into `job_applications` /
`hr_contacts` / `bot_sessions`, pushing WebSocket events as it goes
(`manager.send_to_user(...)` in `app/routers/ws.py` is ready for that).

## Project layout
```
backend/
  app/
    core/       # config, db connection, JWT/password helpers
    routers/    # one file per frontend api/*.js module
    services/   # gmail.py, razorpay_client.py — external integrations
    main.py     # FastAPI app, CORS, router wiring
  requirements.txt
  .env.example
```

## Deploying

- **Docker**: `docker build -t careeros-backend . && docker run -p 8000:8000 --env-file .env careeros-backend` (point `MONGO_URL` at a reachable Mongo — Atlas is easiest for prod).
- **Render / Railway / Heroku-style PaaS**: `Procfile` is included; just set the env vars from `.env.example` in the dashboard.
- Whatever you use, set `CORS_ORIGINS` to your real deployed frontend URL, and update `API_BASE` in the frontend's `js/api/client.js` to your deployed backend URL (it currently points at `http://localhost:8000/api/v1` for local dev).

## Internal worker API
`app/routers/internal.py` exposes endpoints (`/internal/*`, protected by `INTERNAL_API_SECRET`, not user JWTs) for a future job-scanning/auto-apply worker to write results and push live WebSocket events — `/internal/applications`, `/internal/hr-contacts`, `/internal/bot-status`, `/internal/job-requests/queued`, `/internal/push-event`. The worker process itself (the thing that actually scans job boards and applies) isn't built yet — build that separately whenever you're ready, using these endpoints to report back.


- AI provider API keys are currently stored in Mongo in plaintext — encrypt at rest before shipping.
- Self-hosted Mongo mode (`/settings/storage`) only records the user's preference; actually migrating their data is a TODO background job.
- CORS origins default to localhost — set `CORS_ORIGINS` in `.env` to your real frontend URL(s) before deploying.
- Rate-limit `/auth/login` and `/auth/signup` before going live.
