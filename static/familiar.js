/* ===== The familiar — ears, voice, and state machine =====
 *
 * States: asleep -> idle -> listening -> thinking -> speaking -> idle
 *
 * The avatar is driven entirely by  document.body.dataset.state , so a future
 * body (Pi + servos) only needs to receive the same five state strings.
 */

const CONFIG = {
  // name, wakeVariants and lang are overwritten at load by GET /api/config
  // (from the .env FAMILIAR_* vars). These are only fallbacks if that fails.
  name: "Familiar",
  // How speech recognition tends to (mis)hear the name. The server supplies the
  // real list (FAMILIAR_WAKE_WORDS); add variants there as you discover them —
  // watch the browser console, every transcript is logged.
  wakeVariants: ["familiar"],
  lang: "en-GB",
  // First matching substring wins when choosing a TTS voice.
  voicePrefs: ["Daniel", "Google UK English Male", "Serena", "en-GB", "en_GB"],
  rate: 1.02,
  pitch: 0.85,
  // Server-voice loudness. An <audio> element maxes out at the device volume,
  // so we run it through a WebAudio gain (>1) + limiter to make the familiar audible.
  gain: 3.0,
  listenTimeoutMs: 9000,   // max time to wait for a question after the wake word
  silenceMs: 1600,         // pause that ends a question (while still mid-phrase)
  finalGraceMs: 900,       // shorter wait once the engine has end-pointed a phrase
  ambientWordThreshold: 500, // idle-state words buffered before offloading to /api/ambient
  id: "",                    // the bound familiar's id, from /api/config
  shell: "cat",              // appearance: /shells/<shell>/shell.svg + shell.css
  sounds: "cat",             // creature noises: /api/sounds serves this pack
};

// User-tunable voice preferences, persisted per device.
// serverVoice: prefer the familiar's own server voice; falls back to the browser
// voice automatically if the server has none configured or the call fails.
const PREFS_KEY = "familiar-voice";
const prefs = Object.assign(
  { voiceName: null, rate: CONFIG.rate, pitch: CONFIG.pitch, serverVoice: true, gain: CONFIG.gain, meow: true, meowGain: 1, ambient: false },
  JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"),
);
function savePrefs() { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); }

// A scrap of silence used to unlock HTML5 audio inside the wake-up tap gesture,
// so iOS lets us play the server voice later (it blocks un-gestured playback).
const SILENT_WAV =
  "data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAA" +
  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
// One reusable, gesture-unlocked <audio> element for all server-voice playback.
const ttsAudio = new Audio();
ttsAudio.setAttribute("playsinline", "");

/* ---------------- creature noises ---------------- */
// Recorded noises for whatever this familiar is -- meows for a cat, something
// else for anything else. The pack is chosen by the bound familiar and served
// from /api/sounds; the clips themselves live in /sounds/<pack>/. Toggled with
// prefs.meow: one plays when the familiar starts thinking, another when it
// finishes answering. A separate element so a noise never disturbs the TTS
// pipeline.
const meowAudio = new Audio();
meowAudio.setAttribute("playsinline", "");

let soundUrls = [];
let lastMeowIdx = -1;

async function loadSounds() {
  try {
    const res = await fetch(api("/sounds"));
    soundUrls = res.ok ? (await res.json()).sounds || [] : [];
  } catch (_) {
    soundUrls = [];  // no list, no noises — everything else works as before
  }
  lastMeowIdx = -1;
}

function playRandomMeow() {
  if (prefs.meow === false || !soundUrls.length) return;
  let idx = Math.floor(Math.random() * soundUrls.length);
  if (soundUrls.length > 1 && idx === lastMeowIdx) idx = (idx + 1) % soundUrls.length;
  lastMeowIdx = idx;
  ensureMeowGain();  // route through the meow volume stage (built from a gesture)
  if (meowGain) meowGain.gain.value = prefs.meowGain;
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  meowAudio.src = soundUrls[idx];
  meowAudio.play().catch(() => {});  // locked/blocked audio: skip silently
}

function stopMeow() {
  try { meowAudio.pause(); } catch (_) {}
}

const statusEl = document.getElementById("status");
const recordingIndicatorEl = document.getElementById("recording-indicator");
const bubbleEl = document.getElementById("bubble");
const bubbleQ = document.getElementById("bubble-question");
const bubbleA = document.getElementById("bubble-answer");
const sleepBtn = document.getElementById("sleep-btn");
const recapBtn = document.getElementById("recap-btn");

let state = "asleep";
let recognition = null;
let recognitionActive = false;
let questionBuffer = "";
let ambientBuffer = "";
let silenceTimer = null;
let listenTimer = null;
let chosenVoice = null;

recordingIndicatorEl.hidden = !prefs.ambient;

/* ---------------- state machine ---------------- */

function setState(next, { broadcast = true } = {}) {
  const prev = state;
  state = next;
  document.body.dataset.state = next;
  // Recorded meows on locally-driven transitions only (remote mirrors pass
  // broadcast:false): one as it starts pondering, one as the answer wraps up.
  if (broadcast) {
    if (next === "thinking") playRandomMeow();
    else if (next === "speaking") stopMeow();  // never talk over a meow
    else if (prev === "speaking" && next === "idle") playRandomMeow();
  }
  const labels = {
    asleep: `Tap ${CONFIG.name} to wake`,
    idle: `Waiting for someone to say “${CONFIG.name}…”`,
    listening: "Listening…",
    thinking: "Hmm…",
    speaking: "",
  };
  statusEl.textContent = labels[next] ?? "";
  sleepBtn.hidden = next === "asleep";
  // Recap speaks, so it is only offered to a creature that is awake. The
  // handler still refuses when asleep, because the bus can move the state
  // under us between the click and the check.
  recapBtn.hidden = next === "asleep";
  // Tell the other bodies (a second screen, the puppet) so they follow along.
  // Remote-applied states pass broadcast:false to avoid an echo loop.
  if (broadcast) sendState(next);
}

/* ---------------- body bus (state to other bodies) ---------------- */
// A WebSocket to /t/<slug>/ws/state carries the five states to every body
// connected to *this table*. Other tables have their own bus.
// This screen usually drives (it has the mic); others mirror. The wire format
// is an extensible JSON envelope — {type:"state", state} today, room for
// {type:"viseme", level} to drive a jaw servo later.
let bodySocket = null;

function connectBodyBus() {
  const ws = new WebSocket(busUrl());   // this table's bus -- see table.js
  bodySocket = ws;
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    // Mirror a state chosen by another body, without rebroadcasting it back.
    if (msg.type === "state" && msg.state !== state) {
      setState(msg.state, { broadcast: false });
    }
  };
  // Reconnect with a small backoff so the puppet/screen self-heals if the
  // server restarts or WiFi blips.
  ws.onclose = () => { bodySocket = null; setTimeout(connectBodyBus, 2000); };
  ws.onerror = () => ws.close();
}

