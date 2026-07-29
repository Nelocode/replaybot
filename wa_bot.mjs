/**
 * WhatsApp Bot — AutoReply Comercial
 * Usa Baileys (protocolo WhatsApp Web, sin API de Meta)
 * Misma lógica de messages.json, detección de idioma, y estado
 */
import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { Boom } from '@hapi/boom';
import QRCode from 'qrcode';
import { createWhatsAppCallHandler } from './wa_call_handler.mjs';
import { createWhatsAppCallHealth } from './wa_call_health.mjs';
import { PersistentInteractionState } from './interaction_state.mjs';
import { createWhatsAppMessageHandler } from './wa_message_handler.mjs';
import { KeyedSerialQueue } from './keyed_serial_queue.mjs';
import { classifyWhatsAppDisconnect } from './wa_disconnect_policy.mjs';
import { createWhatsAppVoiceNoteReader } from './wa_audio_delivery.mjs';
import { applyWhatsAppProfilePicture } from './wa_profile_picture.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE_DIR = path.resolve(process.env.BOT_DIR || __dirname);
const DATA_DIR = path.join(BASE_DIR, 'data');
const AUDIO_DIR = path.join(BASE_DIR, 'data', 'audios');
const MESSAGES_FILE = path.join(BASE_DIR, 'data', 'messages.json');
const AUTH_DIR = path.resolve(process.env.WA_AUTH_DIR || path.join(DATA_DIR, 'wa_auth'));
const QR_PATH = path.resolve(process.env.WA_QR_PATH || path.join(BASE_DIR, 'wa_qr.png'));
const CALL_HEALTH_FILE = path.resolve(
  process.env.WA_HEALTH_FILE || path.join(DATA_DIR, 'wa_call_health.json'),
);
const INTERACTION_STATE_FILE = path.resolve(
  process.env.WA_INTERACTION_STATE_FILE || path.join(DATA_DIR, 'wa_interaction_state.json'),
);
const VOICE_NOTE_CACHE_DIR = path.resolve(
  process.env.WA_VOICE_NOTE_CACHE_DIR || path.join(DATA_DIR, 'wa_voice_notes'),
);
const PROFILE_PICTURE_PATH = path.resolve(
  process.env.WA_PROFILE_PICTURE_PATH || path.join(BASE_DIR, 'assets', 'whatsapp-profile-logo.jpg'),
);
const PROFILE_PICTURE_STATE_FILE = path.resolve(
  process.env.WA_PROFILE_PICTURE_STATE_FILE || path.join(DATA_DIR, 'wa_profile_picture_state.json'),
);
const IDENTITY_FILE = path.resolve(
  process.env.WA_IDENTITY_FILE || path.join(DATA_DIR, 'wa_identity.json'),
);
const LINK_ONLY = process.env.WA_LINK_ONLY === '1';
const VOICE_NOTES_ENABLED = process.env.WA_VOICE_NOTES_ENABLED !== '0';
const PROFILE_PICTURE_ENABLED = process.env.WA_PROFILE_PICTURE_ENABLED !== '0';
const configuredDefaultLanguage = String(process.env.AUTOREPLY_DEFAULT_LANG || 'es').toLowerCase();
const DEFAULT_LANGUAGE = ['es', 'en', 'fr'].includes(configuredDefaultLanguage)
  ? configuredDefaultLanguage
  : 'es';
const callHealth = createWhatsAppCallHealth({ filePath: CALL_HEALTH_FILE, logger: console });
const interactionState = LINK_ONLY ? null : new PersistentInteractionState({
  filePath: INTERACTION_STATE_FILE,
  defaultLanguage: DEFAULT_LANGUAGE,
  logger: console,
});
// El reclamo global conserva el orden exacto de llegada antes de cualquier
// resolución LID/PN. La entrega sigue aislada por contacto.
const interactionClaimQueue = new KeyedSerialQueue();
const deliveryQueue = new KeyedSerialQueue();
const RECONNECT_DELAY_MS = 2_000;
const CONNECTION_TIMEOUT_MS = 30_000;
const parsedLinkTimeoutMs = Number.parseInt(process.env.WA_LINK_TIMEOUT_MS || '180000', 10);
const LINK_TIMEOUT_MS = Number.isFinite(parsedLinkTimeoutMs)
  ? Math.min(Math.max(parsedLinkTimeoutMs, 30_000), 600_000)
  : 180_000;

let activeSocket = null;
let reconnectTimer = null;
let shuttingDown = false;
let linkExpiryTimer = null;

if (LINK_ONLY) {
  linkExpiryTimer = setTimeout(() => shutdown('link-timeout'), LINK_TIMEOUT_MS);
}

