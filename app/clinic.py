"""Clinic back-office: SQLite store + the business actions the voice agent can take.

All data is fictional demo data, seeded relative to "today" so the demo always has
free and booked slots in the coming week.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "clinic.db"
COVERAGE = json.loads((DATA / "coverage.json").read_text(encoding="utf-8"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

# id, name, specialty, aliases, languages, fee, sessions, weekdays (Mon=0)
DOCTORS = [
    ("D1", "Dr. K. Lakshmi Prasanna", "General Physician",
     ["general", "physician", "fever", "cold", "cough", "bp", "sugar", "diabetes", "body pain", "family doctor", "checkup"],
     ["Telugu", "English", "Hindi"], 400, ["09:00-13:00", "16:00-20:00"], [0, 1, 2, 3, 4, 5]),
    ("D2", "Dr. Ramesh Varma", "Ophthalmology (Eye)",
     ["eye", "eyes", "ophthalmology", "cataract", "vision", "spectacles", "kanti"],
     ["Telugu", "English"], 600, ["10:00-13:00"], [0, 2, 4, 5]),
    ("D3", "Dr. Farah Siddiqui", "Paediatrics (Children)",
     ["child", "children", "kids", "baby", "paediatrics", "pediatrics", "vaccination", "infant"],
     ["Hindi", "Urdu", "English", "Telugu"], 500, ["09:30-12:30", "17:00-19:00"], [0, 1, 2, 3, 4, 5]),
    ("D4", "Dr. S. Venkatesh", "Orthopaedics (Bones & Joints)",
     ["bone", "bones", "joint", "knee", "back pain", "orthopaedics", "orthopedics", "fracture", "shoulder"],
     ["Telugu", "Tamil", "English"], 700, ["16:00-20:00"], [0, 1, 3, 4, 5]),
    ("D5", "Dr. Anitha Reddy", "Obstetrics & Gynaecology",
     ["pregnancy", "gynaecology", "gynecology", "women", "periods", "maternity", "obstetrics", "delivery"],
     ["Telugu", "English", "Hindi"], 600, ["09:00-12:00"], [1, 2, 3, 5]),
]
SLOT_MINUTES = 15


def now() -> dt.datetime:
    return dt.datetime.now(IST)


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


def _shift_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    day = min(d.day, 28)
    return dt.date(y, m, day)


# --------------------------------------------------------------------------- DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors(id TEXT PRIMARY KEY, name TEXT, specialty TEXT, aliases TEXT,
  languages TEXT, fee INTEGER, sessions TEXT, weekdays TEXT);
CREATE TABLE IF NOT EXISTS slots(doctor_id TEXT, start TEXT, status TEXT, appointment_id TEXT,
  PRIMARY KEY(doctor_id, start));
CREATE TABLE IF NOT EXISTS patients(phone TEXT PRIMARY KEY, name TEXT, language TEXT, insurer TEXT,
  plan TEXT, policy_no TEXT, policy_start TEXT);
CREATE TABLE IF NOT EXISTS appointments(id TEXT PRIMARY KEY, doctor_id TEXT, slot_start TEXT,
  patient_phone TEXT, patient_name TEXT, reason TEXT, language TEXT, status TEXT, created_at TEXT,
  source TEXT, call_id TEXT);
CREATE TABLE IF NOT EXISTS handoffs(id TEXT PRIMARY KEY, created_at TEXT, call_id TEXT, reason TEXT,
  urgency TEXT, summary TEXT, language TEXT, caller_phone TEXT, status TEXT, staff_name TEXT, staff_note TEXT);
CREATE TABLE IF NOT EXISTS preauths(id TEXT PRIMARY KEY, created_at TEXT, call_id TEXT, patient_phone TEXT,
  patient_name TEXT, insurer TEXT, plan TEXT, procedure TEXT, doctor_id TEXT, estimated_cost INTEGER,
  clause TEXT, status TEXT, staff_note TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, to_phone TEXT,
  language TEXT, text TEXT, appointment_id TEXT, call_id TEXT);
CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, duration_s REAL,
  languages TEXT, turns INTEGER, tool_calls INTEGER, interruptions INTEGER, latencies TEXT, outcomes TEXT);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS verifications(id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, phone TEXT, code TEXT,
  created_at TEXT, expires_at TEXT, attempts INTEGER DEFAULT 0, verified INTEGER DEFAULT 0);
"""


def conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    return _conn  # type: ignore[return-value]


def init_db(reset: bool = False, path: Path | None = None) -> None:
    """Open (and seed if new) the clinic database. reset=True wipes it for a clean demo."""
    global _conn
    with _lock:
        p = path or DB_PATH
        if _conn is not None:
            _conn.close()
            _conn = None
        if reset and p.exists():
            p.unlink()
        fresh = not p.exists()
        c = sqlite3.connect(p, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        for ddl in ("ALTER TABLE preauths ADD COLUMN staff_name TEXT",
                    "ALTER TABLE preauths ADD COLUMN decided_at TEXT",
                    "ALTER TABLE handoffs ADD COLUMN updated_at TEXT"):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn = c
        if fresh:
            _seed(c)


def _next_id(prefix: str, start: int) -> str:
    c = conn()
    row = c.execute("SELECT value FROM counters WHERE name=?", (prefix,)).fetchone()
    val = (row["value"] if row else start) + 1
    c.execute("INSERT OR REPLACE INTO counters(name, value) VALUES(?,?)", (prefix, val))
    return f"{prefix}-{val}"


def _seed(c: sqlite3.Connection) -> None:
    rnd = random.Random(42)
    today = now().date()
    for d in DOCTORS:
        c.execute("INSERT INTO doctors VALUES(?,?,?,?,?,?,?,?)",
                  (d[0], d[1], d[2], json.dumps(d[3]), json.dumps(d[4]), d[5], json.dumps(d[6]), json.dumps(d[7])))
    patients = [
        ("9848012345", "Venkata Rao", "Telugu", "Suraksha Health", "Gold", "SUR-G-771204", _shift_months(today, -14)),
        ("9876543210", "Sunita Sharma", "Hindi", "Suraksha Health", "Gold", "SUR-G-559310", _shift_months(today, -30)),
        ("9000011111", "Lakshmi Devi", "Telugu", "Navjeevan General", "Family Floater", "NAV-FF-20931", _shift_months(today, -40)),
        ("9123456780", "Arjun Naidu", "English", "Suraksha Health", "Silver", "SUR-S-118842", _shift_months(today, -26)),
    ]
    for p in patients:
        c.execute("INSERT INTO patients VALUES(?,?,?,?,?,?,?)", (*p[:6], p[6].isoformat()))
    for d in DOCTORS:
        for day_off in range(0, 8):
            day = today + dt.timedelta(days=day_off)
            if day.weekday() not in d[7]:
                continue
            for sess in d[6]:
                a, b = sess.split("-")
                t = dt.datetime.combine(day, dt.time.fromisoformat(a), IST)
                end = dt.datetime.combine(day, dt.time.fromisoformat(b), IST)
                while t < end:
                    status = "booked" if rnd.random() < 0.35 else "free"
                    c.execute("INSERT INTO slots VALUES(?,?,?,?)", (d[0], t.isoformat(), status, None))
                    t += dt.timedelta(minutes=SLOT_MINUTES)
    c.execute("INSERT INTO counters VALUES('APT', 1040)")
    c.execute("INSERT INTO counters VALUES('HO', 200)")
    c.execute("INSERT INTO counters VALUES('PA', 500)")
    # One existing booking so duplicate detection can be demoed with Venkata Rao.
    first_free = c.execute("SELECT start FROM slots WHERE doctor_id='D1' AND status='free' AND start>? ORDER BY start LIMIT 1",
                           (now().isoformat(),)).fetchone()
    if first_free:
        c.execute("UPDATE counters SET value=value+1 WHERE name='APT'")
        apt = f"APT-{c.execute('SELECT value FROM counters WHERE name=\"APT\"').fetchone()[0]}"
        c.execute("UPDATE slots SET status='booked', appointment_id=? WHERE doctor_id='D1' AND start=?", (apt, first_free["start"]))
        c.execute("INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (apt, "D1", first_free["start"], "9848012345", "Venkata Rao", "BP follow-up", "Telugu",
                   "confirmed", now().isoformat(), "counter", None))
    c.commit()


# ---------------------------------------------------------------------- helpers

