'use strict';

/**
 * voice.js — Reliable voice capture via AudioContext ScriptProcessor.
 *
 * WHY not MediaRecorder → decodeAudioData:
 *   MediaRecorder produces compressed webm/ogg. decodeAudioData on that
 *   blob fails silently on many Windows / Chrome / Edge setups.
 *
 * SOLUTION:
 *   ScriptProcessor captures raw Float32 PCM directly from the mic at the
 *   browser's native rate → we resample to 16 kHz mono → write PCM-16 WAV
 *   → POST to Flask → Whisper transcribes it reliably.
 */

// ── State ─────────────────────────────────────────────────────────────────────
let audioCtx        = null;   // created ONCE on first user gesture, never closed
let nativeSampleRate = 44100; // set when audioCtx is created
let scriptProcessor = null;
let micSource       = null;
let micStream       = null;
let pcmBuffers      = [];
let isRecording     = false;
let recordTimeout   = null;
let isSpeaking      = false;   // true while TTS audio is playing
let _ttsAudio       = null;    // reference to current <audio> element
let _emailStep      = null;    // current step of voice-guided email compose (or null)
let _msgStep         = null;    // current step of voice-guided Telegram message compose
let _activeService   = null;    // 'email' | 'telegram' | null — chosen by user at first tap
let _choosingService = false;   // true while waiting for user to say which service
let _wsRecog         = null;    // Web Speech API recognizer used during TTS playback
// Initialize from localStorage immediately so the FIRST request already uses
// the stored language — not hardcoded 'en'.  Falls back to 'en' if not set.
let _voiceLang       = localStorage.getItem('voicemail_lang') || 'en';

// Expose setter so dashboard inline script can sync after dropdown change
function _setVoiceLang(lang) { _voiceLang = lang || 'en'; }

const TARGET_SAMPLE_RATE = 16000;   // Whisper requirement
const MAX_RECORD_SECONDS = 8;        // max listen window (seconds) — hard cap
const EMAIL_LISTEN_SECS  = 10;       // longer window after email reading
const BUFFER_SIZE        = 4096;

// ── Silence-gate: stop recording automatically after N ms of silence ─────────
// This prevents sending 8-10 s of dead air to Whisper, cutting latency by ~5×.
const SILENCE_THRESHOLD_RMS = 0.008; // RMS below this = silence (0-1 float32 scale)
const SILENCE_STOP_MS       = 1500;  // stop if silence lasts this long AFTER speech
let   _hadSpeech             = false; // true once we've seen at least one voiced frame
let   _silenceTimer          = null;  // setTimeout handle for auto-stop

let _lastIntent = null;              // tracks last server-returned intent

// ── DOM shorthand ─────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ── Entry point (onclick) ─────────────────────────────────────────────────────
async function toggleRecording() {
  // If AI is speaking, stop it — stopSpeaking() will auto-restart recording
  if (isSpeaking) { stopSpeaking(); return; }
  if (isRecording) { stopRecording(); return; }

  // ── First tap (no service chosen yet) — ask Email or Telegram ─────────────
  if (!_activeService && !_choosingService) {
    _choosingService = true;
    _updateServiceBadge();
    setStatus('🎤 Which service? Say "Email" or "Telegram"', 'idle');
    try {
      const res  = await fetch('/voice/service-greeting');
      const data = await res.json();
      $('responseText').textContent = data.response_text || '';
      // Sync the active voice language from the greeting response so the
      // Web Speech API watcher (used during TTS playback) also uses the
      // correct language immediately.
      if (data.voice_lang) _voiceLang = data.voice_lang;
      if (data.audio_url) {
        // TTS ends → _autoRestart → user speaks → processAndSend sends choosing_service=true
        playTTS(data.audio_url);
        return;
      }
    } catch (e) { /* network error — fall through and start mic directly */ }
  }

  await startRecording();
}