function writeIdentity(user) {
  const rawId = typeof user?.id === 'string' ? user.id.split('@')[0].split(':')[0] : '';
  const digits = rawId.replace(/\D/g, '');
  const displayName = typeof user?.name === 'string' && user.name.trim()
    ? user.name.trim().slice(0, 120)
    : 'Cuenta de WhatsApp';
  const payload = {
    display_name: displayName,
    phone_hint: digits.length >= 4 ? `••••${digits.slice(-4)}` : null,
    updated_at: Date.now(),
  };
  const temporaryPath = `${IDENTITY_FILE}.${process.pid}.tmp`;
  fs.mkdirSync(path.dirname(IDENTITY_FILE), { recursive: true });
  fs.writeFileSync(temporaryPath, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 });
  try {
    fs.renameSync(temporaryPath, IDENTITY_FILE);
  } catch (error) {
    // Windows no siempre permite reemplazar el destino con renameSync.
    if (error?.code !== 'EEXIST' && error?.code !== 'EPERM') throw error;
    fs.rmSync(IDENTITY_FILE, { force: true });
    fs.renameSync(temporaryPath, IDENTITY_FILE);
  }
}

function scheduleReconnect() {
  if (shuttingDown || reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (shuttingDown) return;
    startBot().catch(() => {
      console.error('[WA] Reconnection setup failed');
      scheduleReconnect();
    });
  }, RECONNECT_DELAY_MS);
}

function terminateInvalidSession() {
  // `fs.watchFile` otherwise keeps Node alive after Baileys has declared the
  // credentials unusable. Preserve the health snapshot so the panel can
  // explain that a new QR is required.
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (linkExpiryTimer) clearTimeout(linkExpiryTimer);
  linkExpiryTimer = null;
  fs.unwatchFile(MESSAGES_FILE);
  setImmediate(() => process.exit(0));
}

// ── Cargar mensajes ──
function loadMessages() {
  const raw = fs.readFileSync(MESSAGES_FILE, 'utf-8');
  const data = JSON.parse(raw);
  const result = {};
  for (const [lang, langData] of Object.entries(data)) {
    if (!Array.isArray(langData.steps) || langData.steps.length < 2) {
      throw new Error(`[WA] El idioma ${lang} necesita Paso 1 y Paso 2`);
    }
    result[lang] = {
      steps: langData.steps.map(s => ({ text: s.text, audio: s.audio, loop: s.loop || false })),
      call: langData.call || { text: '📞 Llamada recibida', audio: '' }
    };
  }
  return result;
}

let MESSAGES = loadMessages();

// ── Watch messages.json para recargar en caliente (cuando admin panel guarda) ──
fs.watchFile(MESSAGES_FILE, () => {
  try {
    MESSAGES = loadMessages();
    console.log(`[WA] messages.json recargado — ${Object.keys(MESSAGES).length} idiomas`);
  } catch (e) {
    console.error('[WA] Error recargando messages.json:', e.message);
  }
});

// ── Detección de idioma (misma lógica que bot.py) ──
const LANG_PATTERNS = {
  es: /\b(hola|gracias|por\s*favor|buenos\s*días|quiero|necesito|ayuda|habla|precio|precios|tarifa|tarifas|reserva|reservas|foto|fotos|vídeo|vídeos|video|videos|buenas|amigo|claro|vale|dale|listo|entiendo|puedes|hacer|dónde|cuándo|cómo|cuál|quién|eso|esto|algo|nada|todo|más|menos|está|estoy|estamos|están|tengo|tiene|tenemos|soy|eres|somos|son)\b/gi,
  en: /\b(hello|hi|thanks|thank\s*you|please|help|want|need|can\s*i|price|prices|rate|rates|book|booking|photo|photos|video|videos|yes|sure|fine|good|great|hey|would|could|should|where|when|how|what|who|that|this|there|here|is|are|am|have|has|do|does|did|will|may|might)\b/gi,
  fr: /\b(bonjour|merci|s'il\s*vous\s*plaît|aide|besoin|vouloir|prix|tarif|tarifs|réservation|réserver|photo|photos|vidéo|vidéos|oui|d'accord|bien|tres|peux|peut|où|quand|comment|quoi|qui|que|est|suis|sommes|êtes|sont|ai|as|a|avons|avez|ont|je|tu|il|elle|nous|vous|ils|elles|ce|cet|cette|ces|mon|ton|son|ma|ta|sa)\b/gi,
};
const LANG_MARKERS = {
  es: /\b(español|castellano|hablo español|hablo espanol)\b/i,
  en: /\b(english|speak english)\b/i,
  fr: /\b(français|francais|parle français|parle francais)\b/i,
};
const AMBIGUOUS = new Set(['ok', 'no', 'si', 'hey']);

