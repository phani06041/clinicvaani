// ClinicVaani caller screen: mic <-> Gemini Live (via our server) <-> speaker, with live captions.
const $ = (id) => document.getElementById(id);
const wsBase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;

// ------------------------------------------------------------------ language tag
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

// ------------------------------------------------------------------ caller line
let ws = null, capCtx = null, playCtx = null, capNode = null, playNode = null, micStream = null;
let textOnly = false;
let callStart = 0, timerId = null;
let agentPlaying = false, lastVoiceAt = 0, awaiting = false, floor = 0.01, interruptions = 0;
let lastBubble = null, lastRole = null;

function setState(text, cls = '') { const p = $('callState'); p.textContent = text; p.className = 'pill ' + cls; }

function addBubble(role, text, extra = '') {
  const t = $('transcript');
  t.querySelector('.hint')?.remove();
  const div = document.createElement('div');
  div.className = 'bubble ' + role;
  div.innerHTML = role === 'system' ? '' : `<span class="who">${role === 'caller' ? 'Caller' : 'Vaani'}${extra}<span class="lang"></span></span>`;
  const body = document.createElement('span');
  body.className = 'body';
  body.textContent = text;
  div.appendChild(body);
  t.appendChild(div);
  t.scrollTop = t.scrollHeight;
  return div;
}

function appendTranscript(role, delta, typed) {
  if (lastRole !== role || !lastBubble) {
    lastBubble = addBubble(role, '', typed ? ' <span class="tag">typed</span>' : '');
    lastRole = role;
  }
  const body = lastBubble.querySelector('.body');
  body.textContent += delta;
  if (role === 'caller') {
    const tag = scriptOf(body.textContent);
    lastBubble.querySelector('.lang').innerHTML = tag ? ` <span class="tag">${tag}</span>` : '';
  }
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

function playPcm(buf) {
  const i16 = new Int16Array(buf);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  playNode.port.postMessage(f32);
  if (awaiting && lastVoiceAt) {
    const ms = Math.round(performance.now() - lastVoiceAt);
    awaiting = false;
    if (ms > 0 && ms < 15000) {
      $('lastLat').textContent = ms + ' ms';
      ws?.readyState === 1 && ws.send(JSON.stringify({ type: 'latency', ms }));
    }
  }
}

async function startCall() {
  setState('connecting…');
  $('callBtn').disabled = true;
  try {
    playCtx = new AudioContext({ sampleRate: 24000 });
    await playCtx.audioWorklet.addModule('/static/audio-worklets.js');
    playNode = new AudioWorkletNode(playCtx, 'player-processor');
    playNode.connect(playCtx.destination);
    playNode.port.onmessage = (e) => { agentPlaying = e.data.playing; $('agentDot').classList.toggle('on', agentPlaying); };
  } catch (e) {
    setState('audio error', 'err'); $('callBtn').disabled = false;
    addBubble('system', 'Audio output not available: ' + e.message);
    return;
  }
  textOnly = false;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    capCtx = new AudioContext();
    await capCtx.audioWorklet.addModule('/static/audio-worklets.js');
    capNode = new AudioWorkletNode(capCtx, 'capture-processor');
    capCtx.createMediaStreamSource(micStream).connect(capNode);
    capNode.port.postMessage({ noise: $('noise').value / 100 });
  } catch (e) {
    // No mic (blocked, missing, or an embedded browser): carry on as a typed call. Vaani still speaks.
    textOnly = true; capNode = null; capCtx = null; micStream = null;
  }

  ws = new WebSocket(wsBase + '/ws/call' + (chosenLang ? '?lang=' + encodeURIComponent(chosenLang) : ''));
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    setState(textOnly ? 'connected · typing' : 'connected', 'live');
    callStart = Date.now(); clearInterval(timerId);
    timerId = setInterval(() => { const s = Math.floor((Date.now() - callStart) / 1000); if ($('timer')) $('timer').textContent = String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }, 500);
    $('callBtn').disabled = false; $('callBtn').textContent = 'Hang up'; $('callBtn').classList.add('hang');
    lastBubble = null; lastRole = null; interruptions = 0; $('intCount').textContent = '0';
    $('transcript').innerHTML = '';
    if (textOnly) { addBubble('system', 'No microphone: typed call. Type below, Vaani answers by voice.'); lastBubble = null; $('typeInput').focus(); }
    if (capNode) capNode.port.onmessage = (e) => {
      const { pcm, rms } = e.data;
      $('micLevel').style.width = Math.min(100, rms * 600) + '%';
      const thr = Math.max(0.012, floor * 3);
      if (rms < thr) floor = floor * 0.97 + rms * 0.03;
      // ignore our own echo while the agent talks, unless it is clearly a barge-in
      if (rms > thr && (!agentPlaying || rms > thr * 3)) { lastVoiceAt = performance.now(); awaiting = true; }
      if (ws?.readyState === 1) ws.send(pcm);
    };
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== 'string') { playPcm(e.data); return; }
    const m = JSON.parse(e.data);
    switch (m.type) {
      case 'call_started': $('callId').textContent = m.call_id; break;
      case 'transcript': appendTranscript(m.role, m.delta, m.typed); break;
      case 'interrupted':
        playNode?.port.postMessage('clear');
        $('intCount').textContent = ++interruptions;
        
        lastBubble = null;
        break;
      case 'turn_complete': lastBubble = null; break;
      case 'system_injected': lastBubble = null; break;
      case 'error': setState('error', 'err'); addBubble('system', m.message); break;
      case 'notice': addBubble('system', m.message); break;
      case 'sms': { const b = addBubble('sms', m.text); b.querySelector('.who')?.remove(); b.insertAdjacentHTML('afterbegin', `<span class="who">💬 SMS to ${m.to}</span>`); lastBubble = null; break; }
    }
  };
  ws.onclose = () => endCall(true);
}