function sendState(next) {
  if (bodySocket && bodySocket.readyState === WebSocket.OPEN) {
    bodySocket.send(JSON.stringify({ type: "state", state: next }));
  }
}

// Jaw amplitude for the puppet, emitted while this screen plays TTS audio.
function sendViseme(level) {
  if (bodySocket && bodySocket.readyState === WebSocket.OPEN) {
    bodySocket.send(JSON.stringify({ type: "viseme", level }));
  }
}

connectBodyBus();

/* ---------------- identity (name from the server) ---------------- */
// The same code runs as anyone's familiar. Everything that makes this one
// *this* one -- name, wake-words, language, appearance and creature noises --
// comes from GET /api/config, which serves the bound familiar's document. The
// static markup ships with generic placeholders; applyFamiliar() personalises
// them once config arrives. Built-in CONFIG values are the fallback if the
// fetch fails.

let knownFamiliars = [];   // the switcher's menu: [{id, name, shell}]

async function loadConfig() {
  try {
    // tableConfig() (table.js) rather than a fetch of our own: the navigation
    // drawer wants the same document, and one identity document fetched twice
    // is where two halves of a page start disagreeing about who is bound.
    const cfg = await tableConfig();
    if (cfg.id) CONFIG.id = cfg.id;
    if (cfg.name) CONFIG.name = cfg.name;
    if (Array.isArray(cfg.wakeVariants) && cfg.wakeVariants.length) {
      CONFIG.wakeVariants = cfg.wakeVariants.map((w) => w.toLowerCase());
    }
    if (cfg.lang) CONFIG.lang = cfg.lang;
    if (cfg.shell) CONFIG.shell = cfg.shell;
    if (cfg.sounds) CONFIG.sounds = cfg.sounds;
    // The server decides the ambient buffer size; the browser used to keep
    // its own copy of the number and they could drift apart.
    if (cfg.ambientWordThreshold > 0) CONFIG.ambientWordThreshold = cfg.ambientWordThreshold;
    knownFamiliars = Array.isArray(cfg.familiars) ? cfg.familiars : [];
  } catch (_) { /* keep the built-in defaults */ }
  await applyFamiliar();
}

// The shell contract: /shells/<id>/shell.svg is an <svg id="familiar"> whose
// outline shapes carry class="edge", and /shells/<id>/shell.css animates it
// off body[data-state]. Nothing else about a shell is the app's business --
// which is why swapping one is two fetches rather than a rewrite of this file.
//
// The edge clause is the page's, not the creature's: familiar.css strokes
// those shapes in the daylight theme, where a pale familiar would otherwise
// have no outline against parchment. A shell that omits it still renders, and
// still renders correctly on the dark table.
let loadedShell = null;

async function loadShell(id) {
  if (id === loadedShell) return;
  const link = document.getElementById("shell-css");
  const mount = document.getElementById("shell");
  if (!mount) return;
  try {
    const res = await fetch(`/shells/${encodeURIComponent(id)}/shell.svg`);
    if (!res.ok) throw new Error(`shell ${id}: ${res.status}`);
    const svg = await res.text();
    if (link) link.href = `/shells/${encodeURIComponent(id)}/shell.css`;
    mount.innerHTML = svg;
    loadedShell = id;
  } catch (err) {
    // A missing shell must not leave a blank screen with no way to wake the
    // familiar, so fall back to the one shell that always ships.
    console.warn("shell failed to load", err);
    if (id !== "cat") await loadShell("cat");
  }
}

async function applyFamiliar() {
  document.title = CONFIG.name;
  await loadShell(CONFIG.shell);
  const svg = document.getElementById("familiar");
  if (svg) svg.setAttribute("aria-label", `${CONFIG.name} the familiar`);
  // Not "<name>'s voice" any more: the panel has held the bound familiar, the
  // recording toggle and this device's own audio for a while now, and a title
  // naming only one of them sends people looking in the wrong place.
  const title = document.getElementById("settings-title");
  if (title) title.textContent = "Settings";
  const svLabel = document.getElementById("server-voice-label");
  if (svLabel) svLabel.textContent = `Use ${CONFIG.name}'s own voice (falls back to a device voice below)`;
  await loadSounds();
  renderFamiliarPicker();
  // Re-render the status line (e.g. the asleep prompt) with the resolved name.
  setState(state, { broadcast: false });
}

// Switch the table to a different familiar. The server clears the rolling
// history for us -- a new creature shouldn't continue its predecessor's
// conversation -- so the screen just re-reads its identity and goes to sleep.
async function switchFamiliar(id) {
  const res = await fetch(api("/familiar"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "switch failed");
  goToSleep();
  forgetTableConfig();     // a different creature is bound; the cached one is stale
  await loadConfig();
  renderTableNav();        // and the drawer names it
}

loadConfig();

/* ---------------- speech recognition (ears) ---------------- */

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

function startRecognition() {
  if (!SpeechRecognition) {
    statusEl.textContent = "This browser has no speech recognition — try Safari or Chrome.";
    return;
  }
  if (recognitionActive) return;

  recognition = new SpeechRecognition();
  recognition.lang = CONFIG.lang;
  recognition.continuous = true;
  recognition.interimResults = true;

  recognition.onstart = () => { recognitionActive = true; };

  recognition.onend = () => {
    recognitionActive = false;
    // Recognition stops by itself all the time (especially on iOS).
    // Restart unless we deliberately stopped it.
    if (state === "idle" || state === "listening") {
      setTimeout(() => { try { recognition.start(); } catch (_) {} }, 250);
    }
  };

  recognition.onerror = (e) => {
    console.warn("recognition error:", e.error);
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      setState("asleep");
      statusEl.textContent = `Microphone blocked — allow mic access, then tap ${CONFIG.name}.`;
    }
  };

  recognition.onresult = (event) => {
    let interim = "", finals = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const text = event.results[i][0].transcript;
      if (event.results[i].isFinal) finals += text;
      else interim += text;
    }
    const heard = (finals || interim).trim();
    if (heard) console.log(`heard (${finals ? "final" : "interim"}):`, heard);

    if (state === "idle") {
      const wake = findWakeWord(finals || interim);
      if (wake) {
        // If the question came in the same breath (the name, then "what is a
        // grapple check?") use it straight away.
        if (finals && wake.after.split(/\s+/).filter(Boolean).length >= 2) {
          submitQuestion(wake.after);
        } else {
          beginListening(wake.after);
        }
      } else if (prefs.ambient && finals) {
        bufferAmbient(finals);
      }
    } else if (state === "listening") {
      if (finals) questionBuffer += " " + finals;
      // A final result means the engine's own end-pointer fired — wait only a
      // short grace (in case more is coming) instead of the full silence pause.
      resetSilenceTimer(finals ? CONFIG.finalGraceMs : CONFIG.silenceMs);
    }
  };

  try { recognition.start(); } catch (_) {}
}

function stopRecognition() {
  if (recognition) {
    recognition.onend = null;
    try { recognition.stop(); } catch (_) {}
    recognitionActive = false;
  }
}

