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
            ws.send(JSON.stringify({ type: 'pong' }));
            return;
        }
        if (data.type === 'wake') {
            fetch('/show').catch(() => {});
            if (!sessionStorage.getItem('jarvisGreeted')) {
                // Erste Aktivierung dieser Sitzung → volle Begrüßung
                sessionStorage.setItem('jarvisGreeted', '1');
                markGreeted();
                setOrbState('thinking');
                ws.send(JSON.stringify({ text: 'Jarvis activate' }));
            } else {
                // Bereits begrüßt → nur nach vorne kommen, auf Befehl warten
                setOrbState('listening');
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
        setTimeout(connect, 5000);
    };
}

function queueAudio(base64Audio) {
    audioQueue.push(base64Audio);
    if (!isPlaying) playNext();
}

function playNext() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        setOrbState('listening');
        status.textContent = '';
        // Jarvis verstecken wenn fertig
        setTimeout(() => { fetch('/hide').catch(() => {}); }, 1500);
        setTimeout(startListening, 500);
        return;
    }
    isPlaying = true;
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
    audio.onended = () => { URL.revokeObjectURL(url); playNext(); };
    audio.onerror = () => { URL.revokeObjectURL(url); playNext(); };
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

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.lang = 'de-DE';
    recognition.continuous = true;
    recognition.interimResults = false;

    recognition.onresult = (event) => {
        const last = event.results[event.results.length - 1];
        if (last.isFinal) {
            const text = last[0].transcript.trim();
            const lower = text.toLowerCase();
            // Nur reagieren wenn mit "Jarvis" angesprochen
            if (lower.startsWith('jarvis')) {
                const command = text.replace(/^jarvis[,\s]*/i, '').trim() || 'Jarvis bereit';
                addTranscript('user', command);
                setOrbState('thinking');
                status.textContent = 'Jarvis denkt nach...';
                ws.send(JSON.stringify({ text: command }));
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
    if (isPlaying) return;
    try {
        recognition.start();
        isListening = true;
        setOrbState('listening');
        status.textContent = '';
    } catch(e) {}
}

orb.addEventListener('click', () => {
    if (isPlaying) return;
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