// ── Initialise AudioContext (called once from first user gesture) ─────────────
async function _ensureAudioCtx() {
  if (!micStream || micStream.getTracks().some(t => t.readyState === 'ended')) {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount:     1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl:  true,
        },
        video: false,
      });
    } catch (err) {
      setStatus('Mic access denied: ' + err.message, 'error');
      return false;
    }
  }

  if (!audioCtx) {
    audioCtx         = new (window.AudioContext || window.webkitAudioContext)();
    nativeSampleRate = audioCtx.sampleRate;
  }

  // Browsers can suspend the ctx; resume only works inside a user-gesture call
  // (this path is always triggered by a click on first call)
  if (audioCtx.state === 'suspended') {
    try { await audioCtx.resume(); } catch (_) {}
  }

  return true;
}

// ── Start ─────────────────────────────────────────────────────────────────────
async function startRecording() {
  if (isRecording) return;                           // already recording

  const ready = await _ensureAudioCtx();
  if (!ready) return;

  // AudioContext might have been left suspended by the browser after TTS.
  // Unlike creation, resume() IS permitted outside a user-gesture once the
  // context was originally created inside one.
  if (audioCtx.state === 'suspended') {
    try {
      await audioCtx.resume();
    } catch (_) {}
    // If still suspended we need a real user gesture — show a prompt and retry
    if (audioCtx.state === 'suspended') {
      setStatus('Tap 🎤 to reactivate microphone', 'idle');
      return;
    }
  }

  pcmBuffers = [];
  _hadSpeech  = false;
  if (_silenceTimer) { clearTimeout(_silenceTimer); _silenceTimer = null; }

  // Fresh ScriptProcessor each recording (they can't be reused across recordings)
  if (scriptProcessor) { scriptProcessor.disconnect(); scriptProcessor = null; }
  if (micSource)       { micSource.disconnect();       micSource = null; }

  micSource       = audioCtx.createMediaStreamSource(micStream);
  scriptProcessor = audioCtx.createScriptProcessor(BUFFER_SIZE, 1, 1);

  scriptProcessor.onaudioprocess = event => {
    if (!isRecording) return;
    const samples = event.inputBuffer.getChannelData(0);
    pcmBuffers.push(new Float32Array(samples));

    // ── Silence gate ──────────────────────────────────────────────────────────
    // Compute RMS of this buffer chunk
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length);

    if (rms > SILENCE_THRESHOLD_RMS) {
      // Voiced frame — cancel any pending silence timer
      _hadSpeech = true;
      if (_silenceTimer) { clearTimeout(_silenceTimer); _silenceTimer = null; }
    } else if (_hadSpeech) {
      // Silence after speech — start the auto-stop countdown (if not already running)
      if (!_silenceTimer) {
        _silenceTimer = setTimeout(() => {
          _silenceTimer = null;
          if (isRecording) stopRecording();
        }, SILENCE_STOP_MS);
      }
    }
  };

  micSource.connect(scriptProcessor);
  scriptProcessor.connect(audioCtx.destination);

  isRecording   = true;
  setRecordingUI(true);
  // Use a longer window when the user just heard an email chunk or summary
  const _emailReadIntents = new Set(['read_email','next_email','prev_email','read_more','list_emails','summarize_email']);
  const listenSecs = _emailReadIntents.has(_lastIntent) ? EMAIL_LISTEN_SECS : MAX_RECORD_SECONDS;
  recordTimeout = setTimeout(stopRecording, listenSecs * 1000);
}

// ── Stop ──────────────────────────────────────────────────────────────────────
function stopRecording() {
  if (!isRecording) return;
  clearTimeout(recordTimeout);
  if (_silenceTimer) { clearTimeout(_silenceTimer); _silenceTimer = null; }
  isRecording = false;
  setRecordingUI(false);

  // Disconnect graph but keep audioCtx and micStream alive — the OS mic
  // indicator stays on and auto-restart after TTS works without a user gesture.
  if (scriptProcessor) { scriptProcessor.disconnect(); scriptProcessor = null; }
  if (micSource)       { micSource.disconnect();       micSource = null; }

  processAndSend(pcmBuffers);
}

