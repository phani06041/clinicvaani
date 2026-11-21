// ClinicVaani staff console: live call monitor, agent actions, approvals, appointments, metrics.
const $ = (id) => document.getElementById(id);
const wsBase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;

function scriptOf(t) {
  const c = { Telugu: 0, Hindi: 0, Tamil: 0, Kannada: 0, Latin: 0 };
  for (const ch of t) {
    const o = ch.codePointAt(0);
    if (o >= 0x0c00 && o <= 0x0c7f) c.Telugu++; else if (o >= 0x0900 && o <= 0x097f) c.Hindi++;
    else if (o >= 0x0b80 && o <= 0x0bff) c.Tamil++; else if (o >= 0x0c80 && o <= 0x0cff) c.Kannada++;
    else if (/[a-z]/i.test(ch)) c.Latin++;
  }
  const best = Object.entries(c).sort((a, b) => b[1] - a[1])[0];
  if (!best[1]) return '';
  const native = Object.entries(c).filter(([k, v]) => k !== 'Latin' && v > 0);
  if (native.length && c.Latin > 3) return native[0][0] + '+English';
  return best[0] === 'Latin' ? 'Latin script' : best[0];
}


const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtTime = (iso) => new Date(iso).toLocaleString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', timeZone: 'Asia/Kolkata' });
const pending = {};

// ---------------------------------------------------------------- staff session
let staffName = null;
function toast(msg, kind = 'warn') {
  const t = $('toast'); t.textContent = msg; t.className = 'toast show ' + kind;
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = 'toast'), 4500);
}
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { showLogin(); throw new Error('signed out'); }
  if (r.status === 409) { const j = await r.json().catch(() => ({})); toast(j.detail || 'Already handled by someone else'); refresh(); throw new Error('conflict'); }
  if (!r.ok) { toast('Request failed: ' + r.status); throw new Error(String(r.status)); }
  return r.json();
}
function showLogin(msg = '') {
  staffName = null;
  $('login').classList.remove('hidden'); $('loginErr').textContent = msg; $('whoami').textContent = '';
  setTimeout(() => $('loginName').focus(), 50);
}
function signedIn(name) {
  staffName = name;
  $('login').classList.add('hidden');
  $('whoami').innerHTML = `Signed in as <b>${esc(name)}</b> · <a href="#" id="logout">Sign out</a>`;
  $('logout').onclick = async (e) => { e.preventDefault(); await fetch('/api/staff/logout', { method: 'POST' }); location.reload(); };
  refresh(); connectOps();
}
$('loginForm').onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch('/api/staff/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: $('loginName').value, pin: $('loginPin').value }) });
  if (r.ok) { $('loginPin').value = ''; signedIn((await r.json()).name); } else { $('loginErr').textContent = 'Wrong PIN, try again.'; }
};