def _err(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_date(text: str | None) -> dt.date | None:
    if not text:
        return now().date()
    s = str(text).strip().lower()
    today = now().date()
    if s in ("today", "aaj", "ivala", "ivvala", "eeroju", "ee roju"):
        return today
    if s in ("tomorrow", "kal", "repu"):
        return today + dt.timedelta(days=1)
    if s in ("day after tomorrow", "parso", "ellundi"):
        return today + dt.timedelta(days=2)
    for i, w in enumerate(_WEEKDAYS):
        if w in s:
            delta = (i - today.weekday()) % 7
            if "next" in s and delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _doctor(doctor_id: str) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM doctors WHERE id=?", (str(doctor_id).upper(),)).fetchone()


def _doctor_public(r: sqlite3.Row) -> dict:
    return {"doctor_id": r["id"], "name": r["name"], "specialty": r["specialty"],
            "languages": json.loads(r["languages"]), "fee_inr": r["fee"],
            "sessions": json.loads(r["sessions"]),
            "days": [_WEEKDAYS[i].title() for i in json.loads(r["weekdays"])]}


def _fmt_slot(iso: str) -> dict:
    t = dt.datetime.fromisoformat(iso)
    return {"slot_start": iso, "date": t.date().isoformat(), "day": t.strftime("%A"),
            "time": t.strftime("%I:%M %p").lstrip("0")}


# ------------------------------------------------------------------------ tools

def find_doctors(need: str = "", language: str | None = None) -> dict:
    need_l = (need or "").lower()
    rows = conn().execute("SELECT * FROM doctors").fetchall()
    matches = []
    for r in rows:
        aliases = json.loads(r["aliases"]) + [r["specialty"].lower()]
        score = sum(1 for a in aliases if a in need_l or (need_l and need_l in a))
        if score or not need_l:
            matches.append((score, r))
    if not matches:
        matches = [(0, r) for r in rows if r["id"] == "D1"]
        note = "No specialist matched; the general physician is the right first visit."
    else:
        note = None
    matches.sort(key=lambda x: -x[0])
    out = [_doctor_public(r) for _, r in matches]
    if language:
        for d in out:
            d["speaks_caller_language"] = any(language.lower() in l.lower() for l in d["languages"])
    return {"ok": True, "doctors": out, **({"note": note} if note else {})}


def check_slots(doctor_id: str, date: str | None = None, part_of_day: str = "any", limit: int = 6) -> dict:
    doc = _doctor(doctor_id)
    if not doc:
        return _err("UNKNOWN_DOCTOR", f"No doctor with id {doctor_id}. Call find_doctors first.")
    day = parse_date(date)
    if day is None:
        return _err("BAD_DATE", f"Could not understand date '{date}'. Use YYYY-MM-DD, today or tomorrow.")
    if day < now().date():
        return _err("PAST_DATE", "That date is in the past.")
    bounds = {"morning": (0, 12), "afternoon": (12, 16), "evening": (16, 24)}.get((part_of_day or "any").lower(), (0, 24))
    rows = conn().execute(
        "SELECT start FROM slots WHERE doctor_id=? AND status='free' AND substr(start,1,10)=? AND start>? ORDER BY start",
        (doc["id"], day.isoformat(), now().isoformat())).fetchall()
    free = [r["start"] for r in rows if bounds[0] <= dt.datetime.fromisoformat(r["start"]).hour < bounds[1]]
    result = {"ok": True, "doctor": doc["name"], "date": day.isoformat(), "day": day.strftime("%A"),
              "free_slots": [_fmt_slot(s) for s in free[:max(1, min(int(limit or 6), 12))]],
              "total_free_that_day": len(free)}
    if not free:
        nxt = conn().execute(
            "SELECT start FROM slots WHERE doctor_id=? AND status='free' AND start>? ORDER BY start LIMIT 3",
            (doc["id"], max(now(), dt.datetime.combine(day, dt.time(0), IST)).isoformat())).fetchall()
        result["note"] = (f"{doc['name']} has no free {part_of_day if part_of_day != 'any' else ''} slots on "
                          f"{day.strftime('%A')}. Works on: {', '.join(_doctor_public(doc)['days'])}.")
        result["next_available"] = [_fmt_slot(r["start"]) for r in nxt]
    return result


VERIFY_TTL_MIN = 10
MAX_CODES_PER_CALL = 3
MAX_ATTEMPTS = 3


def _mask(phone: str) -> str:
    return phone[:2] + "XXXXXX" + phone[-2:]


def is_verified(call_id: str | None, phone: str | None) -> bool:
    """Phone calls must prove they own a number before hearing that patient's details.
    call_id=None means a trusted back-office context (staff tools, scripts, tests)."""
    if call_id is None:
        return True
    p = normalize_phone(phone)
    if not p:
        return False
    return conn().execute("SELECT 1 FROM verifications WHERE call_id=? AND phone=? AND verified=1",
                          (call_id, p)).fetchone() is not None


def _needs_verification(phone: str) -> dict:
    return _err("VERIFICATION_REQUIRED",
                f"Before sharing or changing this patient's records, verify the caller: call send_verification_code "
                f"for {_mask(phone)}, ask the caller to read out the 4-digit code from the SMS, then call verify_caller.")


def send_verification_code(phone: str, call_id: str | None = None) -> dict:
    p = normalize_phone(phone)
    if not p:
        return _err("INVALID_PHONE", "Phone number must be a 10-digit Indian mobile number. Ask the caller to repeat it.")
    if not conn().execute("SELECT 1 FROM patients WHERE phone=?", (p,)).fetchone():
        return _err("NOT_REGISTERED", "No patient is registered with this number, so there is nothing to verify. "
                                      "New patients can book without verification.")
    with _lock:
        sent = conn().execute("SELECT count(*) FROM verifications WHERE call_id=?", (call_id,)).fetchone()[0]
        if call_id is not None and sent >= MAX_CODES_PER_CALL:
            return _err("TOO_MANY_CODES", "Too many codes sent on this call. Escalate to staff (routine).")
        code = f"{secrets.randbelow(10000):04d}"
        t = now()
        conn().execute("INSERT INTO verifications(call_id, phone, code, created_at, expires_at) VALUES(?,?,?,?,?)",
                       (call_id, p, code, t.isoformat(), (t + dt.timedelta(minutes=VERIFY_TTL_MIN)).isoformat()))
        conn().execute("INSERT INTO outbox(created_at,to_phone,language,text,appointment_id,call_id) VALUES(?,?,?,?,?,?)",
                       (t.isoformat(), p, "", "Verification code (hidden from staff)", None, call_id))
        conn().commit()
    # The code itself goes only to the patient's phone (main.py strips "_deliver_sms" before Gemini or staff see it).
    return {"ok": True, "sent_to": _mask(p), "channel": "SMS (mock)",
            "next_step": "Ask the caller to read the 4-digit code from the SMS, then call verify_caller. "
                         "Never say or guess the code yourself.",
            "_deliver_sms": {"to": p, "text": f"Arogya Family Clinic: your verification code is {code}. "
                                              f"Valid {VERIFY_TTL_MIN} minutes. Share it only on this call."}}


def verify_caller(phone: str, code: str, call_id: str | None = None) -> dict:
    p = normalize_phone(phone)
    if not p:
        return _err("INVALID_PHONE", "Phone number must be a 10-digit Indian mobile number.")
    with _lock:
        row = conn().execute("SELECT * FROM verifications WHERE call_id IS ? AND phone=? ORDER BY id DESC LIMIT 1",
                             (call_id, p)).fetchone()
        if not row:
            return _err("NO_CODE_SENT", "No code was sent to this number on this call. Call send_verification_code first.")
        if row["verified"]:
            return {"ok": True, "verified": True, "patient": lookup_patient(p, call_id)}
        if row["attempts"] >= MAX_ATTEMPTS:
            return _err("TOO_MANY_ATTEMPTS", "Too many wrong codes. Do not share details; escalate to staff (routine).")
        if dt.datetime.fromisoformat(row["expires_at"]) < now():
            return _err("CODE_EXPIRED", "The code has expired. Offer to send a new one.")
        given = re.sub(r"\D", "", str(code or ""))
        if not secrets.compare_digest(given, row["code"]):
            conn().execute("UPDATE verifications SET attempts=attempts+1 WHERE id=?", (row["id"],))
            conn().commit()
            left = MAX_ATTEMPTS - row["attempts"] - 1
            return _err("WRONG_CODE", f"That code does not match. {left} attempt(s) left. Ask the caller to read it again slowly.")
        conn().execute("UPDATE verifications SET verified=1 WHERE id=?", (row["id"],))
        conn().commit()
    return {"ok": True, "verified": True, "patient": lookup_patient(p, call_id)}


def lookup_patient(phone: str, call_id: str | None = None) -> dict:
    p = normalize_phone(phone)
    if not p:
        return _err("INVALID_PHONE", "Phone number must be a 10-digit Indian mobile number. Ask the caller to repeat it slowly.")
    r = conn().execute("SELECT * FROM patients WHERE phone=?", (p,)).fetchone()
    if r and not is_verified(call_id, p):
        return {"ok": True, "found": True, "verified": False, "phone": p,
                "note": "Existing patient. Their name, insurance and appointments are private: verify the caller "
                        "(send_verification_code, then verify_caller) before using or sharing them. "
                        "A NEW booking can go ahead without verification if the caller gives the name."}
    upcoming = conn().execute(
        "SELECT a.id, a.slot_start, d.name doctor FROM appointments a JOIN doctors d ON d.id=a.doctor_id "
        "WHERE a.patient_phone=? AND a.status='confirmed' AND a.slot_start>? ORDER BY a.slot_start",
        (p, now().isoformat())).fetchall()
    ups = [{"appointment_id": u["id"], "doctor": u["doctor"], **_fmt_slot(u["slot_start"])} for u in upcoming]
    if not r:
        return {"ok": True, "found": False, "phone": p, "upcoming_appointments": ups,
                "note": "New patient. Ask for their name to register during booking."}
    return {"ok": True, "found": True, "phone": p, "name": r["name"], "preferred_language": r["language"],
            "insurance": {"insurer": r["insurer"], "plan": r["plan"], "policy_no": r["policy_no"],
                          "policy_start": r["policy_start"]},
            "upcoming_appointments": ups}


def book_appointment(doctor_id: str, slot_start: str, patient_name: str, patient_phone: str,
                     reason: str = "", language: str = "", caller_confirmed: bool = False,
                     call_id: str | None = None) -> dict:
    doc = _doctor(doctor_id)
    if not doc:
        return _err("UNKNOWN_DOCTOR", f"No doctor with id {doctor_id}.")
    phone = normalize_phone(patient_phone)
    if not phone:
        return _err("INVALID_PHONE", "Phone number must be a 10-digit Indian mobile number. Ask the caller to repeat it.")
    if not (patient_name or "").strip():
        return _err("MISSING_NAME", "Ask the caller for the patient's name.")
    if not caller_confirmed:
        return _err("NEEDS_CONFIRMATION",
                    "Read back doctor, day, time, patient name and phone number to the caller and get an explicit yes, "
                    "then call again with caller_confirmed=true.")
    try:
        start = dt.datetime.fromisoformat(slot_start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return _err("BAD_SLOT", "slot_start must be a slot_start value returned by check_slots.")
    iso = start.astimezone(IST).isoformat()
    with _lock:
        c = conn()
        if start <= now():
            return _err("PAST_SLOT", "That time has already passed.")
        dup = c.execute(
            "SELECT id, slot_start FROM appointments WHERE patient_phone=? AND doctor_id=? AND status='confirmed' "
            "AND substr(slot_start,1,10)=?", (phone, doc["id"], iso[:10])).fetchone()
        if dup:
            if not is_verified(call_id, phone):
                return _err("DUPLICATE_BOOKING", f"This number already has an appointment with {doc['name']} that day. "
                            "Details are private until the caller is verified (send_verification_code).")
            return _err("DUPLICATE_BOOKING",
                        f"This patient already has appointment {dup['id']} with {doc['name']} on the same day.",
                        existing=_fmt_slot(dup["slot_start"]) | {"appointment_id": dup["id"]})
        slot = c.execute("SELECT status FROM slots WHERE doctor_id=? AND start=?", (doc["id"], iso)).fetchone()
        if not slot:
            return _err("NO_SUCH_SLOT", "That time is not a valid slot for this doctor. Use check_slots.")
        if slot["status"] != "free":
            alt = check_slots(doc["id"], iso[:10], limit=3)
            return _err("SLOT_TAKEN", "Someone just booked that slot.", alternatives=alt.get("free_slots", []))
        apt = _next_id("APT", 1040)
        c.execute("UPDATE slots SET status='booked', appointment_id=? WHERE doctor_id=? AND start=?", (apt, doc["id"], iso))
        c.execute("INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (apt, doc["id"], iso, phone, patient_name.strip(), reason, language, "confirmed",
                   now().isoformat(), "voice_agent", call_id))
        if not c.execute("SELECT 1 FROM patients WHERE phone=?", (phone,)).fetchone():
            c.execute("INSERT INTO patients VALUES(?,?,?,?,?,?,?)", (phone, patient_name.strip(), language, None, None, None, None))
        c.commit()
    return {"ok": True, "appointment_id": apt, "doctor": doc["name"], "fee_inr": doc["fee"],
            "patient_name": patient_name.strip(), "phone": phone, **_fmt_slot(iso),
            "instructions": "Arrive 15 minutes early with photo ID, old prescriptions and insurance card."}


def cancel_appointment(appointment_id: str, caller_confirmed: bool = False, call_id: str | None = None) -> dict:
    with _lock:
        c = conn()
        a = c.execute("SELECT * FROM appointments WHERE id=?", (str(appointment_id).upper(),)).fetchone()
        if not a or a["status"] != "confirmed":
            return _err("NOT_FOUND", f"No active appointment {appointment_id}.")
        # Undoing a booking made on this same call needs no code; anything else does.
        if a["call_id"] != call_id and not is_verified(call_id, a["patient_phone"]):
            return _needs_verification(a["patient_phone"])
        if not caller_confirmed:
            return _err("NEEDS_CONFIRMATION", "Confirm with the caller before cancelling, then call with caller_confirmed=true.")
        c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (a["id"],))
        c.execute("UPDATE slots SET status='free', appointment_id=NULL WHERE appointment_id=?", (a["id"],))
        c.commit()
    return {"ok": True, "cancelled": a["id"], **_fmt_slot(a["slot_start"])}