// ── Merge → resample → WAV → POST ─────────────────────────────────────────────
// ── Helper: restart mic after a short grace period ──────────────────────────
// Called at the end of every non-interactive path so the mic is always-on.
function _autoRestart(delayMs = 600) {
  setTimeout(() => { if (!isRecording && !isSpeaking) startRecording(); }, delayMs);
}

async function processAndSend(buffers) {
  // Recording timed out with no audio — just restart silently
  if (!buffers.length) {
    setStatus('🎤 Listening…', 'idle');
    _autoRestart(300);
    return;
  }
  setStatus('Processing…', 'processing');

  // Merge all chunks
  const totalLen = buffers.reduce((n, b) => n + b.length, 0);
  const merged   = new Float32Array(totalLen);
  let off = 0;
  for (const b of buffers) { merged.set(b, off); off += b.length; }

  // Resample to 16 kHz
  const resampled = resample(merged, nativeSampleRate, TARGET_SAMPLE_RATE);

  // Encode as PCM-16 WAV
  const wavBlob = toWavBlob(resampled, TARGET_SAMPLE_RATE);

  // Send
  const form = new FormData();
  form.append('audio', wavBlob, 'recording.wav');
  if (_choosingService) form.append('choosing_service', 'true');
  // Always send the current UI language so the server uses it for STT + TTS
  // even if the Flask session cookie hasn't been refreshed yet.
  form.append('lang', localStorage.getItem('voicemail_lang') || 'en');

  try {
    const res  = await fetch('/voice/process', { method: 'POST', body: form });
    const data = await res.json();

    if (!res.ok) {
      setStatus('Server error: ' + (data.error || 'Unknown'), 'error');
      _autoRestart(1500);   // pause a bit so user can read the error, then retry
      return;
    }

    $('transcriptionText').textContent = data.transcription || '(nothing recognised)';
    $('responseText').textContent      = data.response_text || '';

    // ── Track last intent (used for dynamic listen-window) ────────────────────
    _lastIntent = data.intent || null;
    // ── Language switch: sync client-side language state ───────────────────────
    if (data.voice_lang) {
      _voiceLang = data.voice_lang;
      // Sync the dropdown (covers voice-command switch too)
      const sel = document.getElementById('langSelect');
      if (sel) sel.value = data.voice_lang;
    }
    // ── Service selection / switch result ────────────────────────────────
    if (data.intent === 'service_selected') {
      _activeService   = data.service || null;
      _choosingService = false;
      _updateServiceBadge();
      if (_activeService === 'telegram' && typeof switchTab === 'function') switchTab('messages');
      if (_activeService === 'email'    && typeof switchTab === 'function') switchTab('email');
    } else if (data.intent === 'switch_service') {
      _activeService   = null;
      _choosingService = false;
      _updateServiceBadge();
    } else if (data.intent === 'choosing_service') {
      // service word not recognised — stay in choosing mode, play TTS re-prompt
      _choosingService = true;
      _updateServiceBadge();
    }

    // ── Auto-switch to Messages tab for Telegram intents ─────────────────────
    if (['send_message','read_messages','cancel_message','summarize_message'].includes(data.intent)) {
      if (typeof switchTab === 'function') switchTab('messages');
    }
    // Auto-switch to Email tab for email intents (when switching back)
    if (['read_email','list_emails','send_email','summarize_email'].includes(data.intent)) {
      if (typeof switchTab === 'function') switchTab('email');
    }

    // ── Track email compose step ──────────────────────────────────────────────
    _emailStep = data.email_step || null;
    _updateTypeInBox();

    // ── Track Telegram message compose step ───────────────────────────────────
    _msgStep = data.msg_step || null;
    _updateMsgTypeInBox();

    // ── Handle special intents ────────────────────────────────────────────────
    if (data.intent === 'stop_reading') {
      // Instant cut — go idle, user taps when ready
      _stopSpeechWatcher();
      if (_ttsAudio) { _ttsAudio.pause(); _ttsAudio.src = ''; _ttsAudio = null; }
      _setSpeakingUI(false);
      setStatus('✋ Stopped · Tap 🎤 to continue', 'idle');
      return;   // intentional idle — do NOT auto-restart
    }

    if (data.intent === 'logout') {
      _releaseMic();
      if (data.audio_url) playTTS(data.audio_url);
      setTimeout(() => { window.location.href = '/'; }, 2500);
      return;
    }

    // ── Silence / nothing recognised — restart immediately & quietly ─────────
    if (!data.transcription && data.intent === 'unknown') {
      setStatus('🎤 Listening…', 'idle');
      _autoRestart(300);
      return;
    }

    // ── Show compose step label or generic done status ────────────────────────
    const emailStepLabels = {
      to:      '📧 E-mail Step 1/4 · Say the address — or type it below',
      subject: '📝 E-mail Step 2/4 · Say the subject (or type below)',
      body:    '💬 E-mail Step 3/4 · Say your message (or type below)',
      confirm: '✅ E-mail Step 4/4 · Say "yes" to send · "cancel" to abort',
    };
    const msgStepLabels2 = {
      to:         '👤 Telegram Step 1/4 · Say the contact name (or type below)',
      to_confirm: '❓ Telegram Step 2/4 · Say "yes" to confirm · or say the correct name',
      text:       '💬 Telegram Step 3/4 · Say your message (or type below)',
      confirm:    '✅ Telegram Step 4/4 · Say "yes" to send · "cancel" to abort',
    };
    const intentLabels = {
      summarize_email:   '📋 Summary ready · Say "next", "summarize email" or "send email"',
      summarize_message: '📋 Message summary ready · Say a command',
      read_email:        '📧 Email read · Say "next", "summarize email" or "read more"',
      list_emails:       '📬 Inbox listed · Say "read email 1" or "next"',
      read_messages:     '💬 Messages read · Say "summarize messages" or "send message"',
    };
    const activeStep   = _emailStep || _msgStep;
    const activeLabels = _emailStep ? emailStepLabels : (_msgStep ? msgStepLabels2 : {});
    const statusMsg    = activeStep
      ? (activeLabels[activeStep] || activeStep)
      : (intentLabels[data.intent] || ('Done • ' + (data.intent || '—')));
    setStatus(statusMsg, activeStep ? 'recording' : 'done');

    if (data.audio_url) {
      // playTTS → onended already calls _autoRestart, so we're covered
      playTTS(data.audio_url);
    } else {
      // No TTS for this response (pyttsx3 failed, or TTS skipped) — restart anyway
      _autoRestart(600);
    }

  } catch (err) {
    setStatus('Network error: ' + err.message, 'error');
    console.error(err);
    _autoRestart(1500);
  }
}