function summarize(name, r) {
  if (!r.ok) return `<span class="badge bad">${esc(r.error)}</span> ${esc(r.message || '')}`;
  switch (name) {
    case 'find_doctors': return r.doctors.map((d) => `${esc(d.name)} — ${esc(d.specialty)}`).join('<br>');
    case 'check_slots': return r.free_slots.length ? `${r.free_slots.length} shown of ${r.total_free_that_day} free on ${esc(r.day)}: ${r.free_slots.map((s) => s.time).join(', ')}`
      : esc(r.note) + (r.next_available?.length ? ` Next: ${r.next_available.map((s) => s.day + ' ' + s.time).join(', ')}` : '');
    case 'lookup_patient': return r.found ? `${esc(r.name)} · ${esc(r.insurance.insurer || 'no insurance')} ${esc(r.insurance.plan || '')} · ${r.upcoming_appointments.length} upcoming` : 'New patient';
    case 'book_appointment': return `<b>Booked ${esc(r.appointment_id)}</b> · ${esc(r.doctor)} · ${esc(r.day)} ${esc(r.time)} · ${esc(r.patient_name)}`;
    case 'cancel_appointment': return `Cancelled ${esc(r.cancelled)}`;
    case 'check_insurance_coverage': return `${esc(r.insurer)} ${esc(r.plan)} · ${esc(r.procedure.replace('_', ' '))}: ` +
      (r.covered_now === false ? `<span class="badge bad">not covered now</span> ${esc(r.reason || '')}` : `<span class="badge ok">covered</span> ${esc(r.limit || '')}${r.preauth ? ' · pre-auth needed' : ''}`) +
      ` <span class="tag">${esc(r.clause)}</span>`;
    case 'search_clinic_knowledge': return r.results.map((h) => `<span class="tag">${esc(h.source)}</span>`).join(' ') || esc(r.note);
    case 'create_preauth_request': return `<span class="badge pending">${esc(r.preauth_id)} awaiting staff</span>`;
    case 'escalate_to_human': return `<span class="badge ${r.urgency === 'emergency' ? 'red' : 'pending'}">${esc(r.handoff_id)} · ${esc(r.urgency)}</span> ${esc(r.next_step)}`;
    case 'send_sms_confirmation': return `SMS to ${esc(r.sent_to)}`;
    default: return 'ok';
  }
}


// ---------------------------------------------------------------- live call monitor
const monitors = {};
function monitorFor(id) {
  if (monitors[id]) return monitors[id];
  const box = $('monitor');
  box.querySelector('.empty')?.remove();
  const el = document.createElement('div');
  el.className = 'mon';
  el.innerHTML = `<div class="mon-head"><span class="live-dot"></span><b class="mono">${esc(id)}</b><span class="mon-state">on call with Vaani</span></div><div class="mon-body"></div>`;
  box.prepend(el);
  monitors[id] = { el, body: el.querySelector('.mon-body'), last: null, role: null };
  return monitors[id];
}
function monitorTranscript(ev) {
  const m = monitorFor(ev.call_id);
  if (m.role !== ev.role || !m.last) {
    m.last = document.createElement('div');
    m.last.className = 'line ' + ev.role;
    m.last.innerHTML = `<span class="who">${ev.role === 'caller' ? 'Caller' : 'Vaani'}</span><span class="lang"></span> <span class="txt"></span>`;
    m.body.appendChild(m.last); m.role = ev.role;
  }
  const t = m.last.querySelector('.txt'); t.textContent += ev.delta;
  if (ev.role === 'caller') { const tag = scriptOf(t.textContent); m.last.querySelector('.lang').innerHTML = tag ? `<span class="tag">${tag}</span>` : ''; }
  m.body.scrollTop = m.body.scrollHeight;
}

function timelineAdd(html, cls = '') {
  const tl = $('timeline');
  tl.querySelector('.empty')?.remove();
  const li = document.createElement('li');
  li.className = cls; li.innerHTML = html;
  tl.prepend(li);
  while (tl.children.length > 80) tl.lastChild.remove();
  return li;
}

