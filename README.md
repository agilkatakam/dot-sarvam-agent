# DOT voice agent: Sarvam-only test build

An AI voice agent that answers a test call, gets past the receptionist, pitches DOT to the
right person (placement, academics, library), books a walkthrough on a real calendar, and
ends every call with a tagged outcome. Speech-to-text, the LLM and text-to-speech all run on
**Sarvam AI** with one server-side API key. Every call is measured against the client's KPI
framework.

```
caller (browser or phone) ⇄ this server ⇄ Sarvam STT (saaras:v3-realtime)
                                         → Sarvam LLM (sarvam-105b-conversations) ⇄ tools: calendar, end_call
                                         → Sarvam TTS (bulbul:v3)
```

## Run it

1. Get a free key at <https://dashboard.sarvam.ai> (₹100 free credit is roughly 15 to 20 test calls).
2. Set up:
   ```bash
   cd sarvam-agent
   uv venv .venv && uv pip install --python .venv/bin/python -e .
   cp .env.example .env        # paste SARVAM_API_KEY, change DASHBOARD_PASSWORD
   .venv/bin/python -m agent.server
   ```
3. Call it: open <http://localhost:7860> and press **Start call**.
4. Manage it at <http://localhost:7860/dashboard>. Use any username; the password is `DASHBOARD_PASSWORD`.

| Page | What it's for |
|---|---|
| `/` | Public test-call page (microphone, live captions). |
| `/dashboard` | KPIs, funnel, cost, comparison by prompt version, call list with recordings and review, CSV export. |
| `/studio` | **Prompt studio**: edit the prompt and see exactly what is sent to the model; try it in a text chat. |
| `/admin` | Runtime settings (model, voice, turn-taking, limits), voice preview, calendar, booked meetings, do-not-call list. |

### Friends testing on their own computers

**1. Make the kit** (one zip, about 0.3 MB):
```bash
uv run python -m scripts.make_tester_kit
```
This writes `dist/DOT-Voice-Agent-Test.zip`. Send it on WhatsApp as a Document. Each friend
signs up at dashboard.sarvam.ai for their own free key (₹100 credit, about 15 calls). The
first start asks for the key, checks it with Sarvam, and asks again if it's wrong. To put
your own key in the kit instead, add `--with-key`.

**2. Friends follow `READ ME FIRST.txt`** inside the zip: unzip, double-click **Start**, make
calls in the browser page that opens, then click **"I'm finished: save my results"**. That
puts `dot-test-results-<name>-<date>.zip` in their Downloads folder, and they send it back.
The first start downloads about 5 minutes of setup; later starts take seconds. Windows, Linux
and Apple Silicon Macs (macOS 14+) are supported; Intel Macs are not.

**3. Merge everything they send** and open it in the dashboard:
```bash
uv run python -m scripts.merge_results ~/Downloads/dot-test-results-*.zip
```
This writes `results/` (gitignored), which holds `all_calls.csv` (one spreadsheet with a `tester` column),
`transcripts/`, `recordings/`, each tester's `logs/`, and the database. It then serves the
dashboard on <http://localhost:7860/dashboard>. Running it again with new zips adds only the
new calls.

### Let anyone call it (free public link)

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:7860
```
Share the `https://….trycloudflare.com` URL it prints. It works from any phone browser,
with no signup, for as long as your laptop and the server are running.

### Optional: a real phone number (Twilio trial)

Trial numbers are US numbers and only accept calls from numbers you verify in the Twilio
console. Set the number's "A call comes in" webhook to `https://<your-tunnel>/twilio/voice`
(HTTP POST), and add `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` to `.env` so the agent can hang up.

## Prompt studio

The prompt is a structured document of sections, not one big string:

- **Opening line**: spoken word for word, never generated. It must disclose that this is an AI and that the call may be recorded; saving is blocked otherwise.
- **Sections**, in order. Each one can be switched on or off, reordered, renamed, added or deleted. There are four kinds:
  - **text**: identity, tone, booking rules, ending, and so on.
  - **list**: for example, facts the agent may state.
  - **objections**: trigger and response pairs.
  - **personas**: name, tag, typical roles, pain point, pitch and discovery questions. Each persona tag also becomes an allowed value of `persona` in the `end_call` tool, so adding a persona here teaches the agent to classify it.
- **Variables** like `{org}`, `{today}`, `{calendar_days}` and `{next_steps}` are filled in at call time.

The right-hand pane shows four views:

- The full rendered system prompt.
- Each section's rendered text.
- The exact chat-completions request a call starts with: model, temperature, max tokens, messages and the tools JSON.
- A **test chat**: type as the person who picked up. It uses the prompt as edited (even unsaved), runs real calendar lookups, and makes bookings as dry runs. It costs paise per message.

Every save creates a **version**. The active version is used from the next call, each call records its version, and the dashboard compares KPIs across versions. You can switch back to any old version, or reset to the factory default in `agent/script.yaml`.