// ── Resample (linear interpolation) ──────────────────────────────────────────
function resample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const outLen = Math.round(samples.length * toRate / fromRate);
  const out    = new Float32Array(outLen);
  const ratio  = fromRate / toRate;
  for (let i = 0; i < outLen; i++) {
    const pos  = i * ratio;
    const idx  = Math.floor(pos);
    const frac = pos - idx;
    out[i] = (samples[idx] ?? 0) + frac * ((samples[idx + 1] ?? 0) - (samples[idx] ?? 0));
  }
  return out;
}

// ── PCM-16 WAV encoder ─────────────────────────────────────────────────────────
function toWavBlob(float32, sampleRate) {
  const numCh    = 1;
  const dataSize = float32.length * 2;
  const buf      = new ArrayBuffer(44 + dataSize);
  const view     = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };

  str(0,  'RIFF');  view.setUint32(4,  36 + dataSize,          true);
  str(8,  'WAVE');  str(12, 'fmt ');
  view.setUint32(16, 16,                    true);   // PCM chunk
  view.setUint16(20, 1,                     true);   // PCM format
  view.setUint16(22, numCh,                 true);
  view.setUint32(24, sampleRate,            true);
  view.setUint32(28, sampleRate * numCh * 2, true);  // byte rate
  view.setUint16(32, numCh * 2,             true);   // block align
  view.setUint16(34, 16,                    true);   // bits per sample
  str(36, 'data');  view.setUint32(40, dataSize, true);

  let o = 44;
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    o += 2;
  }
  return new Blob([buf], { type: 'audio/wav' });
}