def _match_procedure(text: str) -> str | None:
    t = (text or "").lower().replace("_", " ")
    best = None
    for key, syns in COVERAGE["procedures"].items():
        for s in [key.replace("_", " "), *syns]:
            if s in t and (best is None or len(s) > best[1]):
                best = (key, len(s))
    return best[0] if best else None


def _match_insurer(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for name in COVERAGE["insurers"]:
        if name.split()[0].lower() in t:
            return name
    return None


def check_insurance_coverage(procedure: str, patient_phone: str | None = None,
                             insurer: str | None = None, plan: str | None = None, call_id: str | None = None) -> dict:
    policy_start = None
    policy_no = None
    if patient_phone and not is_verified(call_id, patient_phone):
        if insurer:  # general rule for a named insurer/plan is public information
            patient_phone = None
        else:
            p = normalize_phone(patient_phone)
            if p and conn().execute("SELECT 1 FROM patients WHERE phone=?", (p,)).fetchone():
                return _needs_verification(p)
            patient_phone = None
    if patient_phone:
        pr = lookup_patient(patient_phone, call_id)
        if pr.get("found") and pr["insurance"]["insurer"]:
            insurer, plan = pr["insurance"]["insurer"], pr["insurance"]["plan"]
            policy_start, policy_no = pr["insurance"]["policy_start"], pr["insurance"]["policy_no"]
    ins = _match_insurer(insurer)
    if not ins:
        return _err("UNKNOWN_INSURER", "Ask which insurance company and plan (or the registered phone number).",
                    supported=list(COVERAGE["insurers"]))
    plans = COVERAGE["insurers"][ins]["plans"]
    plan_key = next((p for p in plans if plan and p.lower() in plan.lower()), None)
    if not plan_key:
        if len(plans) == 1:
            plan_key = next(iter(plans))
        else:
            return _err("UNKNOWN_PLAN", f"Ask which {ins} plan the caller has.", plans=list(plans))
    proc = _match_procedure(procedure)
    if not proc:
        return _err("UNKNOWN_PROCEDURE", "Procedure not in the coverage table; use search_clinic_knowledge or escalate.",
                    known=list(COVERAGE["procedures"]))
    rule = plans[plan_key][proc]
    out = {"ok": True, "insurer": ins, "plan": plan_key, "procedure": proc, "covered": rule["covered"],
           "clause": rule["clause"], "cashless_at_this_clinic": COVERAGE["insurers"][ins]["cashless_at_clinic"],
           "source": f"{ins} {plan_key} policy wording, clause {rule['clause']}"}
    for k in ("limit", "waiting_months", "preauth", "preauth_notice", "prior_intimation", "copay_pct"):
        if k in rule:
            out[k] = rule[k]
    if policy_no:
        out["policy_no"] = policy_no
    if rule["covered"] and rule.get("waiting_months") and policy_start:
        start = dt.date.fromisoformat(policy_start)
        served = _months_between(start, now().date())
        out["policy_start"] = policy_start
        out["months_completed"] = served
        if served < rule["waiting_months"]:
            eligible_from = _shift_months(start, rule["waiting_months"])
            out["covered_now"] = False
            out["reason"] = (f"Waiting period of {rule['waiting_months']} months not completed "
                             f"({served} months done). Eligible from {eligible_from.isoformat()}.")
        else:
            out["covered_now"] = True
    elif rule["covered"]:
        out["covered_now"] = True if not rule.get("waiting_months") else None
        if rule.get("waiting_months"):
            out["note"] = "Policy start date unknown; ask for the registered phone number to check the waiting period."
    else:
        out["covered_now"] = False
    return out


def create_preauth_request(patient_phone: str, procedure: str, doctor_id: str = "",
                           estimated_cost_inr: int | None = None, call_id: str | None = None) -> dict:
    p = normalize_phone(patient_phone)
    if p and not is_verified(call_id, p):
        return _needs_verification(p)
    pr = lookup_patient(patient_phone, call_id)
    if not pr.get("ok"):
        return pr
    if not pr.get("found") or not pr["insurance"]["insurer"]:
        return _err("NO_POLICY_ON_FILE", "No insurance policy on file for this number; staff must register it first. Escalate.")
    cov = check_insurance_coverage(procedure, patient_phone=patient_phone, call_id=call_id)
    if not cov.get("ok"):
        return cov
    if cov.get("covered_now") is False:
        return _err("NOT_ELIGIBLE", "Coverage check says this is not covered right now; do not raise pre-authorization.",
                    coverage=cov)
    with _lock:
        pa = _next_id("PA", 500)
        conn().execute("INSERT INTO preauths(id, created_at, call_id, patient_phone, patient_name, insurer, plan, procedure, "
                       "doctor_id, estimated_cost, clause, status, staff_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pa, now().isoformat(), call_id, pr["phone"], pr["name"], cov["insurer"], cov["plan"],
                        cov["procedure"], doctor_id or None, estimated_cost_inr, cov["clause"], "pending_staff_review", None))
        conn().commit()
    return {"ok": True, "preauth_id": pa, "status": "pending_staff_review", "procedure": cov["procedure"],
            "insurer": cov["insurer"], "plan": cov["plan"], "clause": cov["clause"],
            "next_step": "A front-desk staff member reviews documents and submits to the insurer. "
                         "Tell the caller they will be updated; if a [STAFF UPDATE] arrives during this call, relay it."}


