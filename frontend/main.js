// Jarvis V2 — Frontend
const orb = document.getElementById('orb');
const status = document.getElementById('status');
const transcript = document.getElementById('transcript');

let ws;
let audioQueue = [];
let isPlaying = false;
let audioUnlocked = false;
let audioCtx = null;
let lastDisconnectTime = 0;

// Send a JSON message but only when the socket is OPEN. Otherwise the
// browser raises InvalidStateError and the page logs a noisy stack
// trace — we'd rather silently drop and let the auto-reconnect handler
// recover. Returns true on success.
function wsSend(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        console.warn('[jarvis] wsSend skipped, readyState=' + (ws ? ws.readyState : 'no ws'));
        return false;
    }
    try {
        ws.send(JSON.stringify(payload));
        return true;
    } catch (e) {
        console.warn('[jarvis] wsSend failed:', e);
        return false;
    }
}

// Audio-queue stall watchdog. If the <audio> element neither fires
// `onended` nor `onerror` within MAX_AUDIO_MS (Chrome occasionally
// drops these events after a sleep/wake or screen-share), we force-
// advance the queue so the conversation doesn't deadlock.
const MAX_AUDIO_MS = 60000;
let _audioWatchdog = null;
function armAudioWatchdog() {
    if (_audioWatchdog) clearTimeout(_audioWatchdog);
    _audioWatchdog = setTimeout(() => {
        _audioWatchdog = null;
        console.warn('[jarvis] audio watchdog fired — advancing queue');
        playNext();
    }, MAX_AUDIO_MS);
}
function clearAudioWatchdog() {
    if (_audioWatchdog) { clearTimeout(_audioWatchdog); _audioWatchdog = null; }
}

// Reset all transient playback / orb state. Called on socket disconnect
// so a stuck `isPlaying=true` from a half-played message doesn't keep
// the orb frozen and recognition disabled until reload.
function resetPlaybackState() {
    audioQueue = [];
    isPlaying = false;
    clearAudioWatchdog();
    setOrbState('idle');
}

// Auto-hide: when Jarvis finishes speaking he stays visible for this many
// milliseconds. Any further speech (the user talking, or new audio playing)
// resets the timer. Empirically 30 s is enough for a follow-up question
// without making the desktop permanently busy.
const AUTO_HIDE_DELAY_MS = 30000;
let _hideTimer = null;
function scheduleHide() {
    if (_hideTimer) clearTimeout(_hideTimer);
    _hideTimer = setTimeout(() => {
        _hideTimer = null;
        fetch('/hide').catch(() => {});
    }, AUTO_HIDE_DELAY_MS);
}
function cancelHide() {
    if (_hideTimer) { clearTimeout(_hideTimer); _hideTimer = null; }
}

// Follow-up window: für FOLLOW_UP_WINDOW_MS nach Jarvis' letztem
// Audio-Output akzeptieren wir Sprachbefehle OHNE "Jarvis"-Wake-Word
// — Catrin kann fluessig auf eine Frage antworten, ohne jedesmal
// "Jarvis" voranzustellen. Nach Ablauf wird das Wake-Word wieder
// Pflicht. Wenn Jarvis erneut spricht (audio kommt), startet das
// Fenster nach Ablauf des neuen Audio neu.
const FOLLOW_UP_WINDOW_MS = 30000;
let followUpUntil = 0;
function startFollowUpWindow() {
    followUpUntil = Date.now() + FOLLOW_UP_WINDOW_MS;
}
function endFollowUpWindow() {
    followUpUntil = 0;
}
function inFollowUpWindow() {
    return Date.now() < followUpUntil;
}

// Begrüßungs-Debounce via sessionStorage (überlebt Seitenneuladen durch Fensterresize)
const GREET_COOLDOWN = 90000; // 90 Sekunden
function shouldGreet() {
    const last = parseInt(sessionStorage.getItem('jarvisLastGreet') || '0');
    return Date.now() - last > GREET_COOLDOWN;
}
function markGreeted() {
    sessionStorage.setItem('jarvisLastGreet', Date.now().toString());
}