// ── Words that should cut TTS the instant they're spoken ──────────────────────
// Mirrors server-side _STOP_EXACT + _CANCEL_EXACT, kept broad for the small model
const _INTERRUPT_WORDS = new Set([
  'stop','top','stock','shop','stuff','stoop','stored','sport','stomp',
  'quiet','quite','silence','silent','pause','paws','halt',
  'enough','shut','cancel','cancelled','council','console','abort','no','nope',
]);

// ── Instant stop-watcher using Web Speech API (no server roundtrip) ───────────
function _startSpeechWatcher() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;   // browser doesn't support it — user must tap ⏹ button

  _stopSpeechWatcher();   // kill any previous instance

  const recog = new SR();
  const _langMap = { 'en': 'en-US', 'hi': 'hi-IN', 'mr': 'mr-IN' };
  recog.lang            = _langMap[_voiceLang] || 'en-US';
  recog.continuous      = true;
  recog.interimResults  = true;   // fire on partial results for fastest response
  recog.maxAlternatives = 3;

  recog.onresult = (event) => {
    if (!isSpeaking) { recog.stop(); return; }
    for (let i = event.resultIndex; i < event.results.length; i++) {
      for (let j = 0; j < event.results[i].length; j++) {
        const heard = event.results[i][j].transcript.toLowerCase().trim();
        for (const word of heard.split(/\s+/)) {
          if (_INTERRUPT_WORDS.has(word)) {
            _stopSpeechWatcher();
            // Instant cut — no server trip, no TTS response
            if (_ttsAudio) { _ttsAudio.pause(); _ttsAudio.src = ''; _ttsAudio = null; }
            _setSpeakingUI(false);
            // Go idle — do NOT auto-start recording (would immediately capture
            // silence / the tail of "stop" and trigger "I did not hear anything")
            setStatus('✋ Stopped · Tap 🎤 to continue', 'idle');
            return;
          }
        }
      }
    }
  };

  recog.onerror = (e) => {
    // 'no-speech' is normal — just restart
    if (isSpeaking && e.error === 'no-speech') {
      setTimeout(() => { if (isSpeaking) _startSpeechWatcher(); }, 50);
    }
  };

  recog.onend = () => {
    // Auto-restart while TTS is still playing (browser stops after a few secs)
    if (isSpeaking && _wsRecog === recog) {
      setTimeout(() => { if (isSpeaking) _startSpeechWatcher(); }, 50);
    }
  };

  try { recog.start(); _wsRecog = recog; }
  catch (e) { console.warn('SpeechRecognition start error:', e); }
}

function _stopSpeechWatcher() {
  if (_wsRecog) { try { _wsRecog.stop(); } catch (_) {} _wsRecog = null; }
}