function onOpsEvent(ev) {
  switch (ev.type) {
    case 'transcript': monitorTranscript(ev); break;
    case 'turn_complete': if (monitors[ev.call_id]) monitors[ev.call_id].last = null; break;
    case 'tool_call':
      pending[ev.id] = timelineAdd(`<div class="head"><span class="fn">${esc(ev.name)}</span><span class="badge pending">running</span><span class="ms"></span></div>
        <div class="args">${esc(JSON.stringify(ev.args))}</div>`);
      break;
    case 'tool_result': {
      const li = pending[ev.id] || timelineAdd(`<div class="head"><span class="fn">${esc(ev.name)}</span></div>`);
      const b = li.querySelector('.badge');
      if (b) { b.className = 'badge ' + (ev.result.ok ? 'ok' : 'bad'); b.textContent = ev.result.ok ? 'ok' : 'exception handled'; }
      li.querySelector('.ms').textContent = ev.ms + ' ms';
      li.insertAdjacentHTML('beforeend', `<div class="summary">${summarize(ev.name, ev.result)}</div>
        <details><summary>raw result</summary><pre>${esc(JSON.stringify(ev.result, null, 2))}</pre></details>`);
      delete pending[ev.id];
      break;
    }
    case 'call_started': monitorFor(ev.call_id).el.querySelector('.mon-state').textContent = 'on call · ' + (ev.language === 'ask' ? 'language: ask' : ev.language + ' chosen'); timelineAdd(`📞 ${esc(ev.call_id)} connected · ${esc(ev.model)}`, 'event'); break;
    case 'interrupted': if (monitors[ev.call_id]) { monitors[ev.call_id].last?.querySelector('.who')?.insertAdjacentHTML('beforeend', ' <span class="tag">interrupted</span>'); monitors[ev.call_id].last = null; }
      timelineAdd(`✋ Caller interrupted the agent — playback stopped`, 'event'); break;
    case 'system_injected': timelineAdd(`🧑‍⚕️ Staff update injected into the live call`, 'event'); break;
    case 'call_ended': if (monitors[ev.call_id]) { monitors[ev.call_id].el.classList.add('ended'); monitors[ev.call_id].el.querySelector('.mon-state').textContent = 'call ended · ' + ev.summary.duration_s + 's'; }
      timelineAdd(`Call ${esc(ev.call_id)} ended · ${ev.summary.duration_s}s · ${ev.summary.tool_calls} actions · ${esc(ev.summary.outcomes.join(', ') || 'no outcome')}`, 'event'); break;
    case 'state_changed': refresh(); break;
    case 'latency': refreshSoon(); break;
  }
}

