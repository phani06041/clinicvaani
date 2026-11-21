import asyncio
import json

import pytest
from google.genai import types

from app import clinic, knowledge, tools


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(clinic, "DB_PATH", tmp_path / "clinic.db")
    clinic.init_db(reset=True)
    yield


def first_free(doc="D1"):
    r = clinic.conn().execute("SELECT start FROM slots WHERE doctor_id=? AND status='free' AND start>? ORDER BY start LIMIT 1",
                              (doc, clinic.now().isoformat())).fetchone()
    return r["start"]


def test_booking_requires_confirmation_then_books_and_blocks_duplicates():
    slot = first_free("D3")
    args = dict(doctor_id="D3", slot_start=slot, patient_name="Ravi", patient_phone="+91 98765 11111")
    assert clinic.book_appointment(**args)["error"] == "NEEDS_CONFIRMATION"
    ok = clinic.book_appointment(**args, caller_confirmed=True)
    assert ok["ok"] and ok["appointment_id"].startswith("APT-") and ok["phone"] == "9876511111"
    assert clinic.book_appointment(**args, caller_confirmed=True)["error"] == "DUPLICATE_BOOKING"
    other = clinic.book_appointment(doctor_id="D3", slot_start=slot, patient_name="Meena",
                                    patient_phone="9000000001", caller_confirmed=True)
    assert other["error"] == "SLOT_TAKEN"
    assert clinic.cancel_appointment(ok["appointment_id"], caller_confirmed=True)["ok"]


def test_bad_phone_is_rejected():
    assert clinic.lookup_patient("12345")["error"] == "INVALID_PHONE"


def test_waiting_period_and_preauth():
    not_yet = clinic.check_insurance_coverage("cataract operation", patient_phone="9848012345")
    assert not_yet["covered_now"] is False and "2027" in not_yet["reason"] or not_yet["months_completed"] < 24
    assert clinic.create_preauth_request("9848012345", "cataract")["error"] == "NOT_ELIGIBLE"
    code = clinic.send_verification_code("9876543210", "CALL-X")["_deliver_sms"]["text"].split("code is ")[1][:4]
    clinic.verify_caller("9876543210", code, "CALL-X")
    pa = clinic.create_preauth_request("9876543210", "cataract surgery", "D2", 38000, call_id="CALL-X")
    assert pa["ok"] and pa["status"] == "pending_staff_review"
    res = clinic.decide_preauth(pa["preauth_id"], "approved", "ok", "Swathi")
    assert not res["conflict"] and res["row"]["status"] == "approved" and res["row"]["staff_name"] == "Swathi"
    again = clinic.decide_preauth(pa["preauth_id"], "rejected", "", "Ravi")
    assert again["conflict"] and again["row"]["staff_name"] == "Swathi"


def test_non_network_insurer_and_exclusions():
    r = clinic.check_insurance_coverage("consultation", patient_phone="9000011111")
    assert r["cashless_at_this_clinic"] is False and r["covered_now"] is True
    assert clinic.check_insurance_coverage("physio", insurer="suraksha", plan="gold")["covered"] is False


def test_cross_language_retrieval_finds_hindi_doc_from_english_query():
    hits = knowledge.search_clinic_knowledge("can I drink tea before fasting sugar test")["results"]
    assert "लैब" in hits[0]["source"]


def test_dispatch_ignores_unknown_args_and_reports_bad_ones():
    assert tools.dispatch("find_doctors", {"need": "knee pain", "bogus": 1})["doctors"][0]["doctor_id"] == "D4"
    assert tools.dispatch("check_slots", {})["error"] == "BAD_ARGUMENTS"
    assert tools.dispatch("nope", {})["error"] == "UNKNOWN_TOOL"


def test_declarations_are_valid_sdk_types():
    from app.main import live_config
    cfg = live_config()
    names = {f.name for f in cfg.tools[0].function_declarations}
    assert names == set(tools._FUNCS)


# ---------------------------------------------------------------- end-to-end with a fake Gemini session

class FakeSession:
    def __init__(self):
        self.sent = []
        self.tool_responses = []
        self.script = [
            [types.LiveServerMessage(server_content=types.LiveServerContent(
                model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=b"\x00\x01" * 240, mime_type="audio/pcm;rate=24000"))]),
                output_transcription=types.Transcription(text="Namaskaram, Arogya clinic nunchi Vaani."),
                turn_complete=True))],
            [types.LiveServerMessage(server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="నాకు repu eye doctor kaavali"))),
             types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
                 types.FunctionCall(id="fc1", name="find_doctors", args={"need": "eye", "language": "Telugu"})])),
             types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True)),
             types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))],
        ]

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        if self.script:
            for m in self.script.pop(0):
                yield m
        else:
            await asyncio.sleep(0.01)


