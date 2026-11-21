# 2-minute demo video script

Setup: sign in at `/staff` (name + PIN), then reset demo data. Caller line on the left, console visible. Headphones on. Use screen recording with mic audio
**and** system audio so Vaani's voice is captured.

| Time | You do / say | What the viewer sees |
|---|---|---|
| 0:00–0:10 | Voice-over: "When the OPD is busy, nobody at a small clinic can pick up the phone. ClinicVaani answers in the caller's own language and actually gets things done." | Console with metrics at zero |
| 0:10–0:35 | **Start call.** Telugu/Tenglish: "Namaskaram, naaku Monday eye doctor appointment kaavali, morning lo." While Vaani is reading out times, **interrupt**: "Aagandi, saayantram unte better." | Bubble tagged *Telugu+English*; ✋ interrupted event; `find_doctors` → `check_slots` in the timeline; Vaani says the eye doctor only works mornings and offers morning slots |
| 0:35–0:55 | Accept a slot, give name "Sunita Sharma", phone 98765 43210. Vaani reads it back; say "avunu" (yes). | `lookup_patient`, then `book_appointment` (ok), then `send_sms_confirmation`; a new row lights up in Appointments; a Telugu SMS appears in the outbox |
| 0:55–1:20 | Turn **Background noise** to ~40%. Switch to Hinglish: "Aur mera cataract operation insurance mein cover hoga? Cashless?" Vaani sends a code first: read the 4 digits from the SMS on the patient screen. | `send_verification_code` → `verify_caller` (privacy check), then `check_insurance_coverage` → covered, SUR-G 5.2 clause tag; Vaani answers in Hindi/Hinglish and offers a pre-auth; `create_preauth_request` → PA in Staff queue |
| 1:20–1:35 | On the console click **Approve** on the PA. | "Staff update injected"; Vaani tells the caller in Hindi that pre-auth is approved |
| 1:35–1:50 | New sentence: "Naa father ki chest pain vastondi" (my father has chest pain) | Vaani immediately says call 108; `escalate_to_human` **emergency**, a red card in the queue; click **Take over call** |
| 1:50–2:00 | Voice-over over the header metrics | Response p50 in ms, bookings by agent, languages, interruptions handled, hand-offs |

Optional extras if time allows: the duplicate booking with Venkata Rao (98480 12345), or a question about fasting
before a sugar test, which Vaani answers from the Hindi lab-prep sheet.

For the LinkedIn post (10% of the score): post a 30-second cut of the interruption and staff-approval moments,
tag Google Cloud, YourStory and Hack2skill, and use #BuildwithGemini.
