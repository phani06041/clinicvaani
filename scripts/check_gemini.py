"""Sanity check before a demo: is the key valid, does the Live model answer, and does it call our tools?

    python scripts/check_gemini.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from google.genai import types  # noqa: E402

from app import clinic, tools  # noqa: E402
from app.main import API_KEY, MODEL, live_config, make_client  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def main() -> None:
    if not API_KEY:
        sys.exit("GEMINI_API_KEY missing: copy .env.example to .env and paste your key.")
    clinic.init_db()
    client = make_client()
    print(f"Connecting to {MODEL} …")
    t0 = time.perf_counter()
    async with client.aio.live.connect(model=MODEL, config=live_config()) as s:
        print(f"  connected in {(time.perf_counter() - t0) * 1000:.0f} ms")
        await s.send_realtime_input(text="Naaku repu eye doctor appointment kaavali. Evaru unnaru?")
        t1, first_audio, said, calls = time.perf_counter(), None, "", []
        for _ in range(4):
            async for m in s.receive():
                if m.server_content and m.server_content.model_turn:
                    if first_audio is None and any(p.inline_data for p in m.server_content.model_turn.parts or []):
                        first_audio = time.perf_counter() - t1
                if m.server_content and m.server_content.output_transcription:
                    said += m.server_content.output_transcription.text or ""
                if m.tool_call:
                    rs = []
                    for fc in m.tool_call.function_calls:
                        calls.append(fc.name)
                        rs.append(types.FunctionResponse(id=fc.id, name=fc.name,
                                                         response={"result": tools.dispatch(fc.name, dict(fc.args or {}))}))
                    await s.send_tool_response(function_responses=rs)
            if said and calls:
                break
    print(f"  tools called: {calls or 'none'}")
    print(f"  first audio after {first_audio * 1000:.0f} ms" if first_audio else "  no audio received")
    print(f"  agent said: {said.strip()[:300] or '(no transcript)'}")
    print("OK" if calls and said else "Check the output above.")


asyncio.run(main())