function findWakeWord(text) {
  const lower = text.toLowerCase();
  for (const variant of CONFIG.wakeVariants) {
    const idx = lower.lastIndexOf(variant);
    if (idx !== -1) {
      return { after: text.slice(idx + variant.length).replace(/^[\s,.!?]+/, "") };
    }
  }
  return null;
}

/* ---------------- ambient session capture (opt-in) ---------------- */
// While prefs.ambient is on, idle-state speech that isn't the wake word is
// batched and periodically offloaded to /api/ambient, which the server
// distills into game-context.md so Whiskers carries a sense of what's going
// on in the game forward. Off by default — see the settings-panel toggle.

function bufferAmbient(text) {
  ambientBuffer = `${ambientBuffer} ${text}`.trim();
  if (ambientBuffer.split(/\s+/).filter(Boolean).length >= CONFIG.ambientWordThreshold) {
    flushAmbient();
  }
}

function flushAmbient() {
  const text = ambientBuffer;
  ambientBuffer = "";
  if (!text) return;
  fetch(api("/ambient"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).catch(() => {}); // best-effort; never disturb the main flow
}

function beginListening(prefix) {
  questionBuffer = prefix || "";
  setState("listening");
  chirp();
  resetSilenceTimer();
  clearTimeout(listenTimer);
  listenTimer = setTimeout(finishListening, CONFIG.listenTimeoutMs);
}

function resetSilenceTimer(ms = CONFIG.silenceMs) {
  clearTimeout(silenceTimer);
  silenceTimer = setTimeout(finishListening, ms);
}

function finishListening() {
  clearTimeout(silenceTimer);
  clearTimeout(listenTimer);
  if (state !== "listening") return;
  const question = questionBuffer.trim();
  if (question.split(/\s+/).filter(Boolean).length >= 2) {
    submitQuestion(question);
  } else {
    setState("idle"); // heard its name, then nothing — go back to waiting
  }
}

/* ---------------- asking the brain ---------------- */

async function submitQuestion(question) {
  clearTimeout(silenceTimer);
  clearTimeout(listenTimer);
  stopRecognition(); // free the mic; don't listen to ourselves
  setState("thinking");
  bubbleEl.hidden = false;
  bubbleQ.textContent = `“${question}”`;
  bubbleA.textContent = "…";

  // Tear down any leftover playback (e.g. a running voice test) and capture
  // the generation this answer belongs to.
  speechSynthesis.cancel();
  stopAudio();
  const gen = speakGeneration;

  // Streamed answer first: text appears as it's written and the first
  // sentence goes to TTS before the rest exists. If the stream can't even
  // start (old server, proxy trouble), fall back to plain /api/ask.
  try {
    await submitQuestionStreaming(question, gen);
    return;
  } catch (err) {
    if (gen !== speakGeneration) return; // interrupted, not broken
    console.warn("stream path failed — falling back to /api/ask:", err);
  }

  try {
    const res = await fetch(api("/ask"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    speak(data.answer);
  } catch (err) {
    console.error(err);
    speak("My connection to the weave has frayed. Ask me again in a moment.");
    bubbleA.textContent = `(error: ${err.message})`;
  }
}

// Ask via /api/ask-stream (SSE). Text appears in the bubble as it's generated,
// and — the key latency win — playback of each sentence starts the instant its
// audio is ready, overlapping with the rest of the LLM still generating, rather
// than waiting for the whole answer to finish. Throws only if the stream failed
// before producing any text — then submitQuestion retries via /api/ask.
async function submitQuestionStreaming(question, gen) {
  const controller = new AbortController();
  const res = await fetch(api("/ask-stream"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal: controller.signal,
  });
  if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);

  const t0 = performance.now();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", text = "", errorMsg = null;

  // ── incremental TTS pipeline ────────────────────────────────────────────
  // Producer (the read loop) turns complete sentences into speech chunks as
  // they arrive; consumer (playChunks) plays them in order, starting on the
  // first ready blob. TTS fetches run at most ~2 ahead of the play cursor.
  const serverVoice = prefs.serverVoice !== false;
  const chunks = [];          // { text, fetch|null }, in speaking order
  let chunkSignal = null;     // resolve() for a consumer waiting on more chunks
  let streamDone = false;
  let chunkedUpto = 0;        // chars of `text` already turned into chunks
  let firstPushed = false;
  let groupBuf = "";          // complete sentences after the first, awaiting a group
  let playCursor = 0;         // chunk the consumer is on, so the producer knows
                              // whether a newly-arrived chunk is worth fetching yet

  // How far ahead of the play cursor to synthesise. Two, not more: the server
  // holds one model behind a lock, so requests queue anyway, and issuing ten of
  // them for a long answer just parks ten threads and wastes work when somebody
  // interrupts.
  const PREFETCH_AHEAD = 2;

  // Chars of speech to gather per chunk, growing. The first chunk is not in
  // this table -- it exists to make the first sound arrive quickly, so it is
  // the first sentence, topped up to TTS_FIRST_MIN if that sentence is tiny
  // (see drain).
  //
  // Why a ramp instead of one size: a chunk's audio has to cover the *next*
  // chunk's synthesis, and synthesis runs at ~2.6x realtime (measured, all
  // sizes). So a chunk may be up to ~2.6x its predecessor and still leave no
  // gap. A flat 200 broke that at exactly one place -- the join everyone hears
  // -- because one short opening sentence (~2.6s of audio) had to cover a
  // 200-char synthesis (~4.5s), and could not. Hence ~1.9s of silence between
  // the first and second utterance, every single time.
  const GROUP_CHARS = [110, 260, 420];
  const groupTarget = () =>
    GROUP_CHARS[Math.min(Math.max(chunks.length - 1, 0), GROUP_CHARS.length - 1)];

  const wake = () => { if (chunkSignal) { chunkSignal(); chunkSignal = null; } };

  const ensureFetch = (i) => {
    if (serverVoice && i < chunks.length && !chunks[i].fetch) {
      chunks[i].fetch = fetchTtsSafe(chunks[i].text);
    }
  };
  const pushChunk = (s) => {
    s = s.trim();
    if (!s) return;
    chunks.push({ text: s, fetch: null });
    // Synthesise the moment the text exists, rather than when playback reaches
    // it. The consumer's own ensureFetch(i + 1) looks like it does this, but it
    // runs while chunk i+1 is usually still being streamed out of the model, so
    // it is a no-op, and nothing tries again when the chunk actually arrives.
    // The fetch therefore started only after the previous chunk had finished
    // playing, which put a full synthesis into every join.
    if (chunks.length - 1 <= playCursor + PREFETCH_AHEAD) ensureFetch(chunks.length - 1);
    wake();
  };

  // Carve complete sentences out of the not-yet-chunked tail: the first chunk
  // as soon as it reaches TTS_FIRST_MIN (fast start), then growing groups.
  // `final` flushes the leftover group and any trailing text that never got
  // terminal punctuation.
  let firstBuf = "";          // sentences accumulating toward the first chunk
  const drain = (final) => {
    const rest = text.slice(chunkedUpto);
    const re = /[^.!?]+[.!?]+["')\]]*/g;
    let m, consumed = 0;
    while ((m = re.exec(rest)) !== null) {
      consumed = m.index + m[0].length;
      if (!firstPushed) {
        // A two-word opener alone cannot cover the next chunk's synthesis (the
        // GROUP_CHARS ramp assumes ~TTS_FIRST_MIN chars of audio), and the join
        // after it was a guaranteed ~2s of dead air. Top the first chunk up to
        // the minimum instead: the first sound lands one sentence later, but
        // the speech is continuous.
        firstBuf += (firstBuf ? " " : "") + m[0];
        if (firstBuf.length >= TTS_FIRST_MIN) {
          firstPushed = true;
          pushChunk(firstBuf);
          firstBuf = "";
        }
      } else {
        groupBuf += " " + m[0];
        if (groupBuf.length >= groupTarget()) { pushChunk(groupBuf); groupBuf = ""; }
      }
    }
    chunkedUpto += consumed;
    if (final) {
      const tail = [firstBuf, groupBuf, text.slice(chunkedUpto)]
        .map((s) => s.trim()).filter(Boolean).join(" ");
      firstBuf = groupBuf = "";
      chunkedUpto = text.length;
      if (tail) { firstPushed = true; pushChunk(tail); }
    }
  };

  // Consumer: play chunks in order, flipping to the "speaking" state on the
  // first real sound. Returns true if it spoke anything, false if the server
  // has no voice (204 on the first chunk) so the caller uses the browser voice.
  const playChunks = async () => {
    let spoke = false;
    for (let i = 0; ; i++) {
      // Wait in a `while` (not `if`+`continue`): `continue` would run the
      // for-loop's i++ and silently skip the chunk we were just woken for.
      while (i >= chunks.length) {
        if (streamDone) return spoke;
        await new Promise((r) => { chunkSignal = r; });
      }
      playCursor = i;
      for (let k = i; k <= i + PREFETCH_AHEAD; k++) ensureFetch(k);
      const chunk = chunks[i];

      if (!serverVoice) {
        if (state !== "speaking") setState("speaking");
        await speakBrowser(chunk.text);
        spoke = true;
        continue;
      }

      const waited = performance.now();
      const result = await chunk.fetch;
      if (gen !== speakGeneration) return true;  // interrupted while fetching
      if (result.blob) {
        if (state !== "speaking") {
          setState("speaking");
          console.log(`tts: first audio in ${((performance.now() - t0) / 1000).toFixed(2)}s`);
        }
        // One line per chunk, so a gap can be attributed rather than guessed at:
        // `waitedMs` is time spent with nothing to play (the real symptom),
        // `decodeMs` is what decoding cost off the critical path.
        console.log(`tts chunk ${i + 1}: ${chunk.text.length} chars, waited `
          + `${Math.round(performance.now() - waited)}ms, decode `
          + `${result.decodeMs ?? "n/a"}ms, ${result.buffer ? "scheduled" : "element"}`);
        try {
          if (result.buffer) await playAudioBuffer(result.buffer);
          else await playAudioBlob(result.blob);
          spoke = true;
        } catch (err) {
          if (gen !== speakGeneration) return true;
          console.warn(`tts chunk ${i + 1} playback failed — device voice takes over:`, err);
          await speakBrowser(chunk.text);
          spoke = true;
        }
      } else if (result.blob === null && !spoke) {
        return false;  // no server voice configured (204) — whole-answer fallback
      } else {
        // Mid-answer fetch failure — finish this chunk in the browser voice.
        console.warn(`tts chunk ${i + 1} failed — device voice takes over:`, result.err || "empty");
        if (state !== "speaking") setState("speaking");
        await speakBrowser(chunk.text);
        spoke = true;
      }
    }
    return spoke;
  };

  const consumerPromise = playChunks();

  for (;;) {
    const { value, done } = await reader.read();
    if (gen !== speakGeneration) {  // interrupted (sleep / new question)
      controller.abort();
      streamDone = true; wake();
      await consumerPromise.catch(() => {});
      return;
    }
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of event.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let msg;
        try { msg = JSON.parse(line.slice(6)); } catch (_) { continue; }
        if (msg.delta) {
          text += msg.delta;
          bubbleA.textContent = text;
          drain(false);
        } else if (msg.error) {
          errorMsg = msg.error;
        }
        // msg.done needs no action: the read loop ends when the stream closes.
      }
    }
  }

  drain(true);           // flush the final sentence/tail
  streamDone = true; wake();

  const fullText = text.trim();
  if (!fullText) {
    await consumerPromise.catch(() => {});
    // Nothing arrived. A server-reported LLM error won't get better on an
    // immediate retry, so apologise; a silent connection drop is worth one.
    if (errorMsg) {
      console.error("stream error:", errorMsg);
      speak("My connection to the weave has frayed. Ask me again in a moment.");
      bubbleA.textContent = `(error: ${errorMsg})`;
      return;
    }
    throw new Error("stream ended with no text");
  }
  if (errorMsg) console.warn("stream died after partial text — speaking what arrived:", errorMsg);
  bubbleA.textContent = fullText;

  let spoke;
  try {
    spoke = await consumerPromise;
  } catch (err) {
    console.warn("server voice failed — falling back to browser voice:", err);
    spoke = false;
  }
  if (!spoke && gen === speakGeneration) {
    if (state !== "speaking") setState("speaking");
    await speakBrowser(fullText);
  }

  if (state === "speaking") {
    setState("idle");
    startRecognition();
  }
}

/* ---------------- speech synthesis (voice) ---------------- */

function pickVoice() {
  const voices = speechSynthesis.getVoices();
  if (!voices.length) return null;
  // A voice chosen in the settings panel wins over the built-in preferences.
  if (prefs.voiceName) {
    const saved = voices.find((v) => v.name === prefs.voiceName);
    if (saved) return saved;
  }
  for (const pref of CONFIG.voicePrefs) {
    const v = voices.find((v) => v.name.includes(pref) || v.lang.includes(pref));
    if (v) return v;
  }
  return voices[0];
}
speechSynthesis.onvoiceschanged = () => {
  chosenVoice = pickVoice();
  populateVoiceList();
};

// Browsers silently choke on long utterances (Chrome kills anything over
// ~15s of speech). Split into sentence-sized chunks and queue them instead.
function chunkText(text, maxLen = 180) {
  const sentences = text.match(/[^.!?]+[.!?]+["')\]]*|\s*[^.!?]+$/g) || [text];
  const chunks = [];
  let current = "";
  for (const s of sentences) {
    if ((current + s).length > maxLen && current) {
      chunks.push(current.trim());
      current = "";
    }
    // A single monster sentence still needs splitting — break on commas.
    if (s.length > maxLen) {
      for (const piece of s.split(/(?<=,)\s+/)) {
        if ((current + piece).length > maxLen && current) {
          chunks.push(current.trim());
          current = "";
        }
        current += " " + piece;
      }
    } else {
      current += " " + s;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

let speakWatchdog = null;

// Bumped whenever playback is torn down (sleep, new question). The chunked
// server-voice pipeline captures the value when it starts and bails between
// chunks if it has moved on, so stale audio never plays over a new answer.
let speakGeneration = 0;

// Speak an answer, then return to listening. Tries the familiar's own server voice
// first (if enabled); on 204/failure/decode error it falls back to the
// browser's local voice so he never goes mute.
async function speak(text) {
  setState("speaking");
  bubbleA.textContent = text;
  speechSynthesis.cancel();
  stopAudio();

  let spoke = false;
  if (prefs.serverVoice !== false) {
    try {
      spoke = await speakServer(text);
    } catch (err) {
      console.warn("server voice failed — falling back to browser voice:", err);
    }
  }
  if (!spoke) await speakBrowser(text);

  // Only hand back to the ears if we're still meant to be speaking — a tap on
  // "sleep" (or a new question) may have moved us on while audio played.
  if (state === "speaking") {
    setState("idle");
    startRecognition();
  }
}

// Server voice, pipelined. Synthesis costs ~1s + 0.7× the speech duration, so
// asking for a whole answer at once means ~15s of dead air. Instead: split at
// sentence punctuation into up to three growing chunks (first sentence alone
// for a fast start), fetch ahead while earlier chunks play, and stitch the
// playback together. Resolves true if the answer was spoken (even if a later
// chunk had to finish in the browser voice), false if the server has no voice
// configured (HTTP 204 on the first chunk); throws if the first chunk fails so
// speak() can fall back to the browser voice for the whole answer.
async function speakServer(text) {
  const chunks = ttsChunks(text);
  return playChunkPipeline(chunks, fetchTts(chunks[0]), speakGeneration);
}

// The pipelined player: chunks[0]'s fetch is already in flight (firstFetch),
// so the streaming path can enter here with a head start. Fetches run at most
// two at a time ahead of playback (Gemini's free-tier TTS budget is small).
// Later fetches are wrapped so a failure parks in .err instead of rejecting a
// promise nobody is awaiting yet. Same contract as speakServer.
async function playChunkPipeline(chunks, firstFetch, gen, t0 = performance.now()) {
  const fetches = [firstFetch];
  if (chunks.length > 1) fetches[1] = fetchTtsSafe(chunks[1]);

  const firstBlob = await fetches[0];
  if (firstBlob === null) return false; // no server voice configured
  if (gen !== speakGeneration) return true; // interrupted while fetching

  for (let i = 0; i < chunks.length; i++) {
    let blob;
    if (i === 0) {
      blob = firstBlob;
      console.log(`tts: first audio in ${((performance.now() - t0) / 1000).toFixed(2)}s` +
                  ` (${chunks.length} chunk${chunks.length > 1 ? "s" : ""})`);
    } else {
      const result = await fetches[i];
      if (gen !== speakGeneration) return true;
      if (result.err || !result.blob) {
        // Mid-answer failure — finish the remaining text in the device voice
        // rather than going silent.
        console.warn(`tts chunk ${i + 1}/${chunks.length} failed — device voice takes over:`,
                     result.err || "empty response");
        await speakBrowser(chunks.slice(i).join(" "));
        return true;
      }
      blob = result.blob;
    }

    if (gen !== speakGeneration) return true;
    if (i + 2 < chunks.length && !fetches[i + 2]) {
      fetches[i + 2] = fetchTtsSafe(chunks[i + 2]);
    }

    if (i === 0) {
      await playAudioBlob(blob); // a first-chunk playback error throws → full fallback
    } else {
      try {
        await playAudioBlob(blob);
      } catch (err) {
        if (gen !== speakGeneration) return true;
        console.warn(`tts chunk ${i + 1} playback failed — device voice takes over:`, err);
        await speakBrowser(chunks.slice(i).join(" "));
        return true;
      }
    }
    console.log(`tts: chunk ${i + 1}/${chunks.length} done at ` +
                `${((performance.now() - t0) / 1000).toFixed(2)}s`);
  }
  return true;
}

function splitSentences(text) {
  return (text.match(/[^.!?]+[.!?]+["')\]]*|\s*[^.!?]+$/g) || [text])
    .map((s) => s.trim()).filter(Boolean);
}

// The least text worth synthesising as the opening chunk. ~40 chars is ~2.6s
// spoken -- per the measurements in submitQuestionStreaming's GROUP_CHARS
// comment, just enough audio to cover the next chunk's synthesis. An opener
// below this ("Ah, yes.") is padded with the following sentence(s) rather than
// sent alone, because the pause after a too-short first chunk is a worse
// experience than a slightly later start.
const TTS_FIRST_MIN = 40;

// Group sentences into at most two chunks: ~midLen chars, then the rest.
// (Consumes the array.)
function groupSentences(sentences, midLen = 200) {
  if (!sentences.length) return [];
  let mid = sentences.shift();
  while (sentences.length && (mid + " " + sentences[0]).length <= midLen) {
    mid += " " + sentences.shift();
  }
  const rest = sentences.join(" ");
  return rest ? [mid, rest] : [mid];
}

// Split answer text into up to three chunks at sentence punctuation, sized so
// each is ready before the previous finishes playing: the first sentence
// (topped up to TTS_FIRST_MIN -- a two-word opener can't cover the middle
// chunk's synthesis), then ~200 chars, then the rest.
function ttsChunks(text, midLen = 200) {
  const sentences = splitSentences(text);
  if (sentences.length <= 1) return [text.trim()];
  let first = sentences.shift();
  while (sentences.length && first.length < TTS_FIRST_MIN) {
    first += " " + sentences.shift();
  }
  if (!sentences.length) return [first];
  return [first, ...groupSentences(sentences, midLen)];
}

// One /api/tts call. Resolves to a Blob, or null when there's no server voice
// (204 / empty body); rejects on HTTP errors.
function fetchTts(text) {
  return fetch(api("/tts"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then(async (res) => {
    if (res.status === 204) return null;
    if (!res.ok) throw new Error(`tts ${res.status}`);
    const blob = await res.blob();
    return blob.size ? blob : null;
  });
}

// Prefetch variant that never rejects: resolves {blob} or {err}, so chunks
// fetched ahead of time can't cause unhandled-rejection noise if we bail.
function fetchTtsSafe(text) {
  // Decode here, not at playback time. Turning ~700KB of WAV into an AudioBuffer
  // costs real milliseconds, and doing it while the previous chunk is still
  // playing means it costs nothing anybody hears. A decode failure is not fatal:
  // the blob is kept, and playback falls back to the <audio> element.
  return fetchTts(text).then(
    async (blob) => {
      if (!blob) return { blob };
      const t = performance.now();
      try {
        ensureTtsGain();
        const buffer = await audioCtx.decodeAudioData(await blob.arrayBuffer());
        return { blob, buffer, decodeMs: Math.round(performance.now() - t) };
      } catch (err) {
        return { blob };
      }
    },
    (err) => ({ err }),
  );
}

// Where the next chunk should begin, on the AudioContext's own clock. This is
// what makes joins seamless: chunk i+1 is scheduled to start at the exact
// sample chunk i ends, rather than being started by a callback that fires after
// it has already ended.
let ttsNextStart = 0;
let ttsSources = [];

// Play a decoded chunk, scheduled back-to-back with whatever is already queued.
//
// The <audio> element cannot do this. It holds one source at a time, so each
// chunk's load and decode happen *after* the previous chunk's `onended` — a
// fixed per-chunk cost, in series, that prefetching cannot remove because there
// is nowhere to put the next sound until the current one is finished with. That
// cost is the gap between utterances. A buffer source has no such limit: the
// audio is already in memory and the context mixes as many as you schedule.
//
// Everything downstream is unchanged — these connect to the same ttsGain, so
// the limiter, the volume preference and the analyser that drives the puppet's
// jaw all behave exactly as before.
function playAudioBuffer(buffer) {
  ensureTtsGain();
  if (!audioCtx || !ttsGain) return Promise.reject(new Error("no audio graph"));
  return (audioCtx.state === "suspended"
      ? audioCtx.resume().catch(() => {})
      : Promise.resolve()
  ).then(() => new Promise((resolve, reject) => {
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(ttsGain);
    const startAt = Math.max(audioCtx.currentTime, ttsNextStart);
    const stopViseme = startVisemeTracking();
    source.onended = () => {
      stopViseme();
      ttsSources = ttsSources.filter((s) => s !== source);
      resolve();
    };
    try {
      source.start(startAt);
    } catch (err) {
      stopViseme();
      reject(err);
      return;
    }
    ttsNextStart = startAt + buffer.duration;
    ttsSources.push(source);
  }));
}

// Play one audio blob through the gesture-unlocked element (routed through the
// WebAudio gain graph). Resolves when playback ends; rejects on any error.
async function playAudioBlob(blob) {
  const url = URL.createObjectURL(blob);
  ensureTtsGain();
  // Must *await* the resume: playing into a still-suspended context is pure
  // silence with onended firing normally — it looks like success and sounds
  // like nothing. (iOS also suspends the context when the app backgrounds.)
  if (audioCtx && audioCtx.state === "suspended") {
    try { await audioCtx.resume(); } catch (_) {}
  }
  const stopViseme = startVisemeTracking();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      stopViseme();
      URL.revokeObjectURL(url);
      ttsAudio.onended = ttsAudio.onerror = null;
    };
    ttsAudio.onended = () => { cleanup(); resolve(); };
    ttsAudio.onerror = () => { cleanup(); reject(new Error("audio playback error")); };
    ttsAudio.src = url;
    ttsAudio.play().catch((err) => { cleanup(); reject(err); });
  });
}

function stopAudio() {
  speakGeneration += 1; // cancel any in-flight chunk pipeline
  // Scheduled buffers are already inside the audio clock, so pausing the
  // element is not enough -- anything queued would play on over the top of
  // whatever comes next.
  for (const source of ttsSources.splice(0)) {
    try { source.onended = null; source.stop(); } catch (_) {}
  }
  ttsNextStart = 0;
  try {
    ttsAudio.onended = ttsAudio.onerror = null;
    ttsAudio.pause();
    ttsAudio.removeAttribute("src");
    ttsAudio.load();
  } catch (_) {}
}

// Browser voice: the local speechSynthesis fallback. Long replies are split
// into sentence-sized chunks (engines choke past ~15s) and queued. Resolves
// when the whole utterance has finished (or the watchdog decides it has).
function speakBrowser(text) {
  return new Promise((resolve) => {
    if (!chosenVoice) chosenVoice = pickVoice();
    const chunks = chunkText(text);
    let remaining = chunks.length;
    let settled = false;

    const finish = () => {
      if (settled) return;
      settled = true;
      clearInterval(speakWatchdog);
      resolve();
    };

    for (const chunk of chunks) {
      const utter = new SpeechSynthesisUtterance(chunk);
      if (chosenVoice) utter.voice = chosenVoice;
      utter.rate = prefs.rate;
      utter.pitch = prefs.pitch;
      utter.onend = utter.onerror = () => {
        remaining -= 1;
        if (remaining <= 0) finish();
      };
      speechSynthesis.speak(utter);
    }

    // Watchdog: some engines drop utterances without firing onend/onerror.
    // If nothing is speaking or pending any more, unstick ourselves.
    clearInterval(speakWatchdog);
    let quietChecks = 0;
    speakWatchdog = setInterval(() => {
      if (speechSynthesis.speaking || speechSynthesis.pending) {
        quietChecks = 0;
      } else if (++quietChecks >= 2) {
        finish();
      }
    }, 1000);
  });
}

/* ---------------- cat noises (synthesised, no audio files) ---------------- */

let audioCtx = null;

// Route the server voice through a gain stage so it can play louder than the
// device's own <audio> ceiling, with a compressor acting as a limiter to catch
// the peaks the gain pushes past 0 dBFS. Built once, lazily, from a gesture.
let ttsGain = null;
let ttsAnalyser = null;
function ensureTtsGain() {
  if (ttsGain) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaElementSource(ttsAudio);
    ttsGain = audioCtx.createGain();
    ttsGain.gain.value = prefs.gain;
    const limiter = audioCtx.createDynamicsCompressor();
    src.connect(ttsGain).connect(limiter).connect(audioCtx.destination);
    // Tap the post-gain signal to drive the puppet's jaw in time with the real
    // speech (see startVisemeTracking). A passive AnalyserNode reads its input
    // without needing an onward connection, so it doesn't alter the audio.
    ttsAnalyser = audioCtx.createAnalyser();
    ttsAnalyser.fftSize = 512;
    ttsGain.connect(ttsAnalyser);
  } catch (err) {
    // No WebAudio (or the element's already sourced) — fall back to plain
    // element playback at device volume.
    console.warn("audio gain setup failed:", err);
  }
}

// Same gain-stage treatment for the recorded meows, with its own level so the
// clips (mastered much hotter than the TTS voice) can be balanced against it.
let meowGain = null;
function ensureMeowGain() {
  if (meowGain) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaElementSource(meowAudio);
    meowGain = audioCtx.createGain();
    meowGain.gain.value = prefs.meowGain;
    const limiter = audioCtx.createDynamicsCompressor();
    src.connect(meowGain).connect(limiter).connect(audioCtx.destination);
  } catch (err) {
    console.warn("meow gain setup failed:", err);
  }
}

// While TTS audio plays, sample its envelope ~25×/s and push it to the body bus
// as {type:"viseme", level 0-1} so a puppet's jaw servo can move in time with
// the actual speech (body_client.py's set_jaw_level consumes it). Returns a
// stop function that also closes the jaw.
function startVisemeTracking() {
  if (!ttsAnalyser) return () => {};
  const data = new Uint8Array(ttsAnalyser.fftSize);
  const timer = setInterval(() => {
    ttsAnalyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;  // waveform around the 128 midpoint
      sum += v * v;
    }
    const rms = Math.sqrt(sum / data.length);
    // Speech RMS is small; scale + curve so quiet syllables still open the jaw,
    // then clamp to 0..1.
    const level = Math.min(1, Math.pow(rms * 3.2, 0.6));
    sendViseme(Number(level.toFixed(2)));
  }, 40);
  return () => { clearInterval(timer); sendViseme(0); };
}

// One "syllable" of cat: a sawtooth+triangle pair with vibrato, swept
// through a bandpass filter. freqs = [start, peak, end] in Hz.
function catSound({ freqs, duration, volume = 0.3 }) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t = audioCtx.currentTime;
    const [f0, f1, f2] = freqs;

    const osc = audioCtx.createOscillator();
    osc.type = "sawtooth";
    osc.frequency.setValueAtTime(f0, t);
    osc.frequency.linearRampToValueAtTime(f1, t + duration * 0.3);
    osc.frequency.setValueAtTime(f1, t + duration * 0.45);
    osc.frequency.exponentialRampToValueAtTime(f2, t + duration);

    const body = audioCtx.createOscillator(); // warmth an octave down
    body.type = "triangle";
    body.frequency.setValueAtTime(f0 / 2, t);
    body.frequency.linearRampToValueAtTime(f1 / 2, t + duration * 0.3);
    body.frequency.exponentialRampToValueAtTime(f2 / 2, t + duration);

    const vibrato = audioCtx.createOscillator();
    vibrato.frequency.value = 8;
    const vibratoGain = audioCtx.createGain();
    vibratoGain.gain.value = f1 * 0.03;
    vibrato.connect(vibratoGain).connect(osc.frequency);

    const filter = audioCtx.createBiquadFilter();
    filter.type = "bandpass";
    filter.Q.value = 2.5;
    filter.frequency.setValueAtTime(f1 * 1.6, t);
    filter.frequency.exponentialRampToValueAtTime(f2 * 1.4, t + duration);

    const gain = audioCtx.createGain();
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(volume, t + 0.05);
    gain.gain.setValueAtTime(volume * 0.8, t + duration * 0.6);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);

    osc.connect(filter);
    body.connect(filter);
    filter.connect(gain).connect(audioCtx.destination);
    [osc, body, vibrato].forEach((o) => { o.start(t); o.stop(t + duration + 0.05); });
  } catch (_) {}
}

// "Meooow" — two syllables: rising me-, falling -ow.
function meow() {
  catSound({ freqs: [420, 760, 620], duration: 0.28, volume: 0.28 });
  setTimeout(() => catSound({ freqs: [660, 700, 260], duration: 0.5, volume: 0.32 }), 240);
}

// "Mrrp?" — the short questioning chirp when he hears his name.
function chirp() {
  catSound({ freqs: [480, 900, 850], duration: 0.22, volume: 0.22 });
}

/* ---------------- screen wake lock ---------------- */

// The iPad locks its screen after a minute or two, which kills the mic and the
// avatar mid-session. Hold a screen wake lock while the familiar is awake. iOS drops
// the lock whenever the tab is backgrounded, so we re-request it every time the
// page becomes visible again (Safari ≥ 16.4, Chrome, Edge).

let wakeLock = null;

async function requestWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch (err) {
    console.warn("wake lock request failed:", err.name, err.message);
    wakeLock = null;
  }
}

async function releaseWakeLock() {
  if (!wakeLock) return;
  try { await wakeLock.release(); } catch (_) {}
  wakeLock = null;
}

document.addEventListener("visibilitychange", () => {
  // Re-grab the lock when we come back to the foreground and the familiar's awake.
  if (document.visibilityState === "visible" && state !== "asleep") {
    requestWakeLock();
  }
});

/* ---------------- wake / sleep ---------------- */

// Delegated from the mount rather than bound to the SVG: the shell is injected
// after this runs, and is replaced outright whenever the familiar changes.
document.getElementById("shell").addEventListener("click", () => {
  if (state === "asleep") {
    // A user gesture is required before iOS lets us use mic + speech synthesis;
    // an empty utterance here unlocks browser TTS, and playing a scrap of
    // silence through the audio element unlocks the server voice for later.
    speechSynthesis.speak(new SpeechSynthesisUtterance(""));
    ttsAudio.src = SILENT_WAV;
    ttsAudio.play().catch(() => {});
    meowAudio.src = SILENT_WAV;
    meowAudio.play().catch(() => {});  // unlock the meow element in the same gesture
    chosenVoice = pickVoice();
    requestWakeLock();
    meow();            // creates/unlocks the AudioContext within this gesture
    ensureTtsGain();   // ...so the server-voice gain graph can be built now
    ensureMeowGain();  // ...and the meow clips' volume stage with it
    setState("idle");
    startRecognition();
  } else if (state === "idle") {
    meow(); // petting the cat
  }
});

function goToSleep() {
  stopRecognition();
  speechSynthesis.cancel();
  stopAudio();
  clearTimeout(silenceTimer);
  clearTimeout(listenTimer);
  bubbleEl.hidden = true;
  releaseWakeLock();
  flushAmbient(); // don't lose a near-threshold buffer between sessions
  meow();
  setState("asleep");
}

sleepBtn.addEventListener("click", goToSleep);

/* ---------------- familiar picker ---------------- */
// Which creature is bound to this table. Only rendered when there's more than
// one to choose between -- a single-familiar table shouldn't grow a menu.

function renderFamiliarPicker() {
  const row = document.getElementById("familiar-row");
  const select = document.getElementById("familiar-select");
  if (!row || !select) return;
  row.hidden = knownFamiliars.length < 2;
  if (row.hidden) return;
  select.innerHTML = "";
  for (const f of knownFamiliars) {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = f.name;
    opt.selected = f.id === CONFIG.id;
    select.appendChild(opt);
  }
}

document.getElementById("familiar-select").addEventListener("change", async (ev) => {
  const select = ev.target;
  select.disabled = true;
  try {
    await switchFamiliar(select.value);
  } catch (err) {
    statusEl.textContent = `Couldn't switch: ${err.message}`;
    renderFamiliarPicker();  // put the menu back on the familiar that's actually bound
  } finally {
    select.disabled = false;
  }
});

/* ---------------- voice settings panel ---------------- */

const settingsEl = document.getElementById("settings");
const serverVoiceToggle = document.getElementById("server-voice");
const serverVoicePickerRow = document.getElementById("server-voice-picker-row");
const serverVoiceSelect = document.getElementById("server-voice-select");
const meowToggle = document.getElementById("meow-toggle");
const ambientToggle = document.getElementById("ambient-toggle");
const voiceSelect = document.getElementById("voice-select");
const rateInput = document.getElementById("rate");
const pitchInput = document.getElementById("pitch");
const gainInput = document.getElementById("gain");
const rateVal = document.getElementById("rate-val");
const pitchVal = document.getElementById("pitch-val");
const gainVal = document.getElementById("gain-val");
const meowGainInput = document.getElementById("meow-gain");
const meowGainVal = document.getElementById("meow-gain-val");

function populateVoiceList() {
  const voices = speechSynthesis.getVoices();
  if (!voices.length) return;
  // English voices first, then the rest.
  const sorted = [...voices].sort((a, b) =>
    (b.lang.startsWith("en") ? 1 : 0) - (a.lang.startsWith("en") ? 1 : 0) ||
    a.name.localeCompare(b.name));
  voiceSelect.innerHTML = "";
  for (const v of sorted) {
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = `${v.name} (${v.lang})`;
    if (chosenVoice && v.name === chosenVoice.name) opt.selected = true;
    voiceSelect.appendChild(opt);
  }
}

// Its own voice may have more than one clone available server-side (see
// POCKET_VOICE / voices/*.safetensors|.wav); GET /api/voices lists them and
// POST /api/voice switches the live one. Only relevant when server-voice is on.
async function populateServerVoiceList() {
  try {
    const res = await fetch(api("/voices"));
    if (!res.ok) return;
    const data = await res.json();
    const names = Array.isArray(data.voices) ? data.voices : [];
    serverVoicePickerRow.hidden = names.length < 2 || prefs.serverVoice === false;
    serverVoiceSelect.innerHTML = "";
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === data.current) opt.selected = true;
      serverVoiceSelect.appendChild(opt);
    }
  } catch (_) { /* no pocket-tts voices to offer — leave the row hidden */ }
}