class FakeClient:
    last = None

    def __init__(self, **_):
        self.aio = self
        self.live = self

    def connect(self, model, config):
        sess = FakeSession()
        FakeClient.last = sess

        class Ctx:
            async def __aenter__(self_inner):
                return sess

            async def __aexit__(self_inner, *a):
                return False
        return Ctx()


def test_call_websocket_bridge(monkeypatch):
    from fastapi.testclient import TestClient
    import google.genai as genai
    from app import main

    monkeypatch.setattr(main, "API_KEY", "test")
    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(main, "USE_VERTEX", False)
    with TestClient(main.app) as tc:
        with tc.websocket_connect("/ws/call") as ws:
            events, audio = [], 0
            for _ in range(12):
                msg = ws.receive()
                if msg.get("bytes"):
                    audio += 1
                    continue
                ev = json.loads(msg["text"])
                events.append(ev)
                if ev["type"] == "interrupted":
                    break
            ws.send_bytes(b"\x00" * 3200)
            ws.send_text(json.dumps({"type": "latency", "ms": 640}))
            ws.send_text(json.dumps({"type": "hangup"}))
        kinds = [e["type"] for e in events]
        assert audio == 1
        assert "call_started" in kinds and "tool_result" in kinds and "interrupted" in kinds
        tr = next(e for e in events if e["type"] == "tool_result")
        assert tr["result"]["doctors"][0]["doctor_id"] == "D2"
        sess = FakeClient.last
        assert sess.tool_responses[0].id == "fc1"
        assert "repu eye doctor kaavali" in sess.tool_responses[0].response["reply_language"]
        assert sess.sent[0]["text"].startswith("[SYSTEM]")
        assert tc.get("/api/state").status_code == 401
        assert tc.post("/api/staff/login", json={"name": "Swathi", "pin": main.STAFF_PIN}).status_code == 200
        state = tc.get("/api/state").json()
        assert state["metrics"]["calls"] == 1 and state["metrics"]["languages"] == ["Telugu+English"]
        assert state["metrics"]["interruptions_handled"] == 1


def test_falls_back_when_model_missing(monkeypatch):
    from fastapi.testclient import TestClient
    import google.genai as genai
    from app import main

    class MissingFirst(FakeClient):
        def connect(self, model, config):
            if model == "gemini-3.8-live":
                class Bad:
                    async def __aenter__(self):
                        raise Exception("InvalidStatus: server rejected WebSocket connection: HTTP 404")

                    async def __aexit__(self, *a):
                        return False
                return Bad()
            return super().connect(model, config)

    monkeypatch.setattr(main, "API_KEY", "test")
    monkeypatch.setattr(main, "USE_VERTEX", False)
    monkeypatch.setattr(main, "_working_model", None)
    monkeypatch.setattr(genai, "Client", MissingFirst)
    with TestClient(main.app) as tc:
        with tc.websocket_connect("/ws/call") as ws:
            while True:
                m = ws.receive()
                if m.get("text") and json.loads(m["text"])["type"] == "call_started":
                    assert json.loads(m["text"])["model"] == "gemini-3.1-flash-live-preview"
                    break
            ws.send_text(json.dumps({"type": "hangup"}))


