"""Diagnose Gemini Live access for the key in .env.

    python scripts/list_live_models.py

1. Checks the key works at all (one normal text request).
2. Lists models your key can see that support the Live API (bidiGenerateContent).
3. Tries a Live connection to each and prints Google's actual error body.
Put the first model marked OK into GEMINI_LIVE_MODEL in .env.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import API_KEY, CANDIDATE_MODELS, MODEL, make_client  # noqa: E402

if not API_KEY:
    sys.exit("GEMINI_API_KEY missing in .env")

import google.genai  # noqa: E402

print(f"google-genai {google.genai.__version__}")
client = make_client()

# 1. does the key work for a plain request?
print("\n[1] Plain text request")
listed = []
try:
    for m in client.models.list(config={"page_size": 200}):
        listed.append(m)
    text_model = next((m.name.removeprefix("models/") for m in listed
                       if "generateContent" in (m.supported_actions or []) and "3.8-flash" in m.name), None)
    if text_model:
        r = client.models.generate_content(model=text_model, contents="Say OK")
        print(f"  {text_model}: {(r.text or '').strip()[:40]!r}  -> key works")
except Exception as e:  # noqa: BLE001
    print(f"  FAILED: {type(e).__name__}: {e}")

# 2. which models advertise Live support?
live = [m.name.removeprefix("models/") for m in listed if "bidiGenerateContent" in (m.supported_actions or [])]
print(f"\n[2] Key sees {len(listed)} models; Live-capable: {live or 'NONE'}")


# 3. try connecting
async def probe(name: str) -> str:
    try:
        async with client.aio.live.connect(model=name, config={"response_modalities": ["AUDIO"]}):
            return "OK"
    except Exception as e:  # noqa: BLE001
        body = ""
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "body", None):
            body = resp.body.decode(errors="replace")[:300]
        return f"{type(e).__name__}: {e} {body}".strip()


async def main():
    print("\n[3] Live connection test")
    for name in dict.fromkeys(live + CANDIDATE_MODELS):
        res = await probe(name)
        mark = "  <- configured" if name == MODEL else ""
        print(f"  {name:50s} {res}{mark}")


asyncio.run(main())