serverVoiceSelect.addEventListener("change", async () => {
  const name = serverVoiceSelect.value;
  serverVoiceSelect.disabled = true;
  try {
    const res = await fetch(api("/voice"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: name }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || res.statusText);
    }
  } catch (err) {
    console.warn("voice switch failed:", err);
    statusEl.textContent = "Couldn't switch voice — try again.";
  } finally {
    serverVoiceSelect.disabled = false;
  }
});

function syncSliders() {
  rateInput.value = prefs.rate;
  pitchInput.value = prefs.pitch;
  gainInput.value = prefs.gain;
  meowGainInput.value = prefs.meowGain;
  rateVal.textContent = `× ${Number(prefs.rate).toFixed(2)}`;
  pitchVal.textContent = Number(prefs.pitch).toFixed(2);
  gainVal.textContent = `× ${Number(prefs.gain).toFixed(2)}`;
  meowGainVal.textContent = `× ${Number(prefs.meowGain).toFixed(2)}`;
}

document.getElementById("settings-btn").addEventListener("click", () => {
  closeTableNav();          // two fixed panels at once is the one real collision
  populateVoiceList();
  populateServerVoiceList();
  syncSliders();
  serverVoiceToggle.checked = prefs.serverVoice !== false;
  meowToggle.checked = prefs.meow !== false;
  ambientToggle.checked = prefs.ambient === true;
  settingsEl.hidden = !settingsEl.hidden;
});
serverVoiceToggle.addEventListener("change", () => {
  prefs.serverVoice = serverVoiceToggle.checked;
  savePrefs();
  populateServerVoiceList();
});
meowToggle.addEventListener("change", () => {
  prefs.meow = meowToggle.checked;
  savePrefs();
  if (prefs.meow) playRandomMeow();  // audible confirmation (and unlocks the element)
});
ambientToggle.addEventListener("change", () => {
  prefs.ambient = ambientToggle.checked;
  if (!prefs.ambient) flushAmbient(); // don't hold a partial buffer once turned off
  recordingIndicatorEl.hidden = !prefs.ambient;
  savePrefs();
});
document.getElementById("close-settings").addEventListener("click", () => {
  settingsEl.hidden = true;
});
voiceSelect.addEventListener("change", () => {
  prefs.voiceName = voiceSelect.value;
  chosenVoice = pickVoice();
  savePrefs();
});
rateInput.addEventListener("input", () => {
  prefs.rate = Number(rateInput.value);
  syncSliders();
  savePrefs();
});
pitchInput.addEventListener("input", () => {
  prefs.pitch = Number(pitchInput.value);
  syncSliders();
  savePrefs();
});
gainInput.addEventListener("input", () => {
  prefs.gain = Number(gainInput.value);
  if (ttsGain) ttsGain.gain.value = prefs.gain;  // live while testing
  syncSliders();
  savePrefs();
});
meowGainInput.addEventListener("input", () => {
  prefs.meowGain = Number(meowGainInput.value);
  if (meowGain) meowGain.gain.value = prefs.meowGain;  // live while testing
  syncSliders();
  savePrefs();
});
// On release, play a clip at the new level so it can be judged immediately.
meowGainInput.addEventListener("change", () => playRandomMeow());
document.getElementById("test-voice").addEventListener("click", async () => {
  const line =
    "Naturally I have opinions about your dice rolls. I simply choose when to share them.";
  // Unlock audio inside this gesture — on iOS a fresh page that hasn't had
  // the wake-up tap yet has a locked audio element and a suspended
  // AudioContext, and playback would be silently swallowed.
  speechSynthesis.speak(new SpeechSynthesisUtterance(""));
  ttsAudio.src = SILENT_WAV;
  ttsAudio.play().catch(() => {});
  ensureTtsGain();
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  speechSynthesis.cancel();
  stopAudio();
  // Same routing as a real answer: server voice first (if enabled), then the
  // device voice — but without touching the state machine.
  if (prefs.serverVoice !== false) {
    try {
      if (await speakServer(line)) return;
    } catch (err) {
      console.warn("server voice test failed — using device voice:", err);
    }
  }
  const utter = new SpeechSynthesisUtterance(line);
  chosenVoice = pickVoice();
  if (chosenVoice) utter.voice = chosenVoice;
  utter.rate = prefs.rate;
  utter.pitch = prefs.pitch;
  speechSynthesis.speak(utter);
});
document.getElementById("test-meow").addEventListener("click", meow);

// Recap the previous session: the server summarises the last session's logged
// Q&A in the familiar's voice, and we speak it through the normal voice path.
// Needs the familiar awake (the gesture that unlocked audio).
recapBtn.addEventListener("click", async () => {
  if (state === "asleep") {
    statusEl.textContent = `Tap ${CONFIG.name} to wake first.`;
    return;
  }
  const btn = recapBtn;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const res = await fetch(api("/recap"), { method: "POST", headers: { "Content-Type": "application/json" } });
    if (res.status === 204) {
      statusEl.textContent = "No earlier session to recap yet.";
      return;
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    bubbleEl.hidden = false;
    bubbleQ.textContent = "“Recap last session”";
    stopRecognition();          // don't listen to ourselves while recapping
    speak(data.recap);          // sets speaking → idle → restarts the ears
  } catch (err) {
    console.error("recap failed:", err);
    statusEl.textContent = "Couldn't fetch the recap.";
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

setState("asleep");