function detectLang(text) {
  const scores = { es: 0, en: 0, fr: 0 };

  for (const [lang, pattern] of Object.entries(LANG_PATTERNS)) {
    const matches = text.match(pattern);
    if (matches) {
      for (const m of matches) {
        if (!AMBIGUOUS.has(m.toLowerCase())) {
          scores[lang] += 1;
        }
      }
    }
  }

  // Markers explícitos
  for (const [lang, marker] of Object.entries(LANG_MARKERS)) {
    if (marker.test(text)) {
      scores[lang] += 20;
    }
  }

  if (Math.max(...Object.values(scores)) < 1) return null;
  return Object.entries(scores).sort((a, b) => b[1] - a[1])[0][0];
}

// ── Estado por usuario ──
// ── Obtener mensaje para un paso ──
function getMessage(lang, step) {
  const data = MESSAGES[lang] || MESSAGES['en'];
  const idx = Math.min(step, data.steps.length - 1);
  return data.steps[idx];
}

function getCallMessage(lang) {
  const data = MESSAGES[lang] || MESSAGES['en'];
  return data.call || { text: '📞 Llamada recibida', audio: '' };
}

function getResponseMessage(lang, responseKey) {
  if (responseKey === 'call') return getCallMessage(lang);
  return getMessage(lang, responseKey === 'step1' ? 0 : 1);
}

// ── Leer archivo de audio como buffer ──
const readAudio = createWhatsAppVoiceNoteReader({
  audioDir: AUDIO_DIR,
  cacheDir: VOICE_NOTE_CACHE_DIR,
  ffmpegPath: process.env.FFMPEG_PATH || 'ffmpeg',
  enabled: VOICE_NOTES_ENABLED,
  logger: console,
});