let seenAppts = null;
async function refresh() {
  let s;
  try { s = await api('/api/state'); } catch { return; }
  const m = s.metrics;
  const tiles = [
    ['Calls', m.calls], ['Bookings by agent', m.bookings_by_agent], ['Resolved w/o human', m.automation_rate == null ? '–' : m.automation_rate + '%'],
    ['Response p50', m.latency_p50_ms == null ? '–' : m.latency_p50_ms + ' ms'], ['Interruptions handled', m.interruptions_handled],
    ['Hand-offs', m.handoffs], ['Languages', m.languages.length ? m.languages.join(', ') : '–'],
  ];
  $('metrics').innerHTML = tiles.map(([k, v]) => `<div class="metric"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('');

  const ids = new Set(s.appointments.map((a) => a.id));
  $('appts').innerHTML = s.appointments.map((a) => `<tr class="${seenAppts && !seenAppts.has(a.id) ? 'new' : ''}">
    <td class="mono">${esc(a.id)}</td><td>${fmtTime(a.slot_start)}</td><td>${esc(a.doctor)}</td>
    <td>${esc(a.patient_name)}</td><td>${esc(a.language || '')}</td><td>${a.source === 'voice_agent' ? '🎙️ agent' : esc(a.source)}</td></tr>`).join('')
    || '<tr><td colspan="6" class="empty">No upcoming appointments.</td></tr>';
  seenAppts = ids;

  const q = [];
  for (const h of s.handoffs) {
    q.push(`<div class="qitem ${h.urgency === 'emergency' && h.status === 'waiting' ? 'emergency' : ''}">
      <div class="t">${esc(h.id)} <span class="badge ${h.urgency === 'emergency' ? 'red' : h.urgency === 'urgent' ? 'bad' : 'pending'}">${esc(h.urgency)}</span>
        <span class="badge ${h.status === 'waiting' ? 'pending' : 'ok'}">${esc(h.status)}</span> ${esc(h.reason)}</div>
      <p>${esc(h.summary)}${h.language ? ' · ' + esc(h.language) : ''}${h.caller_phone ? ' · ' + esc(h.caller_phone) : ''}</p>
      ${h.status === 'waiting' ? `<div class="actions"><button class="primary" data-ho="${h.id}" data-d="taken">Take over call</button>
        <button data-ho="${h.id}" data-d="resolved">Mark resolved</button></div>` :
        h.status === 'taken' ? `<p>Taken by <b>${esc(h.staff_name)}</b></p><div class="actions"><button data-ho="${h.id}" data-d="resolved">Mark resolved</button></div>` :
        h.staff_name ? `<p>Resolved · ${esc(h.staff_name)}</p>` : ''}</div>`);
  }
  for (const p of s.preauths) {
    q.push(`<div class="qitem"><div class="t">${esc(p.id)} pre-auth <span class="badge ${p.status === 'approved' ? 'ok' : p.status === 'rejected' ? 'red' : 'pending'}">${esc(p.status.replaceAll('_', ' '))}</span></div>
      <p>${esc(p.patient_name)} · ${esc(p.procedure.replace('_', ' '))} · ${esc(p.insurer)} ${esc(p.plan)} <span class="tag">${esc(p.clause)}</span>
      ${p.estimated_cost ? ' · est. ₹' + Number(p.estimated_cost).toLocaleString('en-IN') : ''}</p>
      ${p.status === 'pending_staff_review' ? `<div class="actions"><button class="primary" data-pa="${p.id}" data-d="approved">Approve</button>
        <button data-pa="${p.id}" data-d="need_more_info">Need documents</button><button class="danger" data-pa="${p.id}" data-d="rejected">Reject</button></div>` : `<p>${p.staff_name ? 'By <b>' + esc(p.staff_name) + '</b>' : ''}${p.staff_note ? ' · ' + esc(p.staff_note) : ''}</p>`}</div>`);
  }
  $('queue').innerHTML = q.join('') || '<p class="empty">No hand-offs or pre-authorizations yet.</p>';
  $('outbox').innerHTML = s.outbox.map((o) => `<div class="sms"><div class="meta">${esc(o.appointment_id)} → ${esc(o.to_phone.slice(0, 2))}XXXXXX${esc(o.to_phone.slice(-2))} · ${esc(o.language || '')}</div>${esc(o.text)}</div>`).join('')
    || '<p class="empty">Nothing sent yet.</p>';
}
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 400); }

$('queue').onclick = async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const note = b.dataset.d === 'approved' ? 'Documents verified, surgery can be scheduled.' :
    b.dataset.d === 'need_more_info' ? 'Please bring the eye surgeon\'s recommendation letter and cost estimate.' :
    b.dataset.d === 'rejected' ? 'Insurer declined the request.' : '';
  const url = b.dataset.pa ? `/api/preauths/${b.dataset.pa}/decision` : `/api/handoffs/${b.dataset.ho}/status`;
  b.disabled = true;
  try {
    await api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision: b.dataset.d, note }) });
    toast(`${b.dataset.pa || b.dataset.ho}: ${b.dataset.d.replace('_', ' ')}`, 'ok');
  } catch { b.disabled = false; }
};
$('resetBtn').onclick = async () => { if (confirm('Reset all demo bookings, hand-offs and call logs?')) { await api('/api/reset', { method: 'POST' }); $('timeline').innerHTML = '<li class="empty">Tool calls appear here live.</li>'; } };

let opsSock = null;
function connectOps() {
  if (opsSock && opsSock.readyState <= 1) return;
  const o = (opsSock = new WebSocket(wsBase + '/ws/ops'));
  o.onmessage = (e) => onOpsEvent(JSON.parse(e.data));
  o.onclose = async () => {
    const me = await fetch('/api/staff/me');
    if (me.status === 401) showLogin('Session ended, please sign in again.'); else setTimeout(connectOps, 1500);
  };
}
fetch('/api/config').then((r) => r.json()).then((c) => {
  $('modelName').textContent = c.model + ' via ' + c.backend + (c.has_key ? '' : ' · GEMINI_API_KEY missing');
});
fetch('/api/staff/me').then(async (r) => (r.ok ? signedIn((await r.json()).name) : showLogin()));
