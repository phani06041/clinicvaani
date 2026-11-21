# ClinicVaani — a Telugu / Hindi / English voice front desk for small clinics

Built for **Build with Gemini (Hack2skill × Google Cloud)**, Problem Statement 03: Customer & Frontline Voice Agents.

A patient calls a Tier-2 clinic and talks in whatever they speak: Telugu, Hindi, English or a mix of them.
**Vaani** answers in real time using the **Gemini Live API** with native audio. She books, changes and cancels
appointments. She answers insurance questions from English policy documents and cites the clause. She raises
cashless pre-authorizations that a staff member approves live, and she hands emergencies and edge cases to a human.
The front-desk console shows each step as it happens.

> All clinic, doctor, patient and insurer data is **fictional demo data**.

**Idea deck:** [docs/idea-deck.pdf](docs/idea-deck.pdf) (submitted for the idea round).

## What it shows (mapped to the PS-03 demo checklist)

| Requirement | Where you see it |
|---|---|
| Live voice, natural turn-taking, **interruption** | Talk over Vaani: playback stops at once (`interrupted` from Gemini → player buffer flushed). The console logs "✋ Caller interrupted". |
| **Indian language / code-switched speech** with noise | Telugu, Hindi, Tenglish or Hinglish. The **Background noise** slider mixes traffic and crowd noise into the mic stream before it is sent. |
| **Grounded** in organisational knowledge | `check_insurance_coverage` (structured policy table) and `search_clinic_knowledge` (BM25 over policy wordings, clinic handbook and a **Hindi** lab-prep sheet). Vaani cites the clause, e.g. SUR-G 5.2. |
| **Real backend action** | `book_appointment`, `cancel_appointment`, `create_preauth_request`, `send_sms_confirmation` write to SQLite and appear in the console. |
| **Human oversight & hand-off** | Pre-auths wait for staff approval. When staff click **Approve**, the decision is pushed *into the live call* and Vaani tells the caller. Emergencies go to `escalate_to_human` and staff can **Take over call**. |
| **Visible latency** | Each turn measures the time from the end of the caller's speech to the first agent audio, shown per turn and as a p50 in the header. |
| Exception handling | Confirmation before booking (`NEEDS_CONFIRMATION`), `SLOT_TAKEN` with alternatives, `DUPLICATE_BOOKING`, invalid phone, insurance waiting period not yet completed, insurer not cashless at this clinic. |

## Architecture

```
Browser (mic 16 kHz PCM, noise mixer, 24 kHz player)
   │  WebSocket /ws/call (binary audio + JSON events)
FastAPI server ──── Gemini Live API (gemini-3.8-live, native audio, transcription, function calling)
   │                     │ tool_call
   │                     ▼
   ├── tools.py → clinic.py (SQLite: doctors, slots, patients, appointments, pre-auths, hand-offs, SMS outbox)
   │            → knowledge.py (BM25 over data/knowledge/*.md, English queries → English/Hindi docs)
   └── /ws/ops → front-desk console (tool timeline, staff queue, appointments, metrics)
        staff decisions → injected into the live session as [STAFF UPDATE]
```

The caller speaks Telugu. Gemini turns the question into an English tool query, retrieves from English or Hindi
documents, and answers in Telugu. That is the cross-language retrieval path. The API key stays on the server.

## Run it (macOS)

```bash
cd clinicvaani
cp .env.example .env        # paste your AI Studio key into GEMINI_API_KEY
./run.sh                    # creates .venv, installs, serves http://localhost:8000
```

Open it in Chrome and allow the microphone. **Use headphones** so Vaani doesn't hear herself.

| URL | Who it's for | What they see |
|---|---|---|
| `http://localhost:8000/patient` | **Patient / caller** | A phone-call screen: Vaani, call button, live captions, type-instead box. No clinic data. |
| `http://localhost:8000/staff` | **Clinic front-desk staff** (PIN) | Live call monitor, agent actions, approval queue (pre-auths, hand-offs), appointments, SMS outbox, metrics. |
| `http://localhost:8000/` | **Demo video** | Both side by side, so judges see the caller and the clinic at once. |

If the browser blocks the microphone, the patient screen falls back to a typed call and Vaani still answers by voice.
To use your phone as the patient, expose the server over HTTPS (e.g. `cloudflared tunnel --url http://localhost:8000`)
and open `/patient` there. Phones only allow the microphone on HTTPS.

