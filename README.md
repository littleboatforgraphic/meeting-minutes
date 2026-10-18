# meeting-minutes

Meeting secretary agent for the Agent-as-a-Service line. It records a meeting from a phone or laptop
(in person, or the audio of a Zoom/Teams/Meet/Webex call), transcribes mixed Mandarin/English/Malay/Cantonese
speech with Ilmu ASR, drafts English minutes and action items with Ilmu Claw, sends the draft to the chair for
approval, then gives every attendee their own task list by email, Telegram or WhatsApp.

**One copy per company.** Each company runs its own Docker Compose stack with its own `company.yaml`, `.env`,
database, data volume, Telegram bot and llm-gateway key. Nothing is shared between companies, so the
database has no `company_id`.

## How a meeting flows

1. **Record**: the Meeting app (a PWA served by this stack) records 16 kHz PCM and uploads it in ~10 s chunks,
   so 2-hour meetings never need a big upload. Mic choice includes external/USB mics; on a laptop
   "Capture meeting audio" adds a shared tab/screen's audio. Audio files can also be uploaded, or sent as
   voice notes/files to the company's Telegram bot or WhatsApp number.
2. **Transcribe**: a worker splits the audio at pauses (WebRTC VAD), sends each segment to the ASR model via
   llm-gateway (`op:asr`), with attendee names and company terms as the prompt. Ilmu ASR has no timestamps or
   speaker labels, so speakers are grouped locally with an ECAPA voice embedding (CPU) and labelled S1, S2, ...
   The live transcript appears in the app while recording.
3. **Voiceprints** (optional): if allowed for the company and switched on for the meeting, the speakers the chair
   names are stored as voiceprints, so later meetings auto-label them. People can opt out.
4. **Draft**: after Stop, audio is deleted and Ilmu Claw writes structured minutes (summary, agenda, decisions,
   action items with owner and deadline). Deadlines are resolved from the meeting date ("next Friday");
   if none was said the deadline stays blank with the remark "No deadline stated in meeting".
   Owners are matched to the directory conservatively; unknown names stay unassigned.
5. **Approve**: the chair gets a link, edits anything (owners, deadlines, speaker names) and approves.
   Nothing reaches attendees before this.
6. **Distribute**: tasks are created, each attendee gets the minutes PDF plus their own tasks
   (email if the company has SMTP, and WhatsApp or Telegram). They update status in the app,
   with a Telegram button, or by replying `done 12`. Reminders go out before deadlines and for overdue tasks.

**Retention** (per company, defaults): audio deleted once transcribed; transcript 7 days; minutes and tasks 90 days.

## Configuration

- `company.yaml`: non-secret settings: company name/timezone, ASR/LLM models, prompt terms, voice settings,
  channels, login method, retention, reminders. See [config/company.example.yaml](config/company.example.yaml).
- `.env`: secrets and URLs: `MM_SECRET_KEY`, `LLM_BASE_URL`/`LLM_API_KEY` (gateway key
  `<client>/meeting-minutes/prod`), SMTP, Telegram, WhatsApp. See [.env.example](.env.example).

Login (`auth.method`): `password` (default), `otp` (one-time code by email/Telegram/WhatsApp) or `password+otp`.
With `remember_device: true`, people stay signed in until they log out, `session_days` passes, or the app version
changes (set `auth.logout_on_app_update`); with `false` they are signed out when the browser is closed.

Models are OpenAI-compatible and go through llm-gateway, so ASR seconds and LLM tokens are billed in MYR on the
company's key. Another ASR model only needs `asr.model` (and the gateway route) changed.

## Deploy a company

```bash
scripts/new-company.sh acme "Acme Wire Mesh Sdn Bhd" 8101   # creates deploy/companies/acme/
# edit deploy/companies/acme/company.yaml and .env
cd deploy/companies/acme
docker compose --env-file .env up -d --build
docker compose exec web mm add-person --name "Admin" --email admin@acme.my --admin --password
docker compose exec web mm import-people /data/hr.xlsx       # or People > Import in the app
```

Then point the HTTPS reverse proxy at `127.0.0.1:8101` (the microphone only works over HTTPS) and register
the webhooks:

- Telegram: `curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" -d url=$MM_PUBLIC_BASE_URL/webhooks/telegram -d secret_token=$TELEGRAM_WEBHOOK_SECRET`
- WhatsApp (Meta app dashboard): callback `$MM_PUBLIC_BASE_URL/webhooks/whatsapp`, verify token `WHATSAPP_VERIFY_TOKEN`,
  subscribe to `messages`. Outside the 24-hour window Meta requires an approved template (`notifiers.whatsapp.template_name`).

People link Telegram from **Me > Link Telegram** in the app. Build with `VOICE=0` for a smaller image without
speaker grouping/voiceprints (everything is then one unlabelled speaker).

## CLI

`mm check | init-db | serve | worker | add-person | import-people | invite <id> | process <file> | purge`

`mm process meeting.m4a --title "Weekly" --chair 1 --attendee 2` runs a file end to end without the app.

## Development

```bash
uv sync --extra voice    # or plain `uv sync` without speaker grouping
uv run ruff check .
uv run pytest -q         # fake ASR/LLM/channels; no network or API key needed
MM_COMPANY_CONFIG=config/company.example.yaml MM_DATA_DIR=./data uv run mm serve
uv run mm worker         # second terminal
```

System packages: `ffmpeg` (audio decoding) and Pango (minutes PDF, via WeasyPrint).

## Not in this version

- Bot that joins online meetings by itself (phase 2; would use platform SDKs/a headless browser, models stay on the gateway).
- ERP / M365 / Google / ClickUp task connectors and calendar import (`tasks.provider`, `calendar.provider` are the extension points).
- Minutes in languages other than English, and the central portal (only `/api/central/info` exists, behind `CENTRAL_UI_TOKEN`).
- Self-hosted GPU ASR/LLM for strict data-policy clients (point `LLM_BASE_URL` at it; no code change expected).