def test_caller_verification_gates_private_data():
    call = "CALL-V"
    pre = clinic.lookup_patient("9876543210", call)
    assert pre["found"] and pre["verified"] is False and "name" not in pre
    assert clinic.check_insurance_coverage("cataract", patient_phone="9876543210", call_id=call)["error"] == "VERIFICATION_REQUIRED"
    # a named insurer/plan question is public
    assert clinic.check_insurance_coverage("cataract", patient_phone="9876543210", insurer="Suraksha", plan="Gold", call_id=call)["ok"]
    assert clinic.create_preauth_request("9876543210", "cataract", call_id=call)["error"] == "VERIFICATION_REQUIRED"
    assert clinic.send_verification_code("9999988888", call)["error"] == "NOT_REGISTERED"
    sent = clinic.send_verification_code("9876543210", call)
    code = sent["_deliver_sms"]["text"].split("code is ")[1][:4]
    # code never appears anywhere except the private delivery payload
    assert code not in json.dumps({k: v for k, v in sent.items() if k != "_deliver_sms"})
    outbox = clinic.dashboard_state()["outbox"][0]["text"]
    assert code not in outbox
    wrong = "0000" if code != "0000" else "1111"
    assert clinic.verify_caller("9876543210", wrong, call)["error"] == "WRONG_CODE"
    ok = clinic.verify_caller("9876543210", " ".join(code), call)  # spoken digits with spaces
    assert ok["verified"] and ok["patient"]["name"] == "Sunita Sharma"
    assert clinic.create_preauth_request("9876543210", "cataract", call_id=call)["ok"]
    # verification does not carry over to another call
    assert clinic.lookup_patient("9876543210", "CALL-OTHER")["verified"] is False


def test_too_many_wrong_codes():
    call = "CALL-W"
    code = clinic.send_verification_code("9848012345", call)["_deliver_sms"]["text"].split("code is ")[1][:4]
    bad = "0000" if code != "0000" else "1111"
    for _ in range(3):
        clinic.verify_caller("9848012345", bad, call)
    assert clinic.verify_caller("9848012345", code, call)["error"] == "TOO_MANY_ATTEMPTS"


def test_cancel_own_booking_without_code_but_not_others():
    slot = first_free("D4")
    b = clinic.book_appointment("D4", slot, "Venkata Rao", "9848012345", caller_confirmed=True, call_id="CALL-A")
    assert clinic.cancel_appointment(b["appointment_id"], True, call_id="CALL-B")["error"] == "VERIFICATION_REQUIRED"
    assert clinic.cancel_appointment(b["appointment_id"], True, call_id="CALL-A")["ok"]


def test_staff_auth_and_decide_once_over_http(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    with TestClient(main.app) as tc:
        assert tc.get("/api/state").status_code == 401
        assert tc.post("/api/reset").status_code == 401
        assert tc.post("/api/staff/login", json={"name": "X", "pin": "nope"}).status_code == 401
        pa = clinic.create_preauth_request("9876543210", "cataract surgery")["preauth_id"]
        a = TestClient(main.app); b = TestClient(main.app)
        a.post("/api/staff/login", json={"name": "Swathi", "pin": main.STAFF_PIN})
        b.post("/api/staff/login", json={"name": "Ravi", "pin": main.STAFF_PIN})
        assert b.get("/api/staff/me").json()["name"] == "Ravi"
        r1 = a.post(f"/api/preauths/{pa}/decision", json={"decision": "approved"})
        r2 = b.post(f"/api/preauths/{pa}/decision", json={"decision": "rejected"})
        assert r1.status_code == 200 and r1.json()["preauth"]["staff_name"] == "Swathi"
        assert r2.status_code == 409 and "Swathi" in r2.json()["detail"]
        forged = TestClient(main.app); forged.cookies.set(main.COOKIE, "eA.9999999999.deadbeef")
        assert forged.get("/api/state").status_code == 401


def test_verification_code_reaches_only_the_caller_screen():
    from app import main

    class WS:
        def __init__(self): self.sent = []
        async def send_text(self, t): self.sent.append(json.loads(t))

    sess = FakeSession()
    call = main.Call(WS()); call.session = sess
    seen_by_staff = []
    q = main.hub.subscribe()
    tc = types.LiveServerToolCall(function_calls=[types.FunctionCall(id="v1", name="send_verification_code", args={"phone": "9876543210"})])
    asyncio.run(main.handle_tool_call(call, tc))
    while not q.empty():
        seen_by_staff.append(q.get_nowait())
    main.hub.unsubscribe(q)
    sms = [e for e in call.ws.sent if e["type"] == "sms"]
    code = sms[0]["text"].split("code is ")[1][:4]
    to_model = sess.tool_responses[0].response["result"]
    assert "_deliver_sms" not in to_model and code not in json.dumps(to_model)
    assert code not in json.dumps(seen_by_staff)


def test_language_choice_shapes_opening():
    from app import prompts
    hi = prompts.system_instruction("hindi")
    assert "caller chose Hindi" in hi and prompts.LANGUAGES["Hindi"] in hi
    ask = prompts.system_instruction("klingon")  # unknown -> ask
    assert "did not choose a language" in ask and "Which language" in ask