// ── TTS playback ──────────────────────────────────────────────────────────────
function playTTS(url) {
  if (_ttsAudio) { _ttsAudio.pause(); _ttsAudio.src = ''; _ttsAudio = null; }

  const a = $('ttsAudio');
  a.src = url + '?t=' + Date.now();
  _ttsAudio = a;

  _setSpeakingUI(true);

  a.onended = () => {
    _ttsAudio = null;
    _stopSpeechWatcher();
    _setSpeakingUI(false);
    // Show a contextual hint after TTS playback
    const _emailReadIntents = new Set(['read_email','next_email','prev_email','read_more','list_emails','summarize_email']);
    const _msgIntents       = new Set(['send_message','read_messages','cancel_message','summarize_message']);
    if (_emailReadIntents.has(_lastIntent)) {
      setStatus('🎤 Say "summarize email", "read more", "next", "previous" or "stop"', 'idle');
    } else if (_msgIntents.has(_lastIntent)) {
      setStatus('🎤 Say "send message", "read messages" or "summarize messages"', 'idle');
    } else {
      setStatus('🎤 Listening…', 'idle');
    }
    _autoRestart(500);
  };
  a.onerror = () => {
    _ttsAudio = null;
    _stopSpeechWatcher();
    _setSpeakingUI(false);
    setStatus('🎤 Listening…', 'idle');
    _autoRestart(500);
  };

  a.play()
    .then(() => {
      // Start instant stop-watcher the moment audio begins
      _startSpeechWatcher();
    })
    .catch(e => {
      console.warn('TTS play error:', e);
      _setSpeakingUI(false);
    });
}

// Called by ⏹ button or toggleRecording() while speaking
function stopSpeaking() {
  _stopSpeechWatcher();
  if (_ttsAudio) { _ttsAudio.pause(); _ttsAudio.src = ''; _ttsAudio = null; }
  _setSpeakingUI(false);
  // Go idle — user taps mic when ready for next command
  setStatus('✋ Stopped · Tap 🎤 to continue', 'idle');
}