function endCall(fromSocket = false) {
  if (!fromSocket && ws?.readyState === 1) { ws.send(JSON.stringify({ type: 'hangup' })); ws.close(); }
  ws = null;
  micStream?.getTracks().forEach((t) => t.stop());
  capCtx?.close(); playCtx?.close(); capCtx = playCtx = null;
  $('micLevel').style.width = '0'; $('agentDot').classList.remove('on');
  $('callBtn').textContent = 'Start call'; $('callBtn').classList.remove('hang'); $('callBtn').disabled = false;
  clearInterval(timerId);
  if ($('callState').textContent !== 'error') setState('call ended');
}

$('callBtn').onclick = () => (ws ? endCall() : startCall());
$('noise').oninput = () => {
  const v = $('noise').value;
  $('noiseVal').textContent = v === '0' ? 'off' : v + '%';
  capNode?.port.postMessage({ noise: v / 100 });
};
$('typeForm').onsubmit = (e) => {
  e.preventDefault();
  const text = $('typeInput').value.trim();
  if (!text || ws?.readyState !== 1) return;
  ws.send(JSON.stringify({ type: 'text', text }));
  lastVoiceAt = performance.now(); awaiting = true;
  $('typeInput').value = '';
};


fetch('/api/config').then((r) => r.json()).then((c) => {
  if (!c.has_key) { setState('unavailable', 'err'); addBubble('system', 'The clinic line is not configured (GEMINI_API_KEY missing).'); }
});

// ---------------------------------------------------------------- preferred language
let chosenLang = '';
try { chosenLang = localStorage.getItem('cv_lang') || ''; } catch { /* storage unavailable */ }
function paintLangs() {
  document.querySelectorAll('#langs button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.lang === chosenLang)));
}
document.getElementById('langs')?.addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b || ws) return; // language is fixed once a call starts
  chosenLang = b.dataset.lang;
  try { localStorage.setItem('cv_lang', chosenLang); } catch { /* ignore */ }
  paintLangs();
});
paintLangs();