// Unlock audio via AudioContext (works without user gesture in Chrome with --autoplay-policy flag)
function unlockAudio() {
    if (!audioUnlocked) {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        audioCtx.resume().then(() => {
            audioUnlocked = true;
            console.log('[jarvis] Audio unlocked via AudioContext');
        }).catch(() => {});
    }
}

// Try to unlock immediately on load (works when Chrome started with --autoplay-policy=no-user-gesture-required)
window.addEventListener('load', () => {
    unlockAudio();
    // Fallback: unlock on any interaction
    document.addEventListener('click', unlockAudio, { once: false });
    document.addEventListener('touchstart', unlockAudio, { once: false });
    document.addEventListener('keydown', unlockAudio, { once: false });
});

function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
        console.log('[jarvis] WebSocket connected');
        lastDisconnectTime = 0;
        setOrbState('listening');
    };
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'ping') {
            wsSend({ type: 'pong' });
            return;
        }
        if (data.type === 'cancelled') {
            // Server confirmed an in-flight action was aborted.
            audioQueue = [];
            isPlaying = false;
            setOrbState('listening');
            status.textContent = 'Abgebrochen.';
            setTimeout(() => { status.textContent = ''; }, 1500);
            return;
        }
        if (data.type === 'wake') {
            // External wake-up (clap-trigger / wakeword / shortcut) — keep
            // window visible until the user is finished, not just until
            // the greeting audio ends.
            cancelHide();
            fetch('/show').catch(() => {});
            if (!sessionStorage.getItem('jarvisGreeted')) {
                // Erste Aktivierung dieser Sitzung → volle Begrüßung
                sessionStorage.setItem('jarvisGreeted', '1');
                markGreeted();
                setOrbState('thinking');
                wsSend({ text: 'Jarvis activate' });
            } else {
                // Bereits begrüßt → make sure recognition is actually running.
                // After a hide / background suspend, Web Speech often pauses
                // silently — the user then talks and nothing happens. Force-
                // restart it. `startListening()` is a no-op if it's already
                // active, so calling it unconditionally is safe.
                if (isListening) {
                    try { recognition.stop(); } catch (e) {}
                    isListening = false;
                }
                setOrbState('listening');
                setTimeout(startListening, 200);
            }
            return;
        }
        if (data.type === 'response') {
            addTranscript('jarvis', data.text);
            if (data.audio && data.audio.length > 0) {
                queueAudio(data.audio);
            } else {
                setOrbState('idle');
                setTimeout(startListening, 500);
            }
        } else if (data.type === 'status') {
            status.textContent = data.text;
        }
    };
    ws.onclose = () => {
        lastDisconnectTime = Date.now();
        status.textContent = '';
        // Auf einer abgerissenen Verbindung kann ein laufendes Audio
        // niemals 'onended' bekommen; ohne Reset bliebe `isPlaying`
        // permanent true und Spracherkennung waere blockiert.
        resetPlaybackState();
        setTimeout(connect, 5000);
    };
}

function queueAudio(base64Audio) {
    audioQueue.push(base64Audio);
    if (!isPlaying) playNext();
}

function playNext() {
    clearAudioWatchdog();
    if (audioQueue.length === 0) {
        isPlaying = false;
        setOrbState('listening');
        status.textContent = '';
        // Schedule a hide AUTO_HIDE_DELAY_MS from now. If the user starts
        // talking before then, the recognition handler cancels the timer
        // and Jarvis stays visible for the follow-up.
        scheduleHide();
        // Open the follow-up window — Catrin can answer the next 30s
        // without saying "Jarvis" first.
        startFollowUpWindow();
        setTimeout(startListening, 500);
        return;
    }
    isPlaying = true;
    // New audio means there's interaction happening — keep window visible.
    cancelHide();
    // Pause the follow-up window while Jarvis speaks; it will restart
    // when his current audio finishes.
    endFollowUpWindow();
    setOrbState('speaking');
    status.textContent = '';
    if (isListening) {
        recognition.stop();
        isListening = false;
    }

    const b64 = audioQueue.shift();
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.onended = () => { clearAudioWatchdog(); URL.revokeObjectURL(url); playNext(); };
    audio.onerror = () => { clearAudioWatchdog(); URL.revokeObjectURL(url); playNext(); };
    armAudioWatchdog();
    audio.play().catch(err => {
        console.warn('[jarvis] Autoplay blocked, waiting for click...');
        status.textContent = 'Klicke irgendwo damit Jarvis sprechen kann.';
        setOrbState('idle');
        // Wait for click then retry
        document.addEventListener('click', function retry() {
            document.removeEventListener('click', retry);
            audio.play().then(() => {
                setOrbState('speaking');
                status.textContent = '';
            }).catch(() => playNext());
        });
    });
}