// Call this only on explicit logout to release the OS mic indicator
function _releaseMic() {
  _stopSpeechWatcher();
  if (scriptProcessor) { scriptProcessor.disconnect(); scriptProcessor = null; }
  if (micSource)       { micSource.disconnect();       micSource = null; }
  if (micStream)       { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (audioCtx)        { audioCtx.close();             audioCtx = null; }
}

// ── Speaking UI helper ────────────────────────────────────────────────────────
function _setSpeakingUI(speaking) {
  isSpeaking = speaking;
  const micBtn  = $('micBtn');
  const stopBtn = $('stopBtn');
  const sr1     = $('speakRing1');
  const sr2     = $('speakRing2');

  if (speaking) {
    // Mic stays visible but dimmed — user can click it to interrupt + record
    micBtn.classList.add('opacity-50', 'scale-90');
    stopBtn.classList.remove('hidden');
    sr1 && sr1.classList.remove('hidden');
    sr2 && sr2.classList.remove('hidden');
    setStatus('🔊 Speaking… say "stop" or tap 🎤', 'processing');
  } else {
    micBtn.classList.remove('opacity-50', 'scale-90');
    stopBtn.classList.add('hidden');
    sr1 && sr1.classList.add('hidden');
    sr2 && sr2.classList.add('hidden');
  }
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function setRecordingUI(on) {
  const btn = $('micBtn');
  if (on) {
    btn.classList.add('recording');
    btn.innerHTML = '⏹';
    $('ring1').classList.remove('hidden');
    $('ring2').classList.remove('hidden');
    setStatus(`Recording… (auto-stops in ${MAX_RECORD_SECONDS}s)`, 'recording');
  } else {
    btn.classList.remove('recording');
    btn.innerHTML = '🎤';
    $('ring1').classList.add('hidden');
    $('ring2').classList.add('hidden');
  }
}

function setStatus(msg, type = 'idle') {
  const colors = { idle:'text-gray-400', recording:'text-red-400', processing:'text-yellow-400', done:'text-green-400', error:'text-red-500' };
  const el = $('statusText');
  el.className  = 'text-center text-sm mt-2 ' + (colors[type] || colors.idle);
  el.textContent = msg;
}

// ── Type-in fallback for voice compose ────────────────────────────────────────
function _updateTypeInBox() {
  const box   = $('typeInBox');
  const input = $('typeInInput');
  if (!box) return;
  if (_emailStep) {
    const placeholders = {
      to:      'recipient@gmail.com',
      subject: 'Email subject…',
      body:    'Your message…',
      confirm: 'Type YES to confirm or NO to cancel',
    };
    input.placeholder = placeholders[_emailStep] || '';
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
    input.value = '';
  }
}
function _updateMsgTypeInBox() {
  const box   = $('msgTypeInBox');
  const input = $('msgTypeInInput');
  if (!box) return;
  if (_msgStep) {
    const placeholders = {
      to:         'Contact name (e.g. Vaibhav)',
      to_confirm: 'Type YES to confirm or type a different name',
      text:       'Your message\u2026',
      confirm:    'Type YES to send or NO to cancel',
    };
    input.placeholder = placeholders[_msgStep] || '';
    box.classList.remove('hidden');
  } else {
    box.classList.add('hidden');
    input.value = '';
  }
}
async function submitTypeIn() {
  const input = $('typeInInput');
  const value = (input.value || '').trim();
  if (!value) return;

  const field = _emailStep;
  if (!field) return;

  // Confirm step: handle yes/no locally — no server call needed for "cancel"
  if (field === 'confirm') {
    if (/^(no|n|cancel|abort|stop|quit)/i.test(value)) {
      _emailStep = null;
      _updateTypeInBox();
      setStatus('Email cancelled · Ready to listen', 'idle');
      _autoRestart(300);
      return;
    }
    if (!/^(yes|y|confirm|ok|okay|send|go)/i.test(value)) {
      $('responseText').textContent = 'Type YES to send or NO to cancel.';
      return;
    }
  }

  setStatus('Processing…', 'processing');
  $('typeInBox').classList.add('hidden');

  try {
    const res  = await fetch('/voice/compose-text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, value }),
    });
    const data = await res.json();
    _handleComposeResponse(data);
  } catch (err) {
    setStatus('Network error: ' + err.message, 'error');
    _autoRestart(1500);
  }
}

function dismissTypeIn() {
  $('typeInBox').classList.add('hidden');
  $('typeInInput').value = '';
}

async function submitMsgTypeIn() {
  const input = $('msgTypeInInput');
  const value = (input.value || '').trim();
  if (!value) return;

  const field = _msgStep;
  if (!field) return;

  // to_confirm step: handle locally for the "no" / re-enter case
  // ("yes" sends to server normally)
  if (field === 'confirm') {
    if (/^(no|n|cancel|abort|stop|quit)/i.test(value)) {
      _msgStep = null;
      _updateMsgTypeInBox();
      setStatus('Telegram message cancelled \u00B7 Ready to listen', 'idle');
      _autoRestart(300);
      return;
    }
    if (!/^(yes|y|confirm|ok|okay|send|go)/i.test(value)) {
      $('responseText').textContent = 'Type YES to send or NO to cancel.';
      return;
    }
  }

  setStatus('Processing\u2026', 'processing');
  $('msgTypeInBox').classList.add('hidden');

  try {
    const res  = await fetch('/voice/msg-compose-text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, value }),
    });
    const data = await res.json();
    $('transcriptionText').textContent = data.transcription || '(typed)';
    $('responseText').textContent      = data.response_text || '';
    _msgStep = data.msg_step || null;
    _updateMsgTypeInBox();
    const msgStepLabels = {
      to:         '\uD83D\uDC64 Telegram Step 1/4 \u00B7 Contact name (or type below)',
      to_confirm: '\u2753 Telegram Step 2/4 \u00B7 Say "yes" to confirm \u00B7 or type the correct name',
      text:       '\uD83D\uDCAC Telegram Step 3/4 \u00B7 Your message (or type below)',
      confirm:    '\u2705 Telegram Step 4/4 \u00B7 Say "yes" to send \u00B7 "cancel" to abort',
    };
    const statusMsg = _msgStep ? (msgStepLabels[_msgStep] || _msgStep) : ('Done \u2022 send_message');
    setStatus(statusMsg, _msgStep ? 'recording' : 'done');
    if (data.audio_url) playTTS(data.audio_url);
    else _autoRestart(600);
  } catch (err) {
    setStatus('Network error: ' + err.message, 'error');
    _autoRestart(1500);
  }
}

