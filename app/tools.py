"""Function declarations exposed to Gemini Live, and the dispatcher that runs them."""
from __future__ import annotations

from . import clinic
from .knowledge import search_clinic_knowledge

S, B, I, O = "STRING", "BOOLEAN", "INTEGER", "OBJECT"


def _fn(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": O, "properties": props, "required": required or []}}


DECLARATIONS = [
    _fn("find_doctors",
        "Find clinic doctors for a need. Pass the need in ENGLISH (e.g. 'eye', 'child fever', 'knee pain', 'pregnancy', 'general').",
        {"need": {"type": S, "description": "Specialty or problem in English"},
         "language": {"type": S, "description": "Caller's language, e.g. Telugu, Hindi, Tamil, English"}},
        ["need"]),
    _fn("check_slots",
        "Get free appointment slots for a doctor on a date. Always call before offering times.",
        {"doctor_id": {"type": S, "description": "Doctor id like D1 from find_doctors"},
         "date": {"type": S, "description": "YYYY-MM-DD, or 'today' / 'tomorrow'"},
         "part_of_day": {"type": S, "enum": ["morning", "afternoon", "evening", "any"]},
         "limit": {"type": I}},
        ["doctor_id", "date"]),
    _fn("lookup_patient",
        "Look up a patient by 10-digit mobile number: name, insurance policy, upcoming appointments.",
        {"phone": {"type": S, "description": "Digits only"}}, ["phone"]),
    _fn("send_verification_code",
        "Send a 4-digit code by SMS to an EXISTING patient's registered mobile, to verify the caller before sharing or changing their records (name, insurance on file, appointments, pre-auth, cancellation).",
        {"phone": {"type": S}}, ["phone"]),
    _fn("verify_caller",
        "Check the 4-digit code the caller reads out from the SMS. On success returns the patient's record.",
        {"phone": {"type": S}, "code": {"type": S, "description": "Digits as spoken by the caller"}}, ["phone", "code"]),
    _fn("book_appointment",
        "Book a slot. Only call with caller_confirmed=true AFTER reading back doctor, day, time, name and phone and hearing an explicit yes.",
        {"doctor_id": {"type": S}, "slot_start": {"type": S, "description": "Exact slot_start from check_slots"},
         "patient_name": {"type": S}, "patient_phone": {"type": S},
         "reason": {"type": S, "description": "Short reason in English"},
         "language": {"type": S, "description": "Caller's language"},
         "caller_confirmed": {"type": B}},
        ["doctor_id", "slot_start", "patient_name", "patient_phone", "caller_confirmed"]),
    _fn("cancel_appointment",
        "Cancel an appointment (also used to undo a booking or before re-booking a different time). Needs caller_confirmed=true.",
        {"appointment_id": {"type": S}, "caller_confirmed": {"type": B}}, ["appointment_id", "caller_confirmed"]),
    _fn("check_insurance_coverage",
        "Check whether a procedure is covered under the caller's insurance, including waiting periods, limits, pre-authorization and cashless status. Prefer passing patient_phone so the policy on file is used.",
        {"procedure": {"type": S, "description": "In English, e.g. 'cataract surgery', 'consultation', 'blood test', 'knee replacement', 'delivery'"},
         "patient_phone": {"type": S}, "insurer": {"type": S}, "plan": {"type": S}},
        ["procedure"]),
    _fn("search_clinic_knowledge",
        "Search clinic policies, timings, fees, lab test preparation and insurance policy wordings. Query MUST be in English even if the caller speaks another language.",
        {"query_en": {"type": S}, "top_k": {"type": I}}, ["query_en"]),
    _fn("create_preauth_request",
        "Raise a cashless pre-authorization request for a covered planned procedure. Goes to front-desk staff for approval.",
        {"patient_phone": {"type": S}, "procedure": {"type": S}, "doctor_id": {"type": S},
         "estimated_cost_inr": {"type": I}},
        ["patient_phone", "procedure"]),
    _fn("escalate_to_human",
        "Hand the caller to clinic staff. Use for emergencies, medical questions, complaints, billing disputes, repeated misunderstanding, or when the caller asks for a person.",
        {"reason": {"type": S}, "urgency": {"type": S, "enum": ["routine", "urgent", "emergency"]},
         "summary_en": {"type": S, "description": "2-3 line English summary for staff"},
         "caller_language": {"type": S}, "caller_phone": {"type": S}},
        ["reason", "urgency", "summary_en"]),
    _fn("send_sms_confirmation",
        "Send the booking confirmation SMS. Write the message in the caller's language (Telugu/Hindi script is fine), max 300 characters, include appointment id, doctor, day and time.",
        {"appointment_id": {"type": S}, "message": {"type": S}, "language": {"type": S}},
        ["appointment_id", "message"]),
]

_FUNCS = {
    "find_doctors": clinic.find_doctors,
    "check_slots": clinic.check_slots,
    "lookup_patient": clinic.lookup_patient,
    "send_verification_code": clinic.send_verification_code,
    "verify_caller": clinic.verify_caller,
    "book_appointment": clinic.book_appointment,
    "cancel_appointment": clinic.cancel_appointment,
    "check_insurance_coverage": clinic.check_insurance_coverage,
    "search_clinic_knowledge": search_clinic_knowledge,
    "create_preauth_request": clinic.create_preauth_request,
    "escalate_to_human": clinic.escalate_to_human,
    "send_sms_confirmation": clinic.send_sms_confirmation,
}
_TAKES_CALL_ID = {"book_appointment", "create_preauth_request", "escalate_to_human", "send_sms_confirmation",
                  "lookup_patient", "check_insurance_coverage", "cancel_appointment",
                  "send_verification_code", "verify_caller"}
STATE_CHANGING = {"book_appointment", "cancel_appointment", "create_preauth_request", "escalate_to_human",
                  "send_sms_confirmation", "send_verification_code"}


def dispatch(name: str, args: dict, call_id: str | None = None) -> dict:
    fn = _FUNCS.get(name)
    if fn is None:
        return {"ok": False, "error": "UNKNOWN_TOOL", "message": f"No tool named {name}"}
    allowed = set(fn.__code__.co_varnames[: fn.__code__.co_argcount])
    clean = {k: v for k, v in (args or {}).items() if k in allowed}
    if name in _TAKES_CALL_ID:
        clean["call_id"] = call_id
    try:
        return fn(**clean)
    except TypeError as e:  # missing / wrong argument from the model
        return {"ok": False, "error": "BAD_ARGUMENTS", "message": str(e)}