Before recording, check that your key and model work:

```bash
source .venv/bin/activate
python scripts/check_gemini.py     # connects, asks a Tenglish question, confirms a tool call + audio
pytest -q                          # 8 tests: tools, exceptions, retrieval, and the full WS bridge with a fake Gemini
```

**Reset demo data** in the console, or `RESET_DB_ON_START=1`, gives you a clean clinic before each take.

### Demo patients (fictional)

| Phone | Name | Policy | Good for showing |
|---|---|---|---|
| 98480 12345 | Venkata Rao (Telugu) | Suraksha Gold, 14 months old | Cataract **not yet covered** (24-month waiting period); already booked with Dr. Lakshmi today, so **duplicate** detection |
| 98765 43210 | Sunita Sharma (Hindi) | Suraksha Gold, 30 months old | Cataract covered → **pre-auth** → staff approves live |
| 90000 11111 | Lakshmi Devi (Telugu) | Navjeevan Family Floater | Insurer **not cashless** here → reimbursement explanation |
| 91234 56780 | Arjun Naidu (English) | Suraksha Silver | OPD not covered, 10% co-pay on surgery |

## Several patients and staff at once

- **Patients:** every caller gets their own call ID and their own Gemini Live session. Every tool call and every staff
  update is tagged with that call ID, so an approval reaches the right caller. Slot booking runs under a lock, so two
  callers can't take the same slot: the second gets `SLOT_TAKEN` and is offered the next free times.
- **Caller verification:** saying a phone number doesn't prove who you are. Before Vaani shares or changes an
  existing patient's records (name, insurance on file, appointments, pre-auth, cancellation), she sends a 4-digit
  code by SMS and the caller reads it back. The code is shown only on the patient's screen. It is never sent to Gemini
  or to the staff console, codes expire after 10 minutes, and 3 wrong tries lock it. New bookings and general
  questions about a named insurer don't need a code.
- **Staff:** `/staff` requires a name and the clinic PIN (`STAFF_PIN` in `.env`) and keeps you signed in with a signed
  cookie for 12 hours. Each queue item can be decided **once**. A second click gets "already approved by Swathi"
  and the caller isn't told twice. Every decision records who made it.
- **Scaling beyond one server (roadmap):** live calls and the staff feed are held in memory in one process. To run
  several instances on Cloud Run, move the event feed to Redis or Cloud Pub/Sub and the database to Cloud SQL.
  Real staff accounts would use Google sign-in (Identity Platform) instead of one shared PIN.

## Moving to Google Cloud (after shortlisting)

Set `USE_VERTEX=1`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` in `.env`, run
`gcloud auth application-default login`, and set `GEMINI_LIVE_MODEL` to a Live model id your Vertex project offers.
Next steps that score on "Gemini Enterprise capability":
- Replace the BM25 retriever with **Vertex AI Search / RAG Engine** over the clinic's real documents.
- Deploy the server to **Cloud Run** and put **Cloud SQL** behind `clinic.py`.
- Connect a telephony channel (SIP or Exotel) so patients can call a real phone number.

## Commercial viability (fill in with your own numbers)

A 1–3 doctor clinic in a Tier-2 town takes roughly *N* calls a day. Many go unanswered during peak OPD hours, and
every missed call is a missed consultation. The console tracks calls, bookings made by the agent, the share of calls
resolved without a human, hand-offs and response latency, so you can report **cost per resolved call** against a
receptionist's time. Pricing idea: a flat monthly fee per clinic plus a per-minute charge. Use current Gemini Live
pricing for the per-minute model cost; it is not hard-coded here.

## Project layout

```
app/main.py        FastAPI server, Gemini Live bridge, ops hub, staff endpoints
app/clinic.py      SQLite back-office and the business actions
app/tools.py       Function declarations for Gemini and the dispatcher
app/knowledge.py   BM25 retriever over data/knowledge
app/prompts.py     System instruction (language, grounding, safety, booking flow)
static/            index.html, app.js, audio-worklets.js (capture + noise + player), style.css
data/              coverage.json and knowledge/*.md (fictional policies, handbook, Hindi lab prep)
scripts/check_gemini.py, tests/
```