function dismissMsgTypeIn() {
  $('msgTypeInBox').classList.add('hidden');
  $('msgTypeInInput').value = '';
}

// Shared handler for both voice and text-input compose responses
function _handleComposeResponse(data) {
  $('transcriptionText').textContent = data.transcription || '(typed)';
  $('responseText').textContent      = data.response_text || '';
  _emailStep = data.email_step || null;
  _updateTypeInBox();
  _msgStep = data.msg_step || null;
  _updateMsgTypeInBox();

  const emailStepLabels3 = {
    to:      '📧 E-mail Step 1/4 · Say the address — or type it below',
    subject: '📝 E-mail Step 2/4 · Say the subject (or type below)',
    body:    '💬 E-mail Step 3/4 · Say your message (or type below)',
    confirm: '✅ E-mail Step 4/4 · Say "yes" to send · "cancel" to abort',
  };
  const msgStepLabels3 = {
    to:         '👤 Telegram Step 1/4 · Contact name (or type below)',
    to_confirm: '❓ Telegram Step 2/4 · Say "yes" to confirm · or type the correct name',
    text:       '💬 Telegram Step 3/4 · Your message (or type below)',
    confirm:    '✅ Telegram Step 4/4 · Say "yes" to send · "cancel" to abort',
  };
  const activeStep3   = _emailStep || _msgStep;
  const activeLabels3 = _emailStep ? emailStepLabels3 : (_msgStep ? msgStepLabels3 : {});
  const statusMsg     = activeStep3
    ? (activeLabels3[activeStep3] || activeStep3)
    : ('Done • ' + (data.intent || '—'));
  setStatus(statusMsg, activeStep3 ? 'recording' : 'done');

  if (data.audio_url) {
    playTTS(data.audio_url);
  } else {
    _autoRestart(600);
  }
}

// ── Service badge ───────────────────────────────────────────────────────────────
function _updateServiceBadge() {
  const badge     = $('serviceBadge');
  const switchBtn = $('switchServiceBtn');
  if (!badge) return;
  const svc = window._svcI18n || {};
  if (!_activeService) {
    badge.textContent = _choosingService
      ? (svc.svcWaiting   || 'Waiting for choice…')
      : (svc.svcTapMic    || '🤖 Tap mic to start');
    badge.className   = 'px-3 py-1 rounded-full text-xs font-semibold bg-white/10 text-gray-400 text-center block';
    if (switchBtn) switchBtn.classList.add('hidden');
  } else if (_activeService === 'email') {
    badge.textContent = svc.svcEmail    || '📧 Email Mode';
    badge.className   = 'px-3 py-1 rounded-full text-xs font-semibold bg-purple-500/30 text-purple-300 text-center block';
    if (switchBtn) { switchBtn.textContent = svc.svcSwitch || '↺ Switch service'; switchBtn.classList.remove('hidden'); }
  } else {
    badge.textContent = svc.svcTelegram || '💬 Telegram Mode';
    badge.className   = 'px-3 py-1 rounded-full text-xs font-semibold bg-green-500/30 text-green-300 text-center block';
    if (switchBtn) { switchBtn.textContent = svc.svcSwitch || '↺ Switch service'; switchBtn.classList.remove('hidden'); }
  }
}

function resetService() {
  _activeService   = null;
  _choosingService = false;
  _updateServiceBadge();
  setStatus('Tap 🎤 to choose a service', 'idle');
  const svc = window._svcI18n || {};
  $('responseText').textContent = svc.svcReset || 'Service reset. Tap the mic and say Email or Telegram.';
}

// ── Initialise badge on page load ────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', _updateServiceBadge);

// Release mic tracks when page closes / navigates away
window.addEventListener('beforeunload', _releaseMic);