## Booking meetings

The agent calls `check_availability` for the day the person suggests and offers real free
slots. It then calls `book_meeting` with the agreed slot, which is re-checked before booking.
A call only counts as `meeting_booked` if a booking really exists. If the model claims a
meeting without one, the call is downgraded.

Meeting hours, length, working days, lead time and timezone are set in `.env`.

- **Built-in calendar (default)**: bookings appear on `/admin`, with an `.ics` invite per meeting and a cancel button.
- **Google Calendar**:
  1. Put a Desktop-app OAuth client JSON at `secrets/credentials.json`.
  2. Run `.venv/bin/python -m scripts.connect_google_calendar` once.
  3. From then on, availability includes the calendar's existing events, and bookings create a Google event with a Meet link. Invites go to the contact and `MEETING_TEAM_EMAILS`.

  Details are in the script's docstring. Delete `secrets/token.json` to switch back to the built-in calendar.

## What gets measured

| KPI | How it's measured | Automatic? |
|---|---|---|
| Connectivity rate | calls where the caller actually heard the agent ÷ call attempts | ✅ |
| Call completion rate | finished calls that reached an outcome ÷ finished calls | ✅ |
| Response latency | caller stops speaking → agent audio starts, for every reply. Reported as share ≤ 600 ms (the client target) and ≤ 1.5 s (working target), plus p50/p90 and the STT/LLM/TTS split | ✅ (server side; excludes network and phone-line delay) |
| Speech recognition accuracy | 1 − word error rate against a reviewer's corrected transcript (filled in automatically for simulated calls) | 👤 review / 🤖 simulated |
| Intent detection accuracy | agent's outcome tag vs reviewer's tag, with a list of the mistakes | 👤 review |
| Meetings per 1,000 dials | north-star metric | ✅ |
| Decision-maker reach | finished calls that got past reception to a persona | ✅ |
| Persona routing accuracy | agent's persona tag vs reviewer's | 👤 review |
| Positive intent rate | interest high or medium | ✅ |
| Cost | ₹ per call, per connected minute, per meeting, and spent on time-wasters, from metered tokens, TTS characters and minutes | ✅ (estimate, ex-GST) |

**Outcomes**: each finished call gets exactly one.

- `abandoned`: the caller hung up first, or the call went idle.
- `polite_decline`
- `meeting_booked`
- `call_again_later`
- `initial_discussion`
- `other`: wrong number or voicemail.

The agent also tags **seriousness**: `genuine`, `gatekeeper`, `time_waster` or `unclear`. A time-waster never gets a callback.

A reviewer can override the outcome and the persona; the override is what the dashboard counts.

**Compliance**:

- The fixed AI disclosure and recording notice are spoken first, and saving a prompt without them is blocked.
- Anyone who opts out goes on the **do-not-call list** (phone, email, institution), which you can manage on `/admin`.
- `GET /api/dnc/check` is there for a future outbound dialer.

**Not meaningful in testing**:

- Outcome rates as business numbers, because testers role-play.
- Real dial connectivity. There is no outbound dialing yet; that needs DLT registration.
- Phone-line speech accuracy. Browser audio is cleaner than 8 kHz phone audio; use the Twilio path to test that.

## Simulated calls

```bash
.venv/bin/python -m scripts.simulate_call --list          # scenarios
.venv/bin/python -m scripts.simulate_call placement_books # one call
.venv/bin/python -m scripts.simulate_call all --label     # every scenario, scored
```

An LLM plays the contact: a placement officer who books, a receptionist who declines, a busy dean, a Hinglish librarian, a joker, or someone opting out. The simulator talks to the running server over the same audio WebSocket as the browser and prints:

- per-reply latency
- whether the agent's outcome matched the scenario
- persona and seriousness
- STT word error rate
- cost

Simulated calls are tagged `simulated`, and the dashboard can filter them in or out. Each one costs roughly ₹1 to ₹3 of Sarvam credit.

## Where things live

- `agent/script.yaml`: the factory-default prompt document. Day to day, edit the prompt in `/studio`.
- `agent/prompt.py`: prompt structure, validation, rendering and versions.
- `agent/runtime.py`: settings editable from `/admin` (stored in `data/runtime.json`).
- `agent/call.py`: the per-call pipeline. It handles Smart Turn end-of-turn detection, the filler line when the model is slow, idle and time-limit hang-ups, and all KPI and cost capture.
- `agent/tools.py`: `check_availability`, `book_meeting` and `end_call`, plus the guards on what the model reports.
- `agent/meetings.py`: slots, booking, Google Calendar and `.ics`.
- `agent/playground.py`: the studio's text chat.
- `agent/kpis.py`: KPI definitions (pure functions, unit-tested).
- `agent/server.py`: the web server, the browser and Twilio endpoints, and the dashboard API.
- `data/`: `calls.db` (SQLite) and `recordings/*.wav` (left = caller, right = agent).

Tests: `.venv/bin/python -m pytest -q`