def escalate_to_human(reason: str, urgency: str = "routine", summary_en: str = "", caller_language: str = "",
                      caller_phone: str | None = None, call_id: str | None = None) -> dict:
    urgency = urgency if urgency in ("routine", "urgent", "emergency") else "routine"
    with _lock:
        ho = _next_id("HO", 200)
        conn().execute("INSERT INTO handoffs(id, created_at, call_id, reason, urgency, summary, language, caller_phone, "
                       "status, staff_name, staff_note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (ho, now().isoformat(), call_id, reason, urgency, summary_en, caller_language,
                        normalize_phone(caller_phone) or caller_phone, "waiting", None, None))
        conn().commit()
    msg = {"emergency": "Staff alerted immediately. Keep telling the caller to call 108 / go to the nearest emergency now.",
           "urgent": "A staff member will pick up shortly. Ask the caller to stay on the line.",
           "routine": "A staff member will call back within 30 minutes during clinic hours."}[urgency]
    return {"ok": True, "handoff_id": ho, "urgency": urgency, "status": "waiting", "next_step": msg}


def send_sms_confirmation(appointment_id: str, message: str, language: str = "", call_id: str | None = None) -> dict:
    a = conn().execute("SELECT * FROM appointments WHERE id=?", (str(appointment_id).upper(),)).fetchone()
    if not a:
        return _err("NOT_FOUND", f"No appointment {appointment_id}.")
    with _lock:
        conn().execute("INSERT INTO outbox(created_at,to_phone,language,text,appointment_id,call_id) VALUES(?,?,?,?,?,?)",
                       (now().isoformat(), a["patient_phone"], language, message, a["id"], call_id))
        conn().commit()
    return {"ok": True, "sent_to": _mask(a["patient_phone"]), "channel": "SMS (mock)",
            "_deliver_sms": {"to": a["patient_phone"], "text": message}}