// Speech Recognition
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition;
let isListening = false;
const SPEECH_AVAILABLE = !!SpeechRecognition;

if (!SPEECH_AVAILABLE) {
    // Firefox / Safari without webkit prefix have no SpeechRecognition.
    // Make the failure mode obvious instead of silently doing nothing.
    status.textContent = 'Spracherkennung nicht verfuegbar. Bitte Google Chrome verwenden.';
    setOrbState('idle');
    orb.style.cursor = 'not-allowed';
    orb.title = 'Spracherkennung nicht verfuegbar (Chrome benoetigt)';
}

if (SPEECH_AVAILABLE) {
    recognition = new SpeechRecognition();
    recognition.lang = 'de-DE';
    recognition.continuous = true;
    recognition.interimResults = false;

    recognition.onresult = (event) => {
        const last = event.results[event.results.length - 1];
        if (last.isFinal) {
            const text = last[0].transcript.trim();
            const lower = text.toLowerCase();

            // Any final speech result is a sign of activity — postpone hide.
            cancelHide();

            // Cancel keywords stop any running action.
            // Recognized: "stopp jarvis", "stop jarvis", "halt jarvis", "abbruch".
            if (/(^|\s)(stopp|stop|halt) jarvis\b/i.test(lower) || /\babbruch\b/i.test(lower)) {
                addTranscript('user', text);
                wsSend({ type: 'cancel' });
                status.textContent = 'Abbruch gesendet.';
                return;
            }

            // Wake-Word "Jarvis" ist Pflicht — AUSSER wir sind im
            // Follow-Up-Fenster (30 s nach Jarvis' letzter Antwort).
            // Dann akzeptiert Jarvis auch Befehle ohne Praefix, damit
            // Catrin fluessig antworten kann.
            const startsWithJarvis = lower.startsWith('jarvis');
            if (startsWithJarvis || inFollowUpWindow()) {
                const command = startsWithJarvis
                    ? (text.replace(/^jarvis[,\s]*/i, '').trim() || 'Jarvis bereit')
                    : text;
                // Sobald wir den Befehl absenden, schliessen wir das
                // Fenster — Jarvis' naechste Antwort startet ein neues.
                endFollowUpWindow();
                addTranscript('user', command);
                setOrbState('thinking');
                status.textContent = 'Jarvis denkt nach...';
                wsSend({ text: command });
            }
        }
    };

    recognition.onend = () => {
        isListening = false;
        if (!isPlaying) setTimeout(startListening, 300);
    };

    recognition.onerror = (event) => {
        isListening = false;
        if (!isPlaying) {
            const delay = (event.error === 'no-speech' || event.error === 'aborted') ? 300 : 1000;
            setTimeout(startListening, delay);
        }
    };
}

function startListening() {
    if (isPlaying || !SPEECH_AVAILABLE) return;
    try {
        recognition.start();
        isListening = true;
        setOrbState('listening');
        status.textContent = '';
    } catch(e) {}
}

orb.addEventListener('click', () => {
    if (isPlaying) return;
    if (!SPEECH_AVAILABLE) {
        status.textContent = 'Spracherkennung nicht verfuegbar. Bitte Google Chrome verwenden.';
        return;
    }
    if (isListening) {
        recognition.stop();
        isListening = false;
        setOrbState('idle');
        status.textContent = 'Pausiert. Klicke zum Fortsetzen.';
    } else {
        startListening();
    }
});

function setOrbState(state) { orb.className = state; }

function addTranscript(role, text) {
    const div = document.createElement('div');
    div.className = role;
    div.textContent = role === 'user' ? `Du: ${text}` : `Jarvis: ${text}`;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
}

connect();