// ── Iniciar conexión WhatsApp ──
async function startBot() {
  // Crear directorio de autenticación si no existe
  if (!fs.existsSync(AUTH_DIR)) {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  callHealth.record({ type: 'connection', state: 'connecting' });

  const sock = makeWASocket({
    auth: state,
    syncFullHistory: false,
    markOnlineOnConnect: true,
    browser: ['AutoReply Bot', 'Chrome', '120.0'],
  });
  activeSocket = sock;
  const connectionWatchdog = state.creds?.registered
    ? setTimeout(() => {
        if (activeSocket !== sock || shuttingDown) return;
        console.warn('[WA] Connection timeout; retrying with the saved session');
        activeSocket = null;
        callHealth.record({ type: 'connection', state: 'closed', reason: 'timeout' });
        try {
          sock.end(new Error('connection-timeout'));
        } catch {
          // The reconnect scheduler below remains authoritative.
        }
        scheduleReconnect();
      }, CONNECTION_TIMEOUT_MS)
    : null;

  if (!LINK_ONLY) {
    // Observe only that a raw call stanza arrived. Never persist its payload.
    if (typeof sock.ws?.on === 'function') {
      sock.ws.on('CB:call', () => callHealth.record({ type: 'raw_call' }));
      callHealth.record({ type: 'raw_listener', state: 'registered' });
    } else {
      callHealth.record({ type: 'raw_listener', state: 'unavailable' });
    }

    // Register immediately: call events are not buffered by Baileys.
    const handleCallBatch = createWhatsAppCallHandler({
      rejectCall: (callId, callFrom) => sock.rejectCall(callId, callFrom),
      sendMessage: (jid, content) => sock.sendMessage(jid, content),
      getCallMessage,
      getResponseMessage,
      routeInteraction: details => interactionState.register(details),
      resolveContactId: async (jid) => {
        if (!jid.endsWith('@lid') && !jid.endsWith('@hosted.lid')) return jid;
        return sock.signalRepository?.lidMapping?.getPNForLID
          ? (await sock.signalRepository.lidMapping.getPNForLID(jid)) || jid
          : jid;
      },
      serializeClaim: operation => interactionClaimQueue.run('all-inbound', operation),
      serializeInteraction: (contactId, operation) => deliveryQueue.run(contactId, operation),
      readAudio,
      logger: console,
      onCallMetric: callHealth.record,
    });
    sock.ev.on('call', handleCallBatch);
    callHealth.record({ type: 'listener_registered' });
  }

  // ── Guardar credenciales cuando se actualicen ──
  sock.ev.on('creds.update', saveCreds);

  // ── Manejar conexión / reconexión ──
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Guardar QR como imagen PNG para el panel web
      fs.mkdirSync(path.dirname(QR_PATH), { recursive: true });
      QRCode.toFile(QR_PATH, qr, { type: 'png', width: 400, margin: 2 }, (err) => {
        if (err) console.error('[WA] Error guardando QR:', err.message);
        else console.log(`[WA] QR guardado en ${QR_PATH}`);
      });
      console.log('\n╔══════════════════════════════════════════════╗');
      console.log('║      ESCANEA EL QR EN EL PANEL ADMIN      ║');
      console.log('║   http://localhost:5000                    ║');
      console.log('╚══════════════════════════════════════════════╝\n');
    }

    if (connection === 'close') {
      if (activeSocket !== sock) return;
      if (connectionWatchdog) clearTimeout(connectionWatchdog);
      activeSocket = null;
      const disconnectStatus = lastDisconnect?.error instanceof Boom
        ? lastDisconnect.error.output?.statusCode
        : (lastDisconnect?.error?.output?.statusCode ?? lastDisconnect?.error?.data?.statusCode);
      const disconnect = classifyWhatsAppDisconnect(disconnectStatus);
      callHealth.record({
        type: 'connection',
        state: 'closed',
        reason: disconnect.reason,
        reauthRequired: disconnect.reauthRequired,
      });

      console.log(`[WA] Conexión cerrada. Estado: ${disconnectStatus ?? 'unknown'}. Reconnect: ${disconnect.shouldReconnect}`);

      if (disconnect.terminateWorker) {
        console.log('[WA] Sesión inválida. Vuelve a vincularla desde el panel.');
        terminateInvalidSession();
      } else {
        scheduleReconnect();
      }
    }

    if (connection === 'open') {
      if (activeSocket !== sock) return;
      if (connectionWatchdog) clearTimeout(connectionWatchdog);
      callHealth.record({ type: 'connection', state: 'open' });
      try {
        fs.rmSync(QR_PATH, { force: true });
        writeIdentity(sock.user);
      } catch {
        console.warn('[WA] No fue posible actualizar la identidad mostrable');
      }
      if (!LINK_ONLY && PROFILE_PICTURE_ENABLED) {
        void applyWhatsAppProfilePicture({
          jid: sock.user?.id,
          updateProfilePicture: (jid, image, dimensions) => (
            sock.updateProfilePicture(jid, image, dimensions)
          ),
          imagePath: PROFILE_PICTURE_PATH,
          statePath: PROFILE_PICTURE_STATE_FILE,
          logger: console,
        }).catch(() => {
          console.warn('[WA] La sincronización del logo de perfil falló sin afectar el bot');
        });
      }
      console.log('[WA] Connected');
    }
  });

  if (!LINK_ONLY) {
    // Un único manejador cuenta texto y cualquier multimedia en el mismo estado
    // que las llamadas. Los eventos de sincronización y control se descartan.
    const handleMessageBatch = createWhatsAppMessageHandler({
      sendMessage: (jid, content) => sock.sendMessage(jid, content),
      routeInteraction: details => interactionState.register(details),
      getResponseMessage,
      readAudio,
      detectLanguage: detectLang,
      resolvePnForLid: async (lid) => (
        sock.signalRepository?.lidMapping?.getPNForLID
          ? sock.signalRepository.lidMapping.getPNForLID(lid)
          : null
      ),
      serializeClaim: operation => interactionClaimQueue.run('all-inbound', operation),
      serializeInteraction: (contactId, operation) => deliveryQueue.run(contactId, operation),
      logger: console,
    });
    sock.ev.on('messages.upsert', handleMessageBatch);
  }

}

// ── Main ──
console.log(`🚀 WhatsApp Bot AutoReply iniciando${LINK_ONLY ? ' (sólo vinculación)' : ''}...`);
console.log(`📁 Directorio: ${BASE_DIR}`);
console.log(`📁 Auth: ${AUTH_DIR}`);
console.log(`🔑 Escanea el QR con tu WhatsApp`);
console.log('────────────────────────────────────────\n');

function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (linkExpiryTimer) clearTimeout(linkExpiryTimer);
  linkExpiryTimer = null;
  callHealth.record({ type: 'connection', state: 'closed', reason: 'shutdown' });
  console.log(`[WA] Shutting down (${signal})`);
  try {
    activeSocket?.end(new Error(signal));
  } catch {
    // Process shutdown must continue even if the socket is already closed.
  }
  process.exit(0);
}

process.on('unhandledRejection', (reason) => {
  console.error('[WA] Unhandled Rejection:', reason);
});

process.on('uncaughtException', (error) => {
  console.error('[WA] Uncaught Exception:', error);
});

process.once('SIGINT', () => shutdown('SIGINT'));
process.once('SIGTERM', () => shutdown('SIGTERM'));

startBot().catch(() => {
  console.error('[WA] Initial connection setup failed');
  scheduleReconnect();
});