# ------------------------------------------------------------ staff (dashboard)

def decide_preauth(pa_id: str, decision: str, note: str = "", staff_name: str = "") -> dict | None:
    """Returns {"row": ..., "conflict": bool}. A pre-auth can be decided once; later clicks are conflicts."""
    with _lock:
        r = conn().execute("SELECT * FROM preauths WHERE id=?", (pa_id,)).fetchone()
        if not r:
            return None
        if r["status"] != "pending_staff_review":
            return {"row": dict(r), "conflict": True}
        conn().execute("UPDATE preauths SET status=?, staff_note=?, staff_name=?, decided_at=? WHERE id=?",
                       (decision, note, staff_name, now().isoformat(), pa_id))
        conn().commit()
        return {"row": dict(conn().execute("SELECT * FROM preauths WHERE id=?", (pa_id,)).fetchone()), "conflict": False}


def update_handoff(ho_id: str, status: str, staff_name: str = "", note: str = "") -> dict | None:
    """waiting -> taken (once) -> resolved. Taking an already-taken hand-off is a conflict."""
    with _lock:
        r = conn().execute("SELECT * FROM handoffs WHERE id=?", (ho_id,)).fetchone()
        if not r:
            return None
        allowed = {"taken": ("waiting",), "resolved": ("waiting", "taken")}[status]
        if r["status"] not in allowed:
            return {"row": dict(r), "conflict": True}
        who = staff_name if status == "taken" or not r["staff_name"] else r["staff_name"]
        conn().execute("UPDATE handoffs SET status=?, staff_name=?, staff_note=?, updated_at=? WHERE id=?",
                       (status, who, note or r["staff_note"], now().isoformat(), ho_id))
        conn().commit()
        return {"row": dict(conn().execute("SELECT * FROM handoffs WHERE id=?", (ho_id,)).fetchone()), "conflict": False}


def save_call(summary: dict) -> None:
    with _lock:
        conn().execute("INSERT OR REPLACE INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (summary["id"], summary["started_at"], summary["ended_at"], summary["duration_s"],
                        json.dumps(summary["languages"]), summary["turns"], summary["tool_calls"],
                        summary["interruptions"], json.dumps(summary["latencies"]), json.dumps(summary["outcomes"])))
        conn().commit()


def dashboard_state() -> dict:
    c = conn()
    today = now().date().isoformat()
    rows = lambda q, *a: [dict(r) for r in c.execute(q, a).fetchall()]
    appts = rows("SELECT a.*, d.name doctor FROM appointments a JOIN doctors d ON d.id=a.doctor_id "
                 "WHERE a.status='confirmed' AND a.slot_start>=? ORDER BY a.created_at DESC LIMIT 40", today)
    calls = rows("SELECT * FROM calls ORDER BY started_at DESC")
    lat = [x for cl in calls for x in json.loads(cl["latencies"] or "[]")]
    lat_sorted = sorted(lat)
    outcomes = [json.loads(cl["outcomes"] or "[]") for cl in calls]
    handled = [o for o in outcomes if "handoff" not in o]
    metrics = {
        "calls": len(calls),
        "bookings_by_agent": c.execute("SELECT count(*) FROM appointments WHERE source='voice_agent'").fetchone()[0],
        "handoffs": c.execute("SELECT count(*) FROM handoffs").fetchone()[0],
        "preauths": c.execute("SELECT count(*) FROM preauths").fetchone()[0],
        "interruptions_handled": sum(cl["interruptions"] or 0 for cl in calls),
        "automation_rate": round(100 * len(handled) / len(calls)) if calls else None,
        "latency_p50_ms": lat_sorted[len(lat_sorted) // 2] if lat_sorted else None,
        "latency_p90_ms": lat_sorted[int(len(lat_sorted) * 0.9)] if lat_sorted else None,
        "avg_call_s": round(sum(cl["duration_s"] or 0 for cl in calls) / len(calls)) if calls else None,
        "languages": sorted({l for cl in calls for l in json.loads(cl["languages"] or "[]")}),
    }
    return {
        "now": now().isoformat(),
        "doctors": [_doctor_public(r) for r in c.execute("SELECT * FROM doctors")],
        "appointments": appts,
        "handoffs": rows("SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 30"),
        "preauths": rows("SELECT * FROM preauths ORDER BY created_at DESC LIMIT 30"),
        "outbox": rows("SELECT * FROM outbox ORDER BY id DESC LIMIT 20"),
        "calls": calls[:15],
        "metrics": metrics,
    }
