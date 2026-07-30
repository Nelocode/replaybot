"""
Panel de Administración — Bot AutoReply Comercial
Flask webapp para gestionar mensajes y audios del bot.
"""
import os
import json
import hashlib
import hmac
import shutil
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from flask import (
    Flask,
    jsonify,
    make_response,
    render_template_string,
    request,
    send_from_directory,
    session,
)
from telegram_auth import TelegramAuthManager
from message_schema import load_message_file
from operator_admin_recovery import (
    OperatorAdminRecoveryGuard,
    operator_key_matches,
    valid_configured_operator_key,
)
from telegram_audio_branding import resolve_audio_branding, save_audio_branding_settings
from test_mode import (
    interaction_state_summary,
    load_test_mode,
    reset_latest_interaction,
    save_test_mode,
)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audios"
MESSAGES_FILE = DATA_DIR / "messages.json"
DEFAULT_MESSAGES_FILE = BASE_DIR / "messages.json"
WA_CALL_HEALTH_FILE = DATA_DIR / "wa_call_health.json"
TG_SESSION_BASE = DATA_DIR / "tg_session"
TG_SWITCH_SESSION_BASE = DATA_DIR / "tg_switch_session"
TG_SWITCH_ROLLBACK_DIR = DATA_DIR / ".tg_switch_rollback"
WA_AUTH_DIR = DATA_DIR / "wa_auth"
WA_IDENTITY_FILE = DATA_DIR / "wa_identity.json"
WA_SWITCH_DIR = DATA_DIR / "wa_switch"
WA_SWITCH_AUTH_DIR = WA_SWITCH_DIR / "candidate_auth"
WA_SWITCH_QR_FILE = WA_SWITCH_DIR / "qr.png"
WA_SWITCH_HEALTH_FILE = WA_SWITCH_DIR / "health.json"
WA_SWITCH_IDENTITY_FILE = WA_SWITCH_DIR / "identity.json"
WA_SWITCH_PID_FILE = WA_SWITCH_DIR / "worker.pid"
WA_SWITCH_OPERATION_FILE = WA_SWITCH_DIR / "operation.json"
WA_SWITCH_RECOVERY_ROOT = DATA_DIR / ".wa_switch_recovery"
PANEL_ADMIN_ACCESS_DIR = DATA_DIR / "panel_admin_access"
OPERATOR_ADMIN_ACCESS_DIR = PANEL_ADMIN_ACCESS_DIR / "operator"
TG_AUDIO_BRANDING_DEFAULTS_FILE = BASE_DIR / "telegram_audio_branding.defaults.json"
TG_AUDIO_BRANDING_SETTINGS_FILE = DATA_DIR / "telegram_audio_branding.json"
TG_INTERACTION_STATE_FILE = DATA_DIR / "tg_interaction_state.json"
WA_INTERACTION_STATE_FILE = DATA_DIR / "wa_interaction_state.json"
TEST_MODE_FILE = DATA_DIR / "test_mode.json"
TEST_MODE_BACKUP_DIR = DATA_DIR / "test_mode_backups"
WA_SWITCH_TIMEOUT_SECONDS = 180
PANEL_ADMIN_RECOVERY_KEY_ENV = "PANEL_ADMIN_RECOVERY_KEY"
OPERATOR_ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

def _load_or_create_flask_secret() -> str:
    configured = os.environ.get("FLASK_SECRET")
    if configured and len(configured) >= 32:
        return configured
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = DATA_DIR / ".flask_secret"
    try:
        saved = secret_file.read_text(encoding="ascii").strip()
        if len(saved) >= 64:
            return saved
    except OSError:
        pass
    generated = secrets.token_hex(32)
    temp = secret_file.with_suffix(".tmp")
    temp.write_text(generated, encoding="ascii")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, secret_file)
    return generated


app = Flask(__name__)
APP_SECRET = _load_or_create_flask_secret()
app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") != "0",
)
app.permanent_session_lifetime = timedelta(days=365)
_operator_admin_recovery = OperatorAdminRecoveryGuard(
    OPERATOR_ADMIN_ACCESS_DIR,
    APP_SECRET,
)

# ── Telethon auth state (para flujo interactivo desde el panel) ──────
# Conserva un solo event loop entre send_code_request, sign_in y 2FA.
_telegram_auth = TelegramAuthManager(str(TG_SESSION_BASE))
_telegram_switch_auth = TelegramAuthManager(str(TG_SWITCH_SESSION_BASE))
_telegram_switch_lock = threading.RLock()
_wa_process_lock = threading.RLock()
_wa_switch_lock = threading.RLock()
_test_mode_lock = threading.RLock()
_wa_switch_expiry_timer: threading.Timer | None = None

# ── HTML Template (todo en uno para portabilidad) ───────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot AutoReply — Admin</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
<style>
body { background: #0f0f1a; color: #e8e8f0; font-family: system-ui, -apple-system, sans-serif; }
.card { background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 12px; margin-bottom: 1.5rem; }
.card-header { background: #16213e; border-bottom: 1px solid #2a2a4a; font-weight: 600; color: #c8d8f0; border-radius: 12px 12px 0 0 !important; }
.form-control, .form-select { background: #12122a; border: 1px solid #3a3a5a; color: #e8e8f0; }
.form-control:focus { background: #1a1a32; border-color: #5a9af0; color: #ffffff; box-shadow: 0 0 0 0.2rem rgba(90,154,240,0.2); }
.form-control::placeholder { color: #6a6a8a; }
.btn-primary { background: #4a90d9; border: none; color: #fff; font-weight: 500; }
.btn-primary:hover { background: #357abd; }
.btn-danger { background: #e74c3c; border: none; color: #fff; }
.btn-success { background: #27ae60; border: none; color: #fff; }
.btn-outline-light { border-color: #3a3a5a; color: #c8c8e0; }
.btn-outline-light:hover { background: #2a2a4a; border-color: #5a5a7a; color: #ffffff; }
.language-tabs { display: flex; gap: 8px; margin-bottom: 1rem; }
.language-tabs .btn { flex: 1; }
.badge-es { background: #e67e22; color: #fff; }
.badge-en { background: #3498db; color: #fff; }
.badge-fr { background: #9b59b6; color: #fff; }
.status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.status-online { background: #27ae60; }
.status-offline { background: #e74c3c; }
.step-label { font-size: 0.85rem; color: #8a8aaa; text-transform: uppercase; letter-spacing: 1px; }
textarea { min-height: 60px; resize: vertical; background: #12122a; border: 1px solid #3a3a5a; color: #e8e8f0; }
hr { border-color: #2a2a4a; }
.text-muted { color: #8a8aaa !important; }
#toast-container { position: fixed; top: 20px; right: 20px; z-index: 9999; }
.toast-msg { background: #1a1a2e; color: #e8e8f0; border: 1px solid #2a2a4a; padding: 12px 20px; border-radius: 8px; margin-bottom: 8px; animation: fadeIn 0.3s; }
.toast-msg.success { border-left: 4px solid #27ae60; }
.toast-msg.error { border-left: 4px solid #e74c3c; }
@keyframes fadeIn { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }
summary { color: #5a9af0; cursor: pointer; }
code { background: #12122a; color: #e8a0d0; padding: 2px 6px; border-radius: 4px; }
.badge.bg-secondary { background: #2a2a4a !important; color: #b0b0d0 !important; }
label { color: #c8c8e0 !important; font-weight: 500; }
.label-text { color: #c8c8e0; }
.card-body .small, .card-body small { color: #9a9ab0 !important; }
</style>
</head>
<body>
<div class="container py-4">
  <!-- Header -->
  <div class="d-flex justify-content-between align-items-center mb-4">
    <div>
      <h1 class="h3 mb-0">🤖 Bot AutoReply</h1>
      <small class="text-muted">Panel de administración — los cambios se guardan al instante</small>
    </div>
    <div class="d-flex align-items-center gap-3">
      <div class="d-flex flex-column align-items-end" style="gap: 4px;">
        <span id="bot-status" class="badge bg-secondary" style="font-size:0.75rem;">📱 TG User: Verificando...</span>
        <span id="bf-status" class="badge bg-secondary" style="font-size:0.75rem;">🤖 BotFather: Verificando...</span>
        <span id="wa-status" class="badge bg-secondary" style="font-size:0.75rem;">💬 WA: Verificando...</span>
      </div>
      <button class="btn btn-sm btn-outline-light" onclick="showSetup()">⚙️ Configurar</button>
      <button class="btn btn-sm btn-outline-light" onclick="restartBot()">🔄 Reiniciar servicio TG</button>
      <button id="restart-wa-btn" class="btn btn-sm btn-outline-light" onclick="restartWaBot()">🔄 Reiniciar servicio WA</button>
    </div>
  </div>

  <!-- Setup Modal -->
  <div id="setup-modal" class="card" style="display:none;">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>⚙️ Configuración — Vinculación de canales</span>
      <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('setup-modal').style.display='none'">✕</button>
    </div>
    <div class="card-body">

      <!-- ── Telegram ── -->
      <h6 class="mb-2">📱 Telegram (User Bot)</h6>
      <details id="tg-credentials-help" class="mb-2" style="display:none;">
        <summary class="text-muted small" style="cursor:pointer;">📖 ¿Cómo obtener las credenciales?</summary>
        <ol class="small mt-2" style="padding-left:1.5rem;">
          <li>Ve a <a href="https://my.telegram.org/apps" target="_blank" style="color:#6ea8fe;">my.telegram.org/apps</a></li>
          <li>Inicia sesión con el número de teléfono de la cuenta</li>
          <li>Crea una aplicación (nombre cualquiera, plataforma "Desktop")</li>
          <li>Copia el <strong>api_id</strong> y <strong>api_hash</strong> de abajo</li>
        </ol>
      </details>
      <div id="tg-initial-link" style="display:none;">
        <div class="row mb-2">
          <div class="col-3">
            <input id="tg-api-id" type="number" class="form-control form-control-sm" placeholder="api_id" style="font-family:monospace;font-size:0.8rem;">
          </div>
          <div class="col-5">
            <input id="tg-api-hash" type="password" autocomplete="new-password" class="form-control form-control-sm" placeholder="api_hash" style="font-family:monospace;font-size:0.8rem;">
          </div>
          <div class="col-4">
            <input id="tg-phone" type="text" class="form-control form-control-sm" placeholder="+57 300 123 4567" style="font-family:monospace;font-size:0.8rem;">
          </div>
        </div>
        <div class="d-flex align-items-center gap-2 mb-2">
          <button id="tg-link-btn" class="btn btn-sm btn-primary" onclick="linkTelegram()">🔗 Vincular</button>
        </div>
      </div>
      <div id="tg-linked-actions" style="display:none;" class="p-2 mb-2 rounded" >
        <div id="tg-account-summary" class="small mb-2"></div>
        <button id="tg-switch-open-btn" class="btn btn-sm btn-primary" onclick="showTelegramSwitch()">🔁 Cambiar cuenta</button>
      </div>
      <div id="admin-access" style="display:none;" class="p-3 mb-3 rounded border border-warning">
        <h6 class="mb-1">🔐 Acceso administrativo</h6>
        <p class="small text-muted mb-2">
          Telegram puede estar vinculado a la cuenta del cliente. Para administrar los canales desde este
          navegador, usa la clave privada del operador; no necesitas los datos ni el acceso a su Telegram.
        </p>
        <label for="admin-operator-key" class="small mb-1">Clave administrativa</label>
        <div class="d-flex flex-wrap gap-2">
          <input id="admin-operator-key" type="password" autocomplete="off" maxlength="512"
                 class="form-control form-control-sm" placeholder="Clave administrativa"
                 aria-describedby="admin-access-status" style="max-width:440px;">
          <button id="admin-access-recover-btn" class="btn btn-sm btn-primary"
                  onclick="recoverOperatorAdminAccess()">🔓 Recuperar acceso</button>
        </div>
        <div id="admin-access-status" class="small text-muted mt-2" role="status" aria-live="polite"></div>
      </div>
      <div id="tg-switch-section" style="display:none;" class="p-2 mb-2 rounded">
        <p class="small text-muted mb-2">La cuenta actual seguirá atendiendo hasta verificar la nueva. Reutilizaremos el api_id y api_hash guardados.</p>
        <div class="d-flex gap-2">
          <input id="tg-switch-phone" type="text" class="form-control form-control-sm" placeholder="Nuevo teléfono, por ejemplo +34..." style="font-family:monospace;">
          <button id="tg-switch-btn" class="btn btn-sm btn-success" onclick="startTelegramSwitch()">📨 Enviar código</button>
          <button class="btn btn-sm btn-outline-light" onclick="hideTelegramSwitch()">Cancelar</button>
        </div>
      </div>
      <div id="tg-link-status" class="small text-muted mb-3"></div>

      <!-- ── Código de verificación TG (oculto hasta que se necesite) ── -->
      <div id="tg-code-section" style="display:none;" class="mb-3 p-2" >
        <h6 class="mb-1">📨 Código de verificación</h6>
        <p id="tg-code-help" class="small text-muted mb-2">Revisa el canal que Telegram indicó y escribe aquí el código más reciente.</p>
        <div class="d-flex gap-2">
          <input id="tg-code-input" type="text" class="form-control form-control-sm" placeholder="Código" maxlength="32" style="font-family:monospace;font-size:1.1rem;letter-spacing:4px;text-align:center;flex:1;">
          <button class="btn btn-sm btn-success" onclick="verifyTgCode()">✅ Verificar</button>
          <button class="btn btn-sm btn-outline-light" onclick="cancelTgAuth()">✕ Cancelar</button>
        </div>
        <div id="tg-code-status" class="small text-muted mt-2"></div>

        <!-- 2FA password (oculto hasta que se necesite) -->
        <div id="tg-2fa-section" style="display:none;" class="mt-3">
          <p class="small text-muted mb-2">Esta cuenta tiene verificación en dos pasos. Ingresa tu contraseña.</p>
          <div class="d-flex gap-2">
            <input id="tg-password-input" type="password" class="form-control form-control-sm" placeholder="Contraseña de 2FA" style="flex:1;">
            <button class="btn btn-sm btn-success" onclick="verifyTgPassword()">✅ Verificar</button>
          </div>
          <div id="tg-password-status" class="small text-muted mt-1"></div>
        </div>
      </div>

      <!-- ── Marca de los audios TG ── -->
      <hr class="my-3">
      <h6 class="mb-2">🎵 Presentación de los audios en Telegram</h6>
      <p class="small text-muted mb-2">
        Edita los textos que aparecen en la tarjeta del audio. El siguiente envío
        usará el cambio sin reiniciar Telegram y la configuración se conservará.
      </p>
      <div class="row g-2">
        <div class="col-md-6">
          <label for="tg-audio-performer" class="form-label small">Nombre de la agencia</label>
          <input id="tg-audio-performer" type="text" maxlength="80"
                 class="form-control form-control-sm"
                 placeholder="Caché Madrid"
                 oninput="markTelegramAudioBrandingDirty()">
        </div>
        <div class="col-md-6">
          <label for="tg-audio-title" class="form-label small">Título del audio</label>
          <input id="tg-audio-title" type="text" maxlength="80"
                 class="form-control form-control-sm"
                 placeholder="Las Fiesteras"
                 oninput="markTelegramAudioBrandingDirty()">
        </div>
      </div>
      <div class="mt-2">
        <button id="tg-audio-branding-save" class="btn btn-sm btn-success"
                onclick="saveTelegramAudioBranding()" disabled>💾 Guardar textos</button>
      </div>
      <div id="tg-audio-branding-preview" class="small text-muted mt-2"></div>
      <div id="tg-audio-branding-status" class="small text-muted mt-1"></div>

      <!-- ── BotFather (Bot API tradicional) ── -->
      <hr class="my-3">
      <h6 class="mb-2">🤖 BotFather (modo prueba/revisión)</h6>
      <details class="mb-2">
        <summary class="text-muted small" style="cursor:pointer;">📖 ¿Cómo crear un bot en BotFather?</summary>
        <ol class="small mt-2" style="padding-left:1.5rem;">
          <li>Abre Telegram y busca <strong>@BotFather</strong></li>
          <li>Envía <code>/newbot</code> y sigue las instrucciones</li>
          <li>BotFather te dará un <strong>token</strong> (ej: <code>123456789:ABCdefGHIjkl...</code>)</li>
          <li>Pega ese token abajo</li>
        </ol>
      </details>
      <div class="d-flex align-items-center gap-2 mb-2">
        <input id="bf-token-input" type="text" class="form-control form-control-sm" placeholder="Token de BotFather" style="flex:1;font-family:monospace;font-size:0.8rem;">
        <button class="btn btn-sm btn-outline-light" onclick="linkBotFather()">🔗 Vincular</button>
      </div>
      <div id="bf-link-status" class="small text-muted mb-2"></div>

      <!-- ── WhatsApp ── -->
      <h6 class="mb-2">💬 WhatsApp</h6>
      <details class="mb-2">
        <summary class="text-muted small" style="cursor:pointer;">📖 ¿Cómo vincular WhatsApp?</summary>
        <ol class="small mt-2" style="padding-left:1.5rem;">
          <li>Haz click en <strong>"Vincular"</strong> o <strong>"Cambiar cuenta"</strong></li>
          <li>El código QR aparecerá automáticamente</li>
          <li>Abre WhatsApp en tu teléfono</li>
          <li>Ve a <strong>3 puntos > Dispositivos vinculados > Vincular dispositivo</strong></li>
          <li>Escanea el QR que aparece en pantalla</li>
          <li>El panel confirmará automáticamente cuando la cuenta esté lista</li>
        </ol>
      </details>
      <div id="wa-account-summary" class="small mb-2"></div>
      <div class="d-flex align-items-center gap-2">
        <button id="wa-switch-btn" class="btn btn-sm btn-primary" onclick="startWaSwitch()">📲 Vincular WhatsApp</button>
      </div>
      <div id="wa-link-status" class="small text-muted mt-2"></div>

      <!-- ── Servidor (futuro deploy) ── -->
      <hr class="my-3">
      <details>
        <summary class="text-muted small" style="cursor:pointer;">🌐 ¿Subir a un servidor?</summary>
        <div class="small mt-2 text-muted">
          <p>Este panel funciona en local. Para producción en un VPS:</p>
          <ol style="padding-left:1.5rem;">
            <li>Copia la carpeta <code>bot-autoreply</code> al servidor</li>
            <li>Ejecuta el script <code>deploy.sh</code> incluido</li>
            <li>Los 3 servicios (TG, WA, Panel) arrancan solos con systemd</li>
            <li>Usa Nginx + Certbot para HTTPS y dominio personalizado</li>
          </ol>
        </div>
      </details>

    </div>
  </div>

  <!-- Test mode -->
  <div id="test-mode-card" class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>🧪 Modo de prueba de conversaciones</span>
      <span id="test-mode-badge" class="badge bg-secondary">Desactivado</span>
    </div>
    <div class="card-body">
      <p class="small text-muted mb-2">
        Permite que el mismo celular vuelva a recibir Paso 1 y pruebe otro idioma o escenario.
        El panel no muestra ni guarda el número: reinicia la conversación más reciente de cada
        canal sin desvincular Telegram ni WhatsApp.
      </p>
      <p class="small text-warning mb-2">
        Úsalo sin tráfico real simultáneo: “más reciente” se determina por la última interacción.
      </p>
      <p id="test-mode-summary" class="small mb-3">Verifica este navegador para consultar el estado.</p>
      <div class="d-flex align-items-center gap-2 mb-3">
        <label for="test-mode-language" class="small mb-0">Idioma al volver a Paso 1:</label>
        <select id="test-mode-language" class="form-select form-select-sm" style="max-width:220px;">
          <option value="auto">Detectar por el próximo texto</option>
          <option value="es">Español</option>
          <option value="en">English</option>
          <option value="fr">Français</option>
        </select>
      </div>
      <div class="d-flex flex-wrap gap-2">
        <button id="test-mode-toggle" class="btn btn-sm btn-outline-light" onclick="toggleTestMode()" disabled>
          Activar modo de prueba
        </button>
        <button class="btn btn-sm btn-primary test-reset-button" onclick="resetTestConversation('telegram')" disabled>
          Reiniciar última de Telegram
        </button>
        <button class="btn btn-sm btn-primary test-reset-button" onclick="resetTestConversation('whatsapp')" disabled>
          Reiniciar última de WhatsApp
        </button>
        <button class="btn btn-sm btn-danger test-reset-button" onclick="resetTestConversation('both')" disabled>
          Reiniciar ambas
        </button>
      </div>
      <div id="test-mode-status" class="small text-muted mt-2"></div>
    </div>
  </div>

  <!-- WhatsApp QR (hidden by default) -->
  <div id="wa-qr-card" class="card" style="display:none;">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>💬 Vincular WhatsApp</span>
      <button class="btn btn-sm btn-outline-light wa-switch-cancel" onclick="cancelWaSwitch()">✕</button>
    </div>
    <div class="card-body text-center py-4">
      <p id="wa-qr-help" class="mb-3">Preparando un QR seguro…</p>
      <img id="wa-qr-img" alt="QR WhatsApp" style="visibility:hidden;display:inline-block;width:300px;height:300px;border-radius:12px;background:#fff;padding:8px;" class="mb-3">
      <p class="text-muted small">WhatsApp > 3 puntos > Dispositivos vinculados</p>
      <div class="mt-2">
        <button class="btn btn-sm btn-outline-light wa-switch-cancel" onclick="cancelWaSwitch()">✕ Cancelar cambio</button>
      </div>
    </div>
  </div>

  <!-- Language Tabs -->
  <div class="language-tabs" id="lang-tabs">
    <button class="btn btn-outline-light active" data-lang="es" onclick="switchLang('es')">
      <span class="badge badge-es">ES</span> Español
    </button>
    <button class="btn btn-outline-light" data-lang="en" onclick="switchLang('en')">
      <span class="badge badge-en">EN</span> English
    </button>
    <button class="btn btn-outline-light" data-lang="fr" onclick="switchLang('fr')">
      <span class="badge badge-fr">FR</span> Français
    </button>
  </div>

  <!-- Steps Container -->
  <div id="steps-container"></div>

  <!-- Info Footer -->
  <div class="mt-4 text-center text-muted" style="font-size:0.85rem;">
    <span>📁 <code>messages.json</code></span>
    <span class="mx-2">·</span>
    <span>🎵 Audios en <code>audios/</code></span>
    <span class="mx-2">·</span>
    <span>🔄 Cambios toman efecto al reiniciar el bot</span>
  </div>
</div>

<!-- Toast Container -->
<div id="toast-container"></div>

<script>
let currentLang = "es";
const LANG_NAMES = {"es":"Español","en":"English","fr":"Français"};
const LANG_CODES = {"es":"ES","en":"EN","fr":"FR"};
let channelCsrf = null;
let channelState = null;
let tgAuthMode = sessionStorage.getItem("tg_auth_mode") || "link";
let waSwitchPollTimer = null;
let waSwitchPolling = false;
let waCommitInFlight = false;
let waSwitchGeneration = 0;
let waSwitchPollAbortController = null;
let waQrLoadGeneration = 0;
let channelStateRequest = null;
let adminRecoveryCooldownTimer = null;
let tgAudioBrandingDirty = false;
let testModeState = null;

function toast(msg, type="success") {
  const c = document.getElementById("toast-container");
  const d = document.createElement("div");
  d.className = "toast-msg " + type;
  d.textContent = msg;
  c.appendChild(d);
  setTimeout(() => d.remove(), 3000);
}

async function loadData() {
  try {
    const r = await fetch("/api/data", {cache: "no-store"});
    const data = await r.json();
    renderSteps(data);
    renderTelegramAudioBranding(data.telegram_audio_branding || {});
    updateBotStatus(data.bot_running);
    updateBfStatus(data.bf_running);
    updateWaStatus(data.wa_running);
  } catch(e) {
    toast("Error cargando datos: " + e.message, "error");
  }
}

function renderTelegramAudioBranding(branding) {
  const performerInput = document.getElementById("tg-audio-performer");
  const titleInput = document.getElementById("tg-audio-title");
  const preview = document.getElementById("tg-audio-branding-preview");
  if (!performerInput || !titleInput || !preview) return;
  if (tgAudioBrandingDirty) {
    renderTelegramAudioBrandingPreview(
      performerInput.value.trim(),
      titleInput.value.trim()
    );
    return;
  }
  const performer = String(branding.performer || "");
  const title = String(branding.title || "Las Fiesteras");
  performerInput.value = performer;
  titleInput.value = title;
  renderTelegramAudioBrandingPreview(performer, title);
}

function renderTelegramAudioBrandingPreview(performer, title) {
  const preview = document.getElementById("tg-audio-branding-preview");
  if (!preview) return;
  preview.textContent = performer && title
    ? `Vista previa: ${performer} — ${title}`
    : "Completa ambos textos para ver cómo aparecerán en Telegram.";
}

function markTelegramAudioBrandingDirty() {
  tgAudioBrandingDirty = true;
  const performerInput = document.getElementById("tg-audio-performer");
  const titleInput = document.getElementById("tg-audio-title");
  renderTelegramAudioBrandingPreview(
    performerInput ? performerInput.value.trim() : "",
    titleInput ? titleInput.value.trim() : ""
  );
}

async function saveTelegramAudioBranding() {
  if (!requirePanelAdmin()) return;
  const performerInput = document.getElementById("tg-audio-performer");
  const titleInput = document.getElementById("tg-audio-title");
  const button = document.getElementById("tg-audio-branding-save");
  const status = document.getElementById("tg-audio-branding-status");
  const performer = performerInput.value.trim();
  const title = titleInput.value.trim();
  if (!performer || !title) {
    toast("Completa el nombre de la agencia y el título del audio.", "error");
    (!performer ? performerInput : titleInput).focus();
    return;
  }

  button.disabled = true;
  status.textContent = "Guardando…";
  try {
    const response = await fetch("/api/telegram_audio_branding", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({title, performer})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "No fue posible guardar los textos");
    tgAudioBrandingDirty = false;
    renderTelegramAudioBranding(data);
    status.textContent = "✅ Se aplicará al próximo audio de Telegram.";
    toast("Textos de los audios actualizados", "success");
  } catch(error) {
    status.textContent = "❌ " + error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = !(channelState && channelState.can_manage);
  }
}

function renderSteps(data) {
  const lang = currentLang;
  const langData = data.messages[lang];
  if (!langData) return;

  let html = `<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span><span class="badge badge-${lang}">${LANG_CODES[lang]}</span> ${LANG_NAMES[lang]} — 2 pasos fijos</span>
      <div>
        <button class="btn btn-sm btn-outline-primary" onclick="previewLang('${lang}')">👁 Vista previa</button>
      </div>
    </div>
    <div class="card-body">`;

  langData.steps.slice(0, 2).forEach((step, i) => {
    const audioPath = `/api/audio/${step.audio}`;
    html += `
    <div class="mb-4 p-3" style="background:#141428; border-radius:8px;" data-step="${i}">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="d-flex align-items-center gap-2">
          <span class="step-label">Paso ${i+1}</span>
          <span class="small text-muted">${i === 0
            ? 'Primera interacción si no fue llamada'
            : '🔁 Loop desde la interacción 2, incluidas llamadas'}</span>
        </div>
      </div>
      <div class="row g-3">
        <div class="col-md-8">
          <label class="form-label small">Texto del mensaje</label>
          <textarea class="form-control" onchange="saveStepText('${lang}', ${i}, this.value)">${escapeHtml(step.text)}</textarea>
        </div>
        <div class="col-md-4">
          <label class="form-label small">Audio</label>
          <div class="audio-preview mb-2">
            <audio controls src="${audioPath}"></audio>
            <span class="small text-muted">${step.audio}</span>
          </div>
          <div class="input-group input-group-sm">
            <input type="file" class="form-control form-control-sm" accept="audio/mpeg,audio/mp3" 
                   onchange="uploadAudio('${lang}', ${i}, this.files[0])">
          </div>
        </div>
      </div>
    </div>`;
  });

  html += `</div></div>`;

  // ── Sección de Llamada ──
  const callData = langData.call || { text: '📞 Llamada recibida', audio: '' };
  const callAudioPath = callData.audio ? `/api/audio/${callData.audio}` : '';
  html += `
  <div class="card mt-3">
    <div class="card-header">
      <span>📞 Llamada — mensaje para cuando alguien llama</span>
    </div>
    <div class="card-body">
      <div class="row g-3">
        <div class="col-md-8">
          <label class="form-label small">Texto de llamada</label>
          <textarea class="form-control" onchange="saveCallText('${lang}', this.value)">${escapeHtml(callData.text)}</textarea>
        </div>
        <div class="col-md-4">
          <label class="form-label small">Audio de llamada</label>
          ${callAudioPath ? `
          <div class="audio-preview mb-2">
            <audio controls src="${callAudioPath}"></audio>
            <span class="small text-muted">${callData.audio}</span>
          </div>` : '<div class="mb-2 small text-muted">🎵 Sin audio aún</div>'}
          <div class="input-group input-group-sm">
            <input type="file" class="form-control form-control-sm" accept="audio/mpeg,audio/mp3"
                   onchange="uploadCallAudio('${lang}', this.files[0])">
          </div>
        </div>
      </div>
    </div>
  </div>`;

  document.getElementById("steps-container").innerHTML = html;
}

function escapeHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function switchLang(lang) {
  currentLang = lang;
  document.querySelectorAll("#lang-tabs .btn").forEach(b => {
    b.classList.toggle("active", b.dataset.lang === lang);
    b.classList.toggle("btn-outline-light", b.dataset.lang !== lang);
    b.classList.toggle("btn-light", b.dataset.lang === lang);
  });
  loadData();
}

async function saveStepLoop(lang, step, loop) {
  if (!requirePanelAdmin()) return;
  await fetch("/api/messages", {
    method: "POST",
    headers: channelHeaders(),
    body: JSON.stringify({lang, step, loop, action: "edit_loop"})
  });
}

async function saveStepText(lang, step, text) {
  if (!requirePanelAdmin()) return;
  try {
    const r = await fetch("/api/messages", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({lang, step, text, action: "edit_text"})
    });
    const resp = await r.json();
    if (resp.ok) toast("✅ Paso "+(step+1)+" guardado");
    else toast("❌ Error: " + resp.error, "error");
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
  }
}

async function uploadAudio(lang, step, file) {
  if (!file) return;
  if (!requirePanelAdmin()) return;
  await uploadFileChunked(file, lang, {step, type: "step"});
}

async function uploadCallAudio(lang, file) {
  if (!file) return;
  if (!requirePanelAdmin()) return;
  await uploadFileChunked(file, lang, {type: "call"});
}

async function uploadFileChunked(file, lang, opts = {}) {
  const CHUNK_SIZE = 500 * 1024; // 500KB per chunk
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE);

  for (let i = 0; i < totalChunks; i++) {
    const chunk = file.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
    const fd = new FormData();
    fd.append("chunk", chunk, file.name);
    fd.append("chunk_index", i);
    fd.append("total_chunks", totalChunks);
    fd.append("lang", lang);
    if (opts.step !== undefined) fd.append("step", opts.step);
    fd.append("type", opts.type || "step");

    try {
      const r = await fetch("/api/upload_chunk", {
        method: "POST",
        headers: channelUploadHeaders(),
        body: fd
      });
      const resp = await r.json();
      if (!resp.ok) {
        toast("❌ Chunk " + (i+1) + "/" + totalChunks + ": " + resp.error, "error");
        return;
      }
    } catch(e) {
      toast("❌ Error chunk " + (i+1) + ": " + e.message, "error");
      return;
    }
  }

  // All chunks uploaded — assemble
  const body = JSON.stringify({
    lang, type: opts.type,
    step: opts.step,
    total_chunks: totalChunks,
    original_name: file.name
  });

  try {
    const r = await fetch("/api/upload_assemble", {
      method: "POST",
      headers: channelHeaders(),
      body
    });
    const resp = await r.json();
    if (resp.ok) {
      toast("🎵 Audio subido: " + resp.filename);
      loadData();
    } else {
      toast("❌ Error: " + resp.error, "error");
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
  }
}

async function addStep(lang) {
  if (!requirePanelAdmin()) return;
  try {
    const r = await fetch("/api/messages", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({lang, action: "add_step"})
    });
    const resp = await r.json();
    if (resp.ok) { toast("➕ Paso añadido"); loadData(); }
    else toast("❌ Error: " + resp.error, "error");
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
  }
}

async function removeStep(lang, step) {
  if (!requirePanelAdmin()) return;
  if (!confirm("¿Eliminar paso " + (step+1) + " de " + LANG_NAMES[lang] + "?")) return;
  try {
    const r = await fetch("/api/messages", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({lang, step, action: "remove_step"})
    });
    const resp = await r.json();
    if (resp.ok) { toast("🗑 Paso eliminado"); loadData(); }
    else toast("❌ Error: " + resp.error, "error");
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
  }
}

function previewLang(lang) {
  // Pequeña simulación de cómo se ve desde Telegram
  window.open("/preview/" + lang, "_blank", "width=400,height=600");
}

async function saveCallText(lang, text) {
  if (!requirePanelAdmin()) return;
  try {
    const r = await fetch("/api/messages", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({lang, text, action: "edit_call_text"})
    });
    const resp = await r.json();
    if (resp.ok) toast("📞 Mensaje de llamada guardado");
    else toast("❌ Error: " + resp.error, "error");
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
  }
}

async function restartBot() {
  const latest = await loadChannelState();
  if (!latest || !latest.can_manage) {
    guideChannelAdminRecovery();
    return;
  }
  if (!confirm("¿Reiniciar el bot de Telegram? (toma efecto inmediato)")) return;
  toast("🔄 Reiniciando bot TG...", "success");
  try {
    const r = await fetch("/api/restart_bot", {
      method: "POST",
      headers: channelHeaders()
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      toast("❌ " + (d.error || "No fue posible reiniciar Telegram"), "error");
      return;
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
    return;
  }
  let attempts = 0;
  const check = setInterval(async () => {
    attempts++;
    try {
      const r = await fetch("/api/status");
      const d = await r.json();
      if (d.bot_running) {
        clearInterval(check);
        toast("✅ Bot TG reiniciado y online", "success");
        updateBotStatus(true);
      } else if (attempts >= 6) {
        clearInterval(check);
        toast("⚠️ Restart TG lanzado, verifica en el bot", "error");
      }
    } catch { if (attempts >= 6) { clearInterval(check); } }
  }, 2000);
}

async function restartWaBot() {
  const latest = await loadChannelState();
  if (!latest) {
    toast("❌ No fue posible consultar el estado de WhatsApp", "error");
    return;
  }
  if (!latest.can_manage) {
    guideChannelAdminRecovery();
    return;
  }
  if (latest.whatsapp && latest.whatsapp.reauth_required) {
    toast("⚠️ WhatsApp cerró la sesión; necesitas un QR nuevo", "error");
    await startWaSwitch(latest);
    return;
  }
  if (!confirm("¿Reiniciar el servicio de WhatsApp? La cuenta vinculada se conservará.")) return;
  toast("🔄 Reiniciando servicio WA...", "success");
  try {
    const r = await fetch("/api/restart_wa_bot", {
      method: "POST",
      headers: channelHeaders()
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      toast("❌ " + (d.error || "No fue posible reiniciar WhatsApp"), "error");
      return;
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
    return;
  }
  let attempts = 0;
  const check = setInterval(async () => {
    attempts++;
    try {
      const r = await fetch("/api/status");
      const d = await r.json();
      if (d.wa_running) {
        clearInterval(check);
        toast("✅ WhatsApp reiniciado; la cuenta sigue vinculada", "success");
        updateWaStatus(true);
      } else if (attempts >= 6) {
        clearInterval(check);
        toast("⚠️ El servicio arrancó, pero todavía no confirma conexión", "error");
      }
    } catch { if (attempts >= 6) { clearInterval(check); } }
  }, 2000);
}

async function updateBotStatus(running) {
  const el = document.getElementById("bot-status");
  if (running === true) {
    el.className = "badge bg-success";
    el.innerHTML = '📱 TG User: <span class="status-dot status-online"></span> Online';
  } else if (running === false) {
    el.className = "badge bg-danger";
    el.innerHTML = '📱 TG User: <span class="status-dot status-offline"></span> Offline';
  } else {
    el.className = "badge bg-secondary";
    el.innerHTML = '📱 TG User: Incierto';
  }
}

async function updateBfStatus(running) {
  const el = document.getElementById("bf-status");
  if (running === true) {
    el.className = "badge bg-success";
    el.innerHTML = '🤖 BotFather: <span class="status-dot status-online"></span> Online';
  } else if (running === false) {
    el.className = "badge bg-secondary";
    el.innerHTML = '🤖 BotFather: No configurado';
  } else {
    el.className = "badge bg-secondary";
    el.innerHTML = '🤖 BotFather: Verificando...';
  }
}

async function updateWaStatus(running) {
  const el = document.getElementById("wa-status");
  const qrCard = document.getElementById("wa-qr-card");
  if (running === true) {
    el.className = "badge bg-success";
    el.innerHTML = '💬 WA: <span class="status-dot status-online"></span> Online';
    if (!waSwitchPolling) qrCard.style.display = 'none';
  } else if (running === false) {
    el.className = "badge bg-danger";
    el.innerHTML = '💬 WA: <span class="status-dot status-offline"></span> Offline';
    if (!waSwitchPolling) qrCard.style.display = 'none';
  } else if (running === null) {
    el.className = "badge bg-warning text-dark";
    el.innerHTML = '💬 WA: <span class="status-dot" style="background:#f39c12;"></span> No vinculado';
  } else {
    el.className = "badge bg-secondary";
    el.innerHTML = '💬 WA: Incierto';
    if (!waSwitchPolling) qrCard.style.display = 'none';
  }
}

function channelHeaders() {
  const headers = {"Content-Type": "application/json"};
  if (channelCsrf) headers["X-Channel-CSRF"] = channelCsrf;
  return headers;
}

function channelUploadHeaders() {
  const headers = {};
  if (channelCsrf) headers["X-Channel-CSRF"] = channelCsrf;
  return headers;
}

function requirePanelAdmin() {
  if (channelState && channelState.can_manage) return true;
  guideChannelAdminRecovery();
  return false;
}

function safeAccountLabel(account, fallback) {
  const parts = [];
  if (account && account.display_name) parts.push(account.display_name);
  if (account && account.username) parts.push("@" + account.username);
  if (account && account.phone_hint) parts.push(account.phone_hint);
  return parts.length ? parts.join(" · ") : fallback;
}

function guideChannelAdminRecovery() {
  document.getElementById("setup-modal").style.display = "block";
  const telegram = channelState && channelState.telegram ? channelState.telegram : {};
  document.getElementById("tg-credentials-help").style.display = "none";
  document.getElementById("tg-initial-link").style.display = "none";
  document.getElementById("tg-linked-actions").style.display = "none";
  const panel = document.getElementById("admin-access");
  panel.style.display = "block";
  document.getElementById("tg-link-status").innerHTML =
    telegram.linked
      ? "🔐 Telegram sigue conectado. Usa la <strong>clave administrativa</strong> del operador para habilitar este navegador."
      : "🔐 Usa la <strong>clave administrativa</strong> del operador antes de configurar Telegram.";
  requestAnimationFrame(() => {
    panel.scrollIntoView({behavior: "smooth", block: "center"});
    document.getElementById("admin-operator-key").focus();
  });
  toast("🔐 Introduce la clave administrativa del operador", "error");
}

function setAdminAccessStatus(message, type="muted") {
  const status = document.getElementById("admin-access-status");
  status.className = "small mt-2 " + (type === "error" ? "text-danger" : type === "success" ? "text-success" : "text-muted");
  status.textContent = message;
}

function setAdminRecoveryCooldown(retryAfter) {
  if (adminRecoveryCooldownTimer) clearInterval(adminRecoveryCooldownTimer);
  let remaining = Math.max(1, Number.parseInt(retryAfter, 10) || 1);
  const button = document.getElementById("admin-access-recover-btn");
  const update = () => {
    button.disabled = remaining > 0;
    button.textContent = remaining > 0
      ? `Espera ${remaining}s`
      : "🔓 Recuperar acceso";
    if (remaining <= 0) {
      clearInterval(adminRecoveryCooldownTimer);
      adminRecoveryCooldownTimer = null;
      return;
    }
    remaining -= 1;
  };
  update();
  adminRecoveryCooldownTimer = setInterval(update, 1000);
}

async function recoverOperatorAdminAccess() {
  const input = document.getElementById("admin-operator-key");
  const button = document.getElementById("admin-access-recover-btn");
  const key = input.value;
  if (!key) {
    setAdminAccessStatus("Ingresa la clave administrativa.", "error");
    input.focus();
    return;
  }

  button.disabled = true;
  button.textContent = "⏳ Comprobando…";
  setAdminAccessStatus("Comprobando la clave administrativa…");
  try {
    const response = await fetch("/api/admin_access/operator", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({key})
    });
    const data = await response.json();
    if (data.csrf) channelCsrf = data.csrf;
    input.value = "";

    if (response.ok && data.ok && data.authorized) {
      setAdminAccessStatus("Acceso recuperado. Ya puedes administrar los canales.", "success");
      toast("✅ Acceso administrativo recuperado", "success");
      const latest = await loadChannelState(true);
      if (latest && latest.csrf) channelCsrf = latest.csrf;
      return;
    }

    if (data.error_code === "operator_recovery_unconfigured") {
      setAdminAccessStatus(
        "La clave administrativa todavía no está configurada. Pide al operador del sistema que la configure.",
        "error"
      );
    } else if (data.error_code === "invalid_operator_key") {
      setAdminAccessStatus("La clave administrativa no es correcta.", "error");
    } else if (data.error_code === "rate_limited" || response.status === 429) {
      setAdminAccessStatus("Hay demasiados intentos. Espera antes de volver a probar.", "error");
      setAdminRecoveryCooldown(data.retry_after);
      input.focus();
      return;
    } else if (data.error_code === "csrf_invalid") {
      setAdminAccessStatus("La sesión del panel venció. Actualiza la página e inténtalo nuevamente.", "error");
    } else {
      setAdminAccessStatus(data.error || "No fue posible recuperar el acceso.", "error");
    }
  } catch(error) {
    input.value = "";
    setAdminAccessStatus("No fue posible comprobar la clave. Revisa la conexión e inténtalo nuevamente.", "error");
  }
  button.disabled = false;
  button.textContent = "🔓 Recuperar acceso";
  input.focus();
}

function revealWaQrCard(resetImage=false) {
  const card = document.getElementById("wa-qr-card");
  const image = document.getElementById("wa-qr-img");
  card.style.display = "block";
  if (resetImage) {
    waQrLoadGeneration += 1;
    image.style.visibility = "hidden";
    image.removeAttribute("src");
    delete image.dataset.qrRevision;
    delete image.dataset.pendingQrRevision;
  }
  requestAnimationFrame(() => card.scrollIntoView({behavior: "smooth", block: "start"}));
}

function showWaQrImage(revision=null) {
  const image = document.getElementById("wa-qr-img");
  const requestedRevision = revision ? String(revision) : "unversioned";
  if (image.dataset.qrRevision === requestedRevision ||
      image.dataset.pendingQrRevision === requestedRevision) return;

  const loadGeneration = waQrLoadGeneration;
  image.dataset.pendingQrRevision = requestedRevision;
  const nextImage = new Image();
  nextImage.onload = () => {
    if (loadGeneration !== waQrLoadGeneration ||
        image.dataset.pendingQrRevision !== requestedRevision) return;
    image.src = nextImage.src;
    image.dataset.qrRevision = requestedRevision;
    delete image.dataset.pendingQrRevision;
    image.style.visibility = "visible";
  };
  nextImage.onerror = () => {
    if (loadGeneration !== waQrLoadGeneration ||
        image.dataset.pendingQrRevision !== requestedRevision) return;
    delete image.dataset.pendingQrRevision;
  };
  nextImage.src = "/api/switch_wa/qr?rev=" + encodeURIComponent(requestedRevision) +
    "&ts=" + Date.now();
}

function renderChannelState(data) {
  channelState = data;
  channelCsrf = data.csrf || channelCsrf;

  const tg = data.telegram || {};
  const tgInitial = document.getElementById("tg-initial-link");
  const tgLinked = document.getElementById("tg-linked-actions");
  const tgCredentialsHelp = document.getElementById("tg-credentials-help");
  const adminAccess = document.getElementById("admin-access");
  const tgSummary = document.getElementById("tg-account-summary");
  const tgSwitchOpen = document.getElementById("tg-switch-open-btn");
  const tgLinkButton = document.getElementById("tg-link-btn");
  tgSwitchOpen.disabled = !data.can_manage || tg.state === "recovery_required";
  adminAccess.style.display = data.can_manage ? "none" : "block";
  if (data.can_manage) {
    document.getElementById("admin-operator-key").value = "";
    setAdminAccessStatus("");
  } else if (!document.getElementById("admin-access-status").textContent.trim()) {
    setAdminAccessStatus("Introduce la clave administrativa entregada al operador.");
  }
  if (tg.linked) {
    tgCredentialsHelp.style.display = "none";
    tgInitial.style.display = "none";
    tgLinked.style.display = data.can_manage ? "block" : "none";
    tgLinkButton.innerHTML = "🔗 Vincular";
    tgSummary.textContent = "Cuenta actual: " + safeAccountLabel(tg, "Telegram vinculado");
    document.getElementById("tg-link-status").innerHTML = data.can_manage
      ? (tg.state === "recovery_required"
          ? "❌ <strong>Hay un respaldo pendiente de recuperación manual.</strong> No se reemplazará."
          : tg.ready
          ? "✅ <strong>Vinculado y en línea.</strong>"
          : "⚠️ <strong>Cuenta vinculada, servicio fuera de línea.</strong>")
      : "🔐 Telegram sigue conectado. Usa abajo la <strong>clave administrativa</strong> del operador.";
  } else {
    tgCredentialsHelp.style.display = data.can_manage ? "block" : "none";
    tgInitial.style.display = data.can_manage ? "block" : "none";
    tgLinked.style.display = "none";
    tgLinkButton.innerHTML = "🔗 Vincular";
    document.getElementById("tg-link-status").innerHTML = data.can_manage
      ? "⚠️ Sin vincular. Ingresa api_id, api_hash y teléfono una sola vez."
      : "🔐 Usa la <strong>clave administrativa</strong> del operador antes de configurar Telegram.";
  }

  const wa = data.whatsapp || {};
  const waBtn = document.getElementById("wa-switch-btn");
  const waSummary = document.getElementById("wa-account-summary");
  const restartWaButton = document.getElementById("restart-wa-btn");
  restartWaButton.disabled = wa.state === "recovery_required" || wa.state === "switching" || wa.state === "switching_elsewhere";
  restartWaButton.innerHTML = !data.can_manage
    ? "🔐 Administrar WA"
    : wa.reauth_required
      ? "📲 Volver a vincular WA"
      : "🔄 Reiniciar servicio WA";
  waBtn.disabled = wa.state === "recovery_required" || wa.state === "switching";
  waBtn.innerHTML = !data.can_manage
    ? "🔐 Recuperar acceso"
    : wa.state === "switching"
    ? "⏳ Cambio en curso…"
    : wa.state === "switching_elsewhere"
      ? "↪️ Continuar vinculación aquí"
    : wa.reauth_required
      ? "📲 Volver a vincular"
      : wa.linked ? "🔁 Cambiar cuenta" : "📲 Vincular WhatsApp";
  waSummary.textContent = wa.linked
    ? "Cuenta actual: " + safeAccountLabel(wa, "WhatsApp vinculado")
    : (data.can_manage
        ? "Todavía no hay una cuenta vinculada."
        : "Usa la clave administrativa para habilitar este navegador.");
  if (!data.can_manage) {
    document.getElementById("wa-link-status").innerHTML =
      "🔐 Para administrar WhatsApp o generar un QR, usa arriba la <strong>clave administrativa</strong> del operador.";
  } else if (wa.state === "recovery_required") {
    document.getElementById("wa-link-status").innerHTML =
      "❌ El cambio requiere recuperación manual. El respaldo se mantiene protegido.";
  } else if (wa.state === "switching_elsewhere") {
    document.getElementById("wa-link-status").innerHTML =
      "⏳ Hay un cambio iniciado en otro navegador. Puedes terminarlo allí o pulsar <strong>Continuar vinculación aquí</strong>.";
  } else if (wa.state !== "switching") {
    const waStatus = document.getElementById("wa-link-status");
    if (wa.reauth_required) {
      waStatus.innerHTML = "⚠️ WhatsApp cerró esta sesión. Usa <strong>Volver a vincular</strong>.";
    } else if (wa.linked && !wa.ready) {
      waStatus.innerHTML = "⚠️ Cuenta conservada, pero el servicio está desconectado. Prueba <strong>Reiniciar servicio WA</strong>.";
    } else if (wa.ready) {
      waStatus.innerHTML = "✅ <strong>Vinculado y en línea.</strong>";
    }
  }
  if (wa.state === "switching" && !waSwitchPolling) {
    revealWaQrCard(true);
    if (data.qr_ready) {
      showWaQrImage(data.qr_revision);
    }
    beginWaSwitchPolling();
  } else if (wa.state === "switching_elsewhere") {
    document.getElementById("wa-qr-card").style.display = "none";
  }

  const brandingSave = document.getElementById("tg-audio-branding-save");
  const brandingPerformer = document.getElementById("tg-audio-performer");
  const brandingTitle = document.getElementById("tg-audio-title");
  brandingSave.disabled = !data.can_manage;
  brandingPerformer.readOnly = !data.can_manage;
  brandingTitle.readOnly = !data.can_manage;
  if (data.can_manage) {
    loadTestMode();
  } else {
    renderTestModeState({ok: false, can_manage: false});
  }
}

async function loadChannelState(force=false) {
  if (channelStateRequest && force) await channelStateRequest;
  if (channelStateRequest) return channelStateRequest;
  channelStateRequest = (async () => {
    try {
      const r = await fetch("/api/channels", {cache: "no-store"});
      const data = await r.json();
      if (r.ok && data.ok) renderChannelState(data);
      return data;
    } catch(e) {
      return null;
    } finally {
      channelStateRequest = null;
    }
  })();
  return channelStateRequest;
}

async function showSetup() {
  document.getElementById("setup-modal").style.display = 'block';
  if (!tgAuthAttempt) {
    document.getElementById("tg-code-section").style.display = 'none';
    document.getElementById("tg-2fa-section").style.display = 'none';
  }
  await loadChannelState();
  if (tgAuthAttempt) {
    document.getElementById("tg-code-section").style.display = 'block';
    if (tgAuthMode === "switch") {
      document.getElementById("tg-switch-section").style.display = 'block';
      document.getElementById("tg-link-status").innerHTML =
        '📨 Continúa con el código pendiente. La cuenta anterior sigue activa.';
    }
  }
  // BotFather status
  fetch("/api/bf_status").then(r => r.json()).then(d => {
    const el = document.getElementById("bf-link-status");
    if (d.linked) {
      el.innerHTML = '✅ <strong>Vinculado</strong> como @' + (d.bot_name || 'desconocido');
      document.getElementById("bf-token-input").placeholder = 'Token configurado';
    } else {
      el.innerHTML = '⚠️ Sin token. Crea un bot en @BotFather y pega el token.';
    }
  }).catch(() => {});
}

let tgRetryTimer = null;
let tgAuthAttempt = sessionStorage.getItem("tg_auth_attempt") || null;
function armTgRetry(seconds) {
  const btn = document.getElementById(tgAuthMode === "switch" ? "tg-switch-btn" : "tg-link-btn");
  let remaining = Math.max(0, Number(seconds) || 0);
  if (tgRetryTimer) clearInterval(tgRetryTimer);
  if (remaining <= 0) {
    btn.disabled = false;
    btn.innerHTML = tgAuthMode === "switch" ? '📨 Enviar código' : '🔗 Solicitar otro código';
    return;
  }
  btn.disabled = true;
  btn.textContent = `Espera ${remaining}s`;
  tgRetryTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearInterval(tgRetryTimer);
      tgRetryTimer = null;
      btn.disabled = false;
      btn.innerHTML = tgAuthMode === "switch" ? '📨 Enviar código' : '🔗 Solicitar otro código';
    } else {
      btn.textContent = `Espera ${remaining}s`;
    }
  }, 1000);
}

function rememberTgAttempt(mode, token) {
  tgAuthMode = mode;
  sessionStorage.setItem("tg_auth_mode", mode);
  if (token) {
    tgAuthAttempt = token;
    sessionStorage.setItem("tg_auth_attempt", token);
  }
}

function clearTgAttempt() {
  tgAuthAttempt = null;
  tgAuthMode = "link";
  sessionStorage.removeItem("tg_auth_attempt");
  sessionStorage.removeItem("tg_auth_mode");
  if (tgRetryTimer) clearInterval(tgRetryTimer);
  tgRetryTimer = null;
}

function showTelegramSwitch() {
  if (!channelState || !channelState.can_manage) {
    toast("❌ Esta sesión del panel no puede cambiar canales.", "error");
    return;
  }
  document.getElementById("tg-switch-section").style.display = "block";
  document.getElementById("tg-switch-phone").focus();
}

async function hideTelegramSwitch() {
  if (tgAuthMode === "switch" && tgAuthAttempt) {
    await cancelTgAuth();
  }
  document.getElementById("tg-switch-section").style.display = "none";
  document.getElementById("tg-switch-phone").value = "";
}

async function startTelegramSwitch() {
  const phone = document.getElementById("tg-switch-phone").value.trim();
  if (!phone) { toast("❌ Ingresa el teléfono de la nueva cuenta", "error"); return; }
  rememberTgAttempt("switch", tgAuthAttempt);
  const btn = document.getElementById("tg-switch-btn");
  btn.disabled = true;
  btn.innerHTML = "⏳ Solicitando...";
  const currentReady = Boolean(channelState && channelState.telegram && channelState.telegram.ready);
  document.getElementById("tg-link-status").innerHTML = currentReady
    ? "🔄 Solicitando el código. La cuenta actual sigue atendiendo."
    : "🔄 Solicitando el código. Los archivos de la sesión actual se conservarán.";
  try {
    const r = await fetch("/api/switch_telegram", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({phone, auth_attempt: tgAuthAttempt})
    });
    const d = await r.json();
    if (d.needs_code) {
      rememberTgAttempt("switch", d.auth_attempt || tgAuthAttempt);
      if (!tgAuthAttempt || d.owned_by_this_browser === false) {
        document.getElementById("tg-link-status").innerHTML =
          "⚠️ Ya existe una autorización activa en otro navegador.";
        armTgRetry(d.retry_after ?? 30);
        return;
      }
      document.getElementById("tg-code-section").style.display = "block";
      document.getElementById("tg-code-input").value = "";
      document.getElementById("tg-code-input").focus();
      document.getElementById("tg-code-status").innerHTML = "";
      document.getElementById("tg-code-help").textContent =
        `Busca el código en ${d.delivery || "el canal elegido por Telegram"}.`;
      document.getElementById("tg-link-status").innerHTML =
        "📨 Código solicitado. La cuenta anterior continuará activa hasta confirmar la nueva.";
      armTgRetry(d.retry_after ?? d.timeout_seconds ?? 30);
    } else if (d.ok && d.switched) {
      clearTgAttempt();
      document.getElementById("tg-code-section").style.display = "none";
      document.getElementById("tg-switch-section").style.display = "none";
      toast("✅ Cuenta de Telegram cambiada", "success");
      await loadChannelState();
      btn.disabled = false;
      btn.innerHTML = "📨 Enviar código";
    } else {
      toast("❌ " + (d.error || "No fue posible iniciar el cambio"), "error");
      document.getElementById("tg-link-status").innerHTML = "❌ " + (d.error || "Error");
      if (d.retry_after) armTgRetry(d.retry_after);
      else {
        btn.disabled = false;
        btn.innerHTML = "📨 Enviar código";
      }
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
    btn.disabled = false;
    btn.innerHTML = "📨 Enviar código";
  }
}

async function linkTelegram() {
  rememberTgAttempt("link", tgAuthAttempt);
  const recoveringAdmin = Boolean(
    channelState && channelState.telegram && channelState.telegram.linked && !channelState.can_manage
  );
  const api_id = document.getElementById("tg-api-id").value.trim();
  const api_hash = document.getElementById("tg-api-hash").value.trim();
  const phone = document.getElementById("tg-phone").value.trim();

  if (!api_id) { toast("❌ Ingresa el api_id", "error"); return; }
  if (!api_hash) { toast("❌ Ingresa el api_hash", "error"); return; }
  if (!phone) { toast("❌ Ingresa el número de teléfono", "error"); return; }

  const btn = document.getElementById("tg-link-btn");
  btn.disabled = true;
  btn.innerHTML = '⏳ Conectando...';
  document.getElementById("tg-link-status").innerHTML = recoveringAdmin
    ? '🔄 Comprobando la cuenta actual de Telegram...'
    : '🔄 Solicitando código a Telegram...';

  try {
    const payload = {api_id: parseInt(api_id), api_hash, phone, auth_attempt: tgAuthAttempt};
    const r = await fetch("/api/link_telegram", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.needs_code) {
      if (d.auth_attempt) {
        rememberTgAttempt("link", d.auth_attempt);
      }
      if (!tgAuthAttempt || d.owned_by_this_browser === false) {
        document.getElementById("tg-code-section").style.display = 'none';
        document.getElementById("tg-link-status").innerHTML =
          '⚠️ Ya existe una autorización activa en otro navegador. Espera a que termine o continúa allí.';
        armTgRetry(d.retry_after ?? 30);
        return;
      }
      document.getElementById("tg-code-section").style.display = 'block';
      document.getElementById("tg-code-input").value = '';
      document.getElementById("tg-code-input").focus();
      document.getElementById("tg-code-status").innerHTML = '';
      const prefix = d.request_in_progress ? 'Ya hay una solicitud activa.' : 'Telegram aceptó la solicitud.';
      const next = d.next_delivery ? ` Si no llega, después podrá intentar por ${d.next_delivery}.` : '';
      document.getElementById("tg-code-help").textContent =
        `Busca el código en ${d.delivery || 'el canal elegido por Telegram'}.${next}`;
      document.getElementById("tg-link-status").innerHTML = `📨 ${prefix} No solicites otro hasta que termine la espera.`;
      armTgRetry(d.retry_after ?? d.timeout_seconds ?? 30);
    } else if (d.ok) {
      const recoveredAdmin = Boolean(d.already_authorized);
      if (d.csrf) channelCsrf = d.csrf;
      clearTgAttempt();
      document.getElementById("tg-api-hash").value = '';
      toast(
        recoveredAdmin
          ? "✅ Acceso administrativo recuperado"
          : "✅ Vinculado. El bot se conectará como usuario.",
        "success"
      );
      document.getElementById("tg-link-status").innerHTML = recoveredAdmin
        ? '✅ <strong>Acceso recuperado. Ya puedes administrar WhatsApp.</strong>'
        : '✅ <strong>Sesión autorizada; iniciando Telegram…</strong>';
      await loadChannelState(true);
      if (recoveredAdmin) {
        requestAnimationFrame(() => document.getElementById("wa-switch-btn").scrollIntoView({behavior: "smooth", block: "center"}));
      } else {
        setTimeout(() => document.getElementById("setup-modal").style.display = 'none', 1500);
      }
      btn.disabled = false;
      btn.innerHTML = '🔗 Vincular';
    } else {
      toast("❌ " + (d.error || "Error"), "error");
      document.getElementById("tg-link-status").innerHTML = '❌ ' + (d.error || "Error");
      if (d.retry_after) armTgRetry(d.retry_after);
      else {
        btn.disabled = false;
        btn.innerHTML = '🔗 Vincular';
      }
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
    btn.disabled = false;
    btn.innerHTML = '🔗 Vincular';
  }
}

async function verifyTgCode() {
  const code = document.getElementById("tg-code-input").value.trim();
  if (!code) { toast("❌ Ingresa el código", "error"); return; }
  document.getElementById("tg-code-input").value = '';

  document.getElementById("tg-code-status").innerHTML = '🔄 Verificando...';
  try {
    const switching = tgAuthMode === "switch";
    const r = await fetch(switching ? "/api/switch_telegram/code" : "/api/verify_telegram_code", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({code, auth_attempt: tgAuthAttempt})
    });
    const d = await r.json();
    if (d.ok) {
      const wasSwitch = switching;
      clearTgAttempt();
      document.getElementById("tg-api-hash").value = '';
      toast(wasSwitch ? "✅ Cuenta de Telegram cambiada" : "✅ ¡Vinculado correctamente!", "success");
      document.getElementById("tg-link-status").innerHTML = wasSwitch
        ? '✅ <strong>Cuenta cambiada y verificada.</strong>'
        : '✅ <strong>Vinculado</strong>';
      document.getElementById("tg-code-section").style.display = 'none';
      document.getElementById("tg-switch-section").style.display = 'none';
      await loadChannelState();
      if (!wasSwitch) setTimeout(() => document.getElementById("setup-modal").style.display = 'none', 1500);
    } else if (d.needs_password) {
      // 2FA requerido
      document.getElementById("tg-2fa-section").style.display = 'block';
      document.getElementById("tg-password-input").focus();
      document.getElementById("tg-code-status").innerHTML = '🔐 Esta cuenta tiene 2FA. Ingresa tu contraseña abajo.';
    } else {
      document.getElementById("tg-code-status").innerHTML = '❌ ' + (d.error || "Código inválido");
    }
  } catch(e) {
    document.getElementById("tg-code-status").innerHTML = '❌ Error: ' + e.message;
  }
}

async function verifyTgPassword() {
  const password = document.getElementById("tg-password-input").value;
  if (!password) { toast("❌ Ingresa la contraseña", "error"); return; }
  document.getElementById("tg-password-input").value = '';

  document.getElementById("tg-password-status").innerHTML = '🔄 Verificando...';
  try {
    const switching = tgAuthMode === "switch";
    const r = await fetch(switching ? "/api/switch_telegram/password" : "/api/verify_telegram_password", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({password, auth_attempt: tgAuthAttempt})
    });
    const d = await r.json();
    if (d.ok) {
      const wasSwitch = switching;
      clearTgAttempt();
      document.getElementById("tg-api-hash").value = '';
      toast(wasSwitch ? "✅ Cuenta de Telegram cambiada" : "✅ ¡Vinculado correctamente!", "success");
      document.getElementById("tg-link-status").innerHTML = wasSwitch
        ? '✅ <strong>Cuenta cambiada y verificada.</strong>'
        : '✅ <strong>Vinculado</strong>';
      document.getElementById("tg-code-section").style.display = 'none';
      document.getElementById("tg-2fa-section").style.display = 'none';
      document.getElementById("tg-switch-section").style.display = 'none';
      await loadChannelState();
      if (!wasSwitch) setTimeout(() => document.getElementById("setup-modal").style.display = 'none', 1500);
    } else {
      document.getElementById("tg-password-status").innerHTML = '❌ ' + (d.error || "Contraseña incorrecta");
    }
  } catch(e) {
    document.getElementById("tg-password-status").innerHTML = '❌ Error: ' + e.message;
  }
}

async function cancelTgAuth() {
  const switching = tgAuthMode === "switch";
  await fetch(switching ? "/api/switch_telegram/cancel" : "/api/cancel_telegram_auth", {
    method: "POST",
    headers: channelHeaders(),
    body: JSON.stringify({auth_attempt: tgAuthAttempt})
  }).catch(() => {});
  clearTgAttempt();
  const btn = document.getElementById(switching ? "tg-switch-btn" : "tg-link-btn");
  btn.disabled = false;
  btn.innerHTML = switching ? '📨 Enviar código' : '🔗 Vincular';
  document.getElementById("tg-api-hash").value = '';
  document.getElementById("tg-code-input").value = '';
  document.getElementById("tg-password-input").value = '';
  document.getElementById("tg-code-section").style.display = 'none';
  document.getElementById("tg-2fa-section").style.display = 'none';
  document.getElementById("tg-link-status").innerHTML = switching
    ? 'ℹ️ Cambio cancelado. La cuenta anterior sigue activa.'
    : '⚠️ Vinculación cancelada. Intenta de nuevo.';
}

async function linkBotFather() {
  if (!requirePanelAdmin()) return;
  const token = document.getElementById("bf-token-input").value.trim();
  if (!token) { toast("❌ Pega el token primero", "error"); return; }
  if (!token.includes(":")) { toast("❌ Token inválido", "error"); return; }

  const el = document.getElementById("bf-link-status");
  el.innerHTML = '🔄 Validando token...';

  try {
    const r = await fetch("/api/link_botfather", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({token})
    });
    const d = await r.json();
    if (d.ok) {
      await fetch("/api/start_botfather", {
        method: "POST",
        headers: channelHeaders(),
        body: "{}"
      });
      toast("✅ BotFather vinculado como @" + d.bot_name, "success");
      el.innerHTML = '✅ <strong>Vinculado</strong> como @' + d.bot_name;
    } else {
      toast("❌ " + (d.error || "Error"), "error");
      el.innerHTML = '❌ ' + (d.error || "Error");
    }
  } catch(e) {
    toast("❌ Error: " + e.message, "error");
    el.innerHTML = '❌ Error de conexión';
  }
}

function setWaSwitchCancelDisabled(disabled) {
  document.querySelectorAll(".wa-switch-cancel").forEach(button => {
    button.disabled = disabled;
  });
}

function stopWaSwitchPolling() {
  waSwitchGeneration += 1;
  waSwitchPolling = false;
  if (waSwitchPollTimer) clearTimeout(waSwitchPollTimer);
  waSwitchPollTimer = null;
  if (waSwitchPollAbortController) waSwitchPollAbortController.abort();
  waSwitchPollAbortController = null;
  waCommitInFlight = false;
}

function beginWaSwitchPolling() {
  stopWaSwitchPolling();
  waSwitchPolling = true;
  setWaSwitchCancelDisabled(false);
  waSwitchPollAbortController = new AbortController();
  pollWaSwitch(waSwitchGeneration);
}

async function startWaSwitch(knownState=null) {
  const latest = knownState || await loadChannelState();
  if (!latest) {
    toast("❌ No fue posible consultar el estado de los canales", "error");
    return;
  }
  if (!latest.can_manage) {
    guideChannelAdminRecovery();
    return;
  }
  const current = latest.whatsapp || {};
  if (current.state === "switching_elsewhere") {
    const accepted = confirm(
      "Hay una vinculación abierta en otro navegador. ¿Quieres continuarla de forma segura aquí?"
    );
    if (accepted) await claimWaSwitch();
    return;
  }
  const linked = Boolean(current.linked);
  const ready = Boolean(current.ready);
  const question = linked
    ? (ready
        ? "¿Cambiar la cuenta de WhatsApp? La cuenta actual seguirá atendiendo hasta que la nueva quede vinculada."
        : "¿Volver a vincular WhatsApp? La sesión actual está desconectada, pero sus archivos se conservarán durante el cambio.")
    : "¿Vincular esta instalación con una cuenta de WhatsApp?";
  if (!confirm(question)) return;
  const btn = document.getElementById("wa-switch-btn");
  btn.disabled = true;
  document.getElementById("wa-link-status").innerHTML = linked && ready
    ? "🔄 Preparando el cambio sin desconectar la cuenta actual..."
    : linked
      ? "🔄 Preparando una nueva vinculación; la sesión anterior queda respaldada..."
      : "🔄 Preparando el QR...";
  try {
    const r = await fetch("/api/switch_wa", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({confirm: true})
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      revealWaQrCard(true);
      document.getElementById("wa-qr-help").textContent = "Preparando un QR seguro…";
      beginWaSwitchPolling();
    } else if (d.error_code === "switch_in_progress" && d.owned_by_this_browser === true) {
      revealWaQrCard();
      beginWaSwitchPolling();
    } else if (d.error_code === "switch_in_progress") {
      btn.disabled = false;
      const accepted = confirm(
        "Ya hay una vinculación abierta en otro navegador. ¿Quieres continuarla de forma segura en este navegador?"
      );
      if (accepted) await claimWaSwitch();
      else {
        document.getElementById("wa-link-status").textContent =
          "El intento anterior sigue protegido. Puedes terminarlo en el otro navegador o reintentar cuando venza.";
      }
    } else {
      btn.disabled = false;
      document.getElementById("wa-link-status").innerHTML = "❌ " + (d.error || "Error");
      toast("❌ " + (d.error || "No fue posible preparar el cambio"), "error");
    }
  } catch(e) {
    btn.disabled = false;
    document.getElementById("wa-link-status").innerHTML = "❌ Error: " + e.message;
  }
}

async function claimWaSwitch() {
  const button = document.getElementById("wa-switch-btn");
  button.disabled = true;
  document.getElementById("wa-link-status").textContent = "Transfiriendo la vinculación a este navegador…";
  try {
    const response = await fetch("/api/switch_wa/claim", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({confirm: true})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "No fue posible continuar la vinculación");
    revealWaQrCard(true);
    if (data.qr_ready) {
      showWaQrImage(data.qr_revision);
    }
    document.getElementById("wa-qr-help").textContent = data.qr_ready
      ? "Escanea este QR. Confirmaremos el cambio automáticamente."
      : "Preparando un QR seguro…";
    beginWaSwitchPolling();
  } catch(error) {
    button.disabled = false;
    document.getElementById("wa-qr-card").style.display = "none";
    document.getElementById("wa-link-status").textContent = "❌ " + error.message;
    toast("⚠️ " + error.message, "error");
    await loadChannelState();
  }
}

async function pollWaSwitch(generation) {
  if (!waSwitchPolling || generation !== waSwitchGeneration) return;
  const help = document.getElementById("wa-qr-help");
  try {
    const r = await fetch("/api/switch_wa/status", {
      cache: "no-store",
      signal: waSwitchPollAbortController?.signal
    });
    const d = await r.json();
    // Cancelar invalida la generación y aborta la consulta. Esta segunda
    // comprobación evita promover una cuenta con una respuesta ya resuelta.
    if (!waSwitchPolling || generation !== waSwitchGeneration) return;
    if (d.ready_to_commit && !waCommitInFlight) {
      waCommitInFlight = true;
      setWaSwitchCancelDisabled(true);
      await commitWaSwitch(generation);
      return;
    }
    if (!r.ok && d.state !== "preparing" && d.state !== "awaiting_qr") {
      stopWaSwitchPolling();
      setWaSwitchCancelDisabled(false);
      document.getElementById("wa-qr-card").style.display = "none";
      document.getElementById("wa-switch-btn").disabled = false;
      document.getElementById("wa-link-status").innerHTML = "❌ " + (d.error || "El cambio no se completó.");
      toast("⚠️ " + (d.error || "La cuenta anterior sigue activa"), "error");
      await loadChannelState();
      return;
    }
    if (d.qr_ready) {
      help.textContent = "Escanea este QR. Confirmaremos el cambio automáticamente.";
      showWaQrImage(d.qr_revision);
    } else {
      help.textContent = "Preparando un QR seguro…";
    }
  } catch(e) {
    if (e.name === "AbortError" || !waSwitchPolling || generation !== waSwitchGeneration) return;
    help.textContent = "Reconectando con el servidor…";
  }
  if (waSwitchPolling && generation === waSwitchGeneration) {
    waSwitchPollTimer = setTimeout(() => pollWaSwitch(generation), 1200);
  }
}

async function commitWaSwitch(generation) {
  if (!waSwitchPolling || generation !== waSwitchGeneration || !waCommitInFlight) return;
  const help = document.getElementById("wa-qr-help");
  help.textContent = "Cuenta verificada. Activando el cambio…";
  try {
    const r = await fetch("/api/switch_wa/commit", {
      method: "POST",
      headers: channelHeaders(),
      body: "{}"
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "No fue posible activar la cuenta nueva");
    stopWaSwitchPolling();
    setWaSwitchCancelDisabled(false);
    document.getElementById("wa-qr-card").style.display = "none";
    document.getElementById("wa-link-status").innerHTML = "✅ Cuenta vinculada y verificada.";
    toast("✅ Cuenta de WhatsApp cambiada", "success");
    await loadChannelState();
  } catch(e) {
    stopWaSwitchPolling();
    setWaSwitchCancelDisabled(false);
    document.getElementById("wa-qr-card").style.display = "none";
    document.getElementById("wa-switch-btn").disabled = false;
    document.getElementById("wa-link-status").innerHTML = "❌ " + e.message;
    toast("⚠️ " + e.message, "error");
    await loadChannelState();
  }
}

async function cancelWaSwitch() {
  if (waCommitInFlight) {
    setWaSwitchCancelDisabled(true);
    toast("⏳ La cuenta ya fue verificada y se está activando; esta etapa no puede cancelarse.", "error");
    return;
  }
  stopWaSwitchPolling();
  setWaSwitchCancelDisabled(true);
  try {
    const r = await fetch("/api/switch_wa/cancel", {
      method: "POST",
      headers: channelHeaders(),
      body: "{}"
    });
    const d = await r.json();
    if (r.status === 404) {
      document.getElementById("wa-link-status").textContent =
        "El cambio ya había finalizado o no seguía activo; se actualizó el estado.";
    } else {
      if (!r.ok || !d.ok) throw new Error(d.error || "No fue posible cancelar");
      document.getElementById("wa-link-status").innerHTML =
        "ℹ️ Cambio cancelado. La cuenta anterior sigue activa.";
    }
  } catch(e) {
    toast("❌ " + e.message, "error");
  }
  document.getElementById("wa-qr-card").style.display = "none";
  document.getElementById("wa-switch-btn").disabled = false;
  setWaSwitchCancelDisabled(false);
  await loadChannelState();
}

function renderTestModeState(data) {
  const canManage = Boolean(data && data.can_manage);
  const enabled = canManage && Boolean(data.enabled);
  testModeState = canManage ? data : null;
  const badge = document.getElementById("test-mode-badge");
  const toggle = document.getElementById("test-mode-toggle");
  const summary = document.getElementById("test-mode-summary");
  badge.className = enabled ? "badge bg-success" : "badge bg-secondary";
  badge.textContent = canManage ? (enabled ? "Activado" : "Desactivado") : "Restringido";
  toggle.disabled = !canManage;
  toggle.textContent = enabled ? "Desactivar modo de prueba" : "Activar modo de prueba";
  document.querySelectorAll(".test-reset-button").forEach(button => {
    button.disabled = !canManage || !enabled;
  });
  if (!canManage) {
    summary.textContent = "Usa la clave administrativa del operador para consultar y administrar las pruebas.";
    return;
  }
  const tgCount = data?.telegram?.conversation_count ?? 0;
  const waCount = data?.whatsapp?.conversation_count ?? 0;
  summary.textContent =
    `Conversaciones guardadas: Telegram ${tgCount} · WhatsApp ${waCount}. ` +
    "Cada botón reinicia únicamente la conversación más reciente y conserva un respaldo.";
}

async function loadTestMode() {
  try {
    const response = await fetch("/api/test_mode", {cache: "no-store"});
    const data = await response.json();
    if (response.ok && data.ok) renderTestModeState(data);
    else if (response.status === 403) renderTestModeState({ok: false, can_manage: false});
    return data;
  } catch(error) {
    document.getElementById("test-mode-status").textContent =
      "No fue posible consultar el modo de prueba.";
    return null;
  }
}

async function toggleTestMode() {
  if (!requirePanelAdmin()) return;
  const enabled = !(testModeState && testModeState.enabled);
  const action = enabled ? "activar" : "desactivar";
  if (!confirm(`¿Quieres ${action} el modo de prueba?`)) return;
  const toggle = document.getElementById("test-mode-toggle");
  toggle.disabled = true;
  try {
    const response = await fetch("/api/test_mode", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({enabled})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "No fue posible cambiar el modo");
    renderTestModeState(data);
    toast(enabled ? "Modo de prueba activado" : "Modo de prueba desactivado", "success");
  } catch(error) {
    toast(error.message, "error");
    await loadTestMode();
  }
}

async function resetTestConversation(channel) {
  if (!requirePanelAdmin()) return;
  const channelLabel = channel === "both" ? "Telegram y WhatsApp" :
    channel === "telegram" ? "Telegram" : "WhatsApp";
  const language = document.getElementById("test-mode-language").value;
  const languageLabel = language === "auto" ? "detección automática" : language.toUpperCase();
  if (!confirm(
    `Se reiniciará la conversación más reciente de ${channelLabel}. ` +
    `La próxima interacción recibirá Paso 1 con ${languageLabel}. ¿Continuar?`
  )) return;

  document.querySelectorAll(".test-reset-button").forEach(button => button.disabled = true);
  const status = document.getElementById("test-mode-status");
  status.textContent = "Reiniciando el estado y recargando sólo los servicios que estaban activos…";
  try {
    const response = await fetch("/api/test_mode/reset", {
      method: "POST",
      headers: channelHeaders(),
      body: JSON.stringify({channel, language, confirm: true})
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "No fue posible reiniciar la conversación");
    const resetChannels = Object.entries(data.results || {})
      .filter(([, result]) => result.reset)
      .map(([name]) => name === "telegram" ? "Telegram" : "WhatsApp");
    status.textContent = resetChannels.length
      ? `Listo: ${resetChannels.join(" y ")} comenzará desde Paso 1.`
      : "No había conversaciones guardadas para reiniciar.";
    toast(status.textContent, "success");
    await Promise.all([loadTestMode(), loadData(), loadChannelState(true)]);
  } catch(error) {
    status.textContent = error.message;
    toast(error.message, "error");
    await loadTestMode();
  }
}

document.getElementById("admin-operator-key").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    recoverOperatorAdminAccess();
  }
});

// Auto-refresh cada 10 segundos
loadData();
loadChannelState();
setInterval(loadData, 10000);
setInterval(loadChannelState, 10000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadChannelState();
});
</script>
</body>
</html>"""

# ── Preview template ──────────────────────────────────────────────────
PREVIEW_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>Vista previa — {{ lang_name }}</title>
<style>
body { background: #17212b; color: #e0e0e0; font-family: system-ui; margin: 0; padding: 16px; }
.msg { background: #2b5278; padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; max-width: 85%; }
.msg.own { background: #182533; margin-left: auto; }
.audio-msg { background: #232e3c; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; font-size: 0.9rem; }
.small { font-size: 0.75rem; color: #7f8c8d; margin-bottom: 4px; }
</style>
</head>
<body>
<div class="small">🤖 Bot AutoReply — {{ lang_name }}</div>
{% for step in steps %}
<div class="msg"><strong>Bot:</strong> {{ step.text }}</div>
<div class="audio-msg">🎵 {{ step.audio }}</div>
{% endfor %}
<div class="small" style="margin-top:16px;">* Así se ven los mensajes en Telegram</div>
</body>
</html>"""

# ── API Routes ────────────────────────────────────────────────────────

def load_messages_json():
    return load_message_file(MESSAGES_FILE, DEFAULT_MESSAGES_FILE, persist=True)

def save_messages_json(data):
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_telegram_audio_branding() -> dict[str, str]:
    title, performer = resolve_audio_branding(
        defaults_path=TG_AUDIO_BRANDING_DEFAULTS_FILE,
        settings_path=TG_AUDIO_BRANDING_SETTINGS_FILE,
    )
    return {"title": title, "performer": performer}

def _read_env_var(key: str) -> str | None:
    """Lee una variable desde data/.env.local."""
    env_file = DATA_DIR / ".env.local"
    if not env_file.exists():
        return None
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _configured_operator_recovery_key() -> str | None:
    """Load the operator-owned key only from the deployment environment."""

    configured = os.environ.get(PANEL_ADMIN_RECOVERY_KEY_ENV)
    return configured if valid_configured_operator_key(configured) else None


def _operator_recovery_key_version(configured_key: str) -> str | None:
    """Derive a server-keyed version that revokes sessions when the key rotates."""

    if not valid_configured_operator_key(configured_key):
        return None
    return hmac.new(
        APP_SECRET.encode("utf-8"),
        b"panel-operator-session:v1\0" + configured_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _channel_worker_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a worker environment that cannot inherit the panel operator key."""

    worker_env = os.environ.copy()
    worker_env.pop(PANEL_ADMIN_RECOVERY_KEY_ENV, None)
    if extra:
        worker_env.update(extra)
    return worker_env


def _mask_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return f"••••{digits[-4:]}" if len(digits) >= 4 else None


def _read_safe_identity(file_path: Path) -> dict:
    try:
        parsed = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            return {}
        return {
            "display_name": str(parsed.get("display_name") or "")[:120] or None,
            "username": str(parsed.get("username") or "")[:64] or None,
            "phone_hint": str(parsed.get("phone_hint") or "")[:16] or None,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _secure_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _directory_has_entries(directory: Path) -> bool:
    try:
        return directory.is_dir() and any(directory.iterdir())
    except OSError:
        return True


def _telegram_recovery_pending() -> bool:
    """Nunca pisa un respaldo que todavía requiere intervención."""

    return _directory_has_entries(TG_SWITCH_ROLLBACK_DIR)


def _whatsapp_recovery_pending() -> bool:
    """Detecta respaldos apartados fuera del staging transaccional."""

    return _directory_has_entries(WA_SWITCH_RECOVERY_ROOT)


def _channel_csrf_token() -> str:
    token = session.get("channel_csrf")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["channel_csrf"] = token
        session.permanent = True
    return token


def _operator_recovery_identity() -> str:
    """Build an ephemeral client identity; only its keyed digest is persisted."""

    token = session.get("operator_recovery_browser")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["operator_recovery_browser"] = token
        session.permanent = True
    return token + "\0" + str(request.remote_addr or "unknown")


def _can_manage_channels() -> bool:
    if not session.get("channel_admin"):
        return False

    configured_key = _configured_operator_recovery_key()
    expected_version = (
        _operator_recovery_key_version(configured_key)
        if configured_key is not None
        else None
    )
    supplied_version = session.get("operator_recovery_key_version")
    verified_at = session.get("operator_recovery_verified_at")
    try:
        session_age = time.time() - float(verified_at)
    except (TypeError, ValueError):
        session_age = OPERATOR_ADMIN_SESSION_TTL_SECONDS + 1

    authorized = bool(
        expected_version
        and isinstance(supplied_version, str)
        and secrets.compare_digest(expected_version, supplied_version)
        and -300 <= session_age <= OPERATOR_ADMIN_SESSION_TTL_SECONDS
    )
    if authorized:
        return True

    session.pop("channel_admin", None)
    session.pop("operator_recovery_key_version", None)
    session.pop("operator_recovery_verified_at", None)
    session.pop("telegram_admin", None)
    session.pop("wa_admin", None)
    return False
def _channel_csrf_error():
    supplied = request.headers.get("X-Channel-CSRF", "")
    expected = _channel_csrf_token()
    if not supplied or not secrets.compare_digest(supplied, expected):
        return jsonify({
            "ok": False,
            "error_code": "csrf_invalid",
            "error": "La sesión del panel cambió. Recarga la página e intenta nuevamente.",
        }), 403
    return None


def _channel_mutation_error():
    if not _can_manage_channels():
        return jsonify({
            "ok": False,
            "error_code": "admin_required",
            "error": "Usa la clave administrativa del operador para habilitar este navegador.",
        }), 403
    return _channel_csrf_error()


_PANEL_CSRF_ONLY_MUTATIONS = frozenset({
    "/api/admin_access/request",
    "/api/admin_access/verify",
    "/api/admin_access/cancel",
    "/api/admin_access/operator",
})


@app.before_request
def _protect_panel_api_mutations():
    """Protección central y fail-closed para toda mutación actual o futura."""

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if not request.path.startswith("/api/"):
        return None
    # Endpoint histórico deliberadamente inerte: siempre responde 410 y nunca
    # toca la sesión. Se conserva sin auth para que clientes antiguos fallen de
    # forma explícita en lugar de interpretar un 403 como un problema de acceso.
    if request.path == "/api/reset_wa":
        return None
    if request.path in _PANEL_CSRF_ONLY_MUTATIONS:
        return _channel_csrf_error()
    return _channel_mutation_error()


def _normalize_telegram_phone(phone: str) -> str | None:
    """Normaliza un teléfono internacional sin aceptar saltos ni texto."""
    digits = re.sub(r"\D", "", (phone or "").strip())
    if not 7 <= len(digits) <= 15:
        return None
    return "+" + digits


def _valid_telegram_credentials(api_id, api_hash: str, phone: str) -> bool:
    try:
        valid_id = int(api_id) > 0
    except (TypeError, ValueError):
        valid_id = False
    return bool(
        valid_id
        and re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or "")
        and re.fullmatch(r"\+[0-9]{7,15}", phone or "")
    )


def _save_telegram_creds(api_id, api_hash, phone):
    """Guarda credenciales TG en data/.env.local."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env_file = DATA_DIR / ".env.local"
    lines = []
    if env_file.exists():
        with open(env_file, "r") as f:
            lines = f.readlines()
    new_lines = []
    replaced = {"TG_API_ID": False, "TG_API_HASH": False, "TG_PHONE": False}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TG_API_ID="):
            new_lines.append(f"TG_API_ID={api_id}\n")
            replaced["TG_API_ID"] = True
        elif stripped.startswith("TG_API_HASH="):
            new_lines.append(f"TG_API_HASH={api_hash}\n")
            replaced["TG_API_HASH"] = True
        elif stripped.startswith("TG_PHONE="):
            new_lines.append(f"TG_PHONE={phone}\n")
            replaced["TG_PHONE"] = True
        else:
            new_lines.append(line)
    for key, found in replaced.items():
        if not found:
            val = str(api_id) if key == "TG_API_ID" else (api_hash if key == "TG_API_HASH" else phone)
            new_lines.append(f"{key}={val}\n")
    temp_file = env_file.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(new_lines)
    try:
        os.chmod(temp_file, 0o600)
    except OSError:
        pass
    os.replace(temp_file, env_file)


def _telegram_session_artifacts(session_base: Path) -> list[Path]:
    return [Path(f"{session_base}{suffix}") for suffix in (
        ".session",
        ".session-journal",
        ".session-wal",
        ".session-shm",
    )]


def _remove_telegram_session_artifacts(session_base: Path) -> None:
    for artifact in _telegram_session_artifacts(session_base):
        artifact.unlink(missing_ok=True)


def _telegram_session_has_auth_key(session_base: Path = TG_SESSION_BASE) -> bool:
    """Comprueba la clave MTProto; por sí sola no prueba login de usuario."""
    session_path = Path(f"{session_base}.session")
    if not session_path.is_file() or session_path.stat().st_size == 0:
        return False
    try:
        uri = f"file:{session_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            row = db.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
        return bool(row and row[0])
    except (OSError, sqlite3.Error):
        return False


def _write_telegram_authorized_marker() -> None:
    """Marca una sesión sólo después de que Telegram confirmó al usuario."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    marker = DATA_DIR / "tg_session_authorized.json"
    temp = marker.with_suffix(".tmp")
    temp.write_text(
        json.dumps({"authorized": True, "updated_at": time.time()}),
        encoding="utf-8",
    )
    os.replace(temp, marker)


def _telegram_session_is_authorized() -> bool:
    """Exige auth key y la confirmación escrita tras is_user_authorized()."""
    marker = DATA_DIR / "tg_session_authorized.json"
    try:
        with open(marker, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("authorized")) and _telegram_session_has_auth_key()
    except (OSError, json.JSONDecodeError):
        return False


def _telegram_heartbeat_is_fresh(max_age: int = 45) -> bool:
    health_file = DATA_DIR / "tg_userbot_health.json"
    try:
        with open(health_file, "r", encoding="utf-8") as f:
            health = json.load(f)
        return bool(health.get("ready")) and time.time() - float(health["updated_at"]) <= max_age
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _tracked_telegram_pid() -> int | None:
    pid_file = DATA_DIR / "tg_userbot.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        if pid <= 1 or pid == os.getpid():
            return None
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _windows_process_command_line(pid: int) -> str:
    """Obtiene la línea de comando para no terminar un PID reciclado en Windows."""

    if sys.platform != "win32" or pid <= 1:
        return ""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _is_telegram_worker_pid(pid: int) -> bool:
    """Evita terminar BotFather u otro proceso si el PID quedó obsoleto."""
    if sys.platform != "win32":
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
            return "bot.py" in cmdline and "botfather_bot.py" not in cmdline and "restart_bot.py" not in cmdline
        except OSError:
            return False
    cmdline = _windows_process_command_line(pid).lower()
    return "bot.py" in cmdline and "botfather_bot.py" not in cmdline and "restart_bot.py" not in cmdline


def _stop_telegram_worker() -> None:
    pid_file = DATA_DIR / "tg_userbot.pid"
    pid = _tracked_telegram_pid()
    if pid and _is_telegram_worker_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                if sys.platform != "win32":
                    os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pid_file.unlink(missing_ok=True)
        (DATA_DIR / "tg_userbot_health.json").unlink(missing_ok=True)
    except OSError:
        pass


def restart_telegram_worker() -> tuple[bool, str]:
    """Reinicia sólo el worker rastreado y comprueba que no muera al arrancar."""
    api_id = os.environ.get("TG_API_ID") or _read_env_var("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH") or _read_env_var("TG_API_HASH")
    phone = os.environ.get("TG_PHONE") or _read_env_var("TG_PHONE")
    if not api_id or not api_hash or not phone or not _telegram_session_is_authorized():
        return False, "Telegram aún no tiene una sesión autorizada."

    _stop_telegram_worker()
    log_path = Path(os.environ.get("TG_LOG_PATH", "/tmp/bot_tg.log"))
    if sys.platform == "win32" and str(log_path).startswith("/tmp/"):
        log_path = DATA_DIR / "bot_tg.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = _channel_worker_environment({
        "TG_API_ID": str(api_id),
        "TG_API_HASH": api_hash,
        "TG_PHONE": phone,
    })

    kwargs = {"cwd": str(BASE_DIR), "env": env, "start_new_session": True}
    if sys.platform == "win32":
        kwargs.pop("start_new_session")
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "bot.py")],
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )

    pid_file = DATA_DIR / "tg_userbot.pid"
    pid_temp = pid_file.with_suffix(".tmp")
    pid_temp.write_text(str(proc.pid), encoding="ascii")
    os.replace(pid_temp, pid_file)
    time.sleep(1)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return False, "El proceso de Telegram terminó durante el arranque."
    return True, "Telegram iniciado; la conexión se está comprobando."


def bot_is_running():
    """Verifica si el bot de Telegram está corriendo (por archivo PID)."""
    pid = _tracked_telegram_pid()
    return bool(pid and _is_telegram_worker_pid(pid) and _telegram_heartbeat_is_fresh())


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{destination}.switch.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _restore_optional_file(backup_dir: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    backup = backup_dir / destination.name
    if backup.is_file():
        _copy_file_atomic(backup, destination)


def _wait_until(predicate, timeout_seconds: float, interval_seconds: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval_seconds)
    return False


def _promote_telegram_candidate(
    api_id: int,
    api_hash: str,
    phone: str,
) -> tuple[bool, str, bool, bool]:
    """Activa una sesión candidata y restaura la anterior ante cualquier fallo."""

    candidate_session = Path(f"{TG_SWITCH_SESSION_BASE}.session")
    if not _telegram_session_has_auth_key(TG_SWITCH_SESSION_BASE):
        return (
            False,
            "La nueva sesión no quedó autorizada; la cuenta anterior no fue modificada.",
            True,
            bot_is_running(),
        )

    if _telegram_recovery_pending():
        return (
            False,
            "Hay un respaldo anterior pendiente de recuperación; no se reemplazó ningún archivo.",
            True,
            bot_is_running(),
        )

    with _telegram_switch_lock:
        old_credentials = {
            "TG_API_ID": os.environ.get("TG_API_ID") or _read_env_var("TG_API_ID"),
            "TG_API_HASH": os.environ.get("TG_API_HASH") or _read_env_var("TG_API_HASH"),
            "TG_PHONE": os.environ.get("TG_PHONE") or _read_env_var("TG_PHONE"),
        }
        old_was_running = bot_is_running()
        env_file = DATA_DIR / ".env.local"
        marker = DATA_DIR / "tg_session_authorized.json"
        interaction_state_file = DATA_DIR / "tg_interaction_state.json"
        identity_file = DATA_DIR / "tg_identity.json"

        _stop_telegram_worker()
        if TG_SWITCH_ROLLBACK_DIR.exists():
            shutil.rmtree(TG_SWITCH_ROLLBACK_DIR)
        _secure_directory(TG_SWITCH_ROLLBACK_DIR)
        try:
            os.chmod(candidate_session, 0o600)
        except OSError:
            pass

        backup_files = [
            *_telegram_session_artifacts(TG_SESSION_BASE),
            env_file,
            marker,
            interaction_state_file,
            identity_file,
        ]
        try:
            for source in backup_files:
                if source.is_file():
                    shutil.copy2(source, TG_SWITCH_ROLLBACK_DIR / source.name)
        except Exception:
            old_ready = False
            if old_was_running and _telegram_session_is_authorized():
                started, _ = restart_telegram_worker()
                old_ready = started and _wait_until(bot_is_running, 15)
            if old_ready:
                shutil.rmtree(TG_SWITCH_ROLLBACK_DIR, ignore_errors=True)
            message = (
                "No se pudo preparar el cambio; la cuenta anterior sigue disponible."
                if old_ready
                else "No se pudo preparar el cambio y el servicio anterior no volvió a quedar en línea."
            )
            return False, message, True, old_ready

        try:
            _remove_telegram_session_artifacts(TG_SESSION_BASE)
            _copy_file_atomic(candidate_session, Path(f"{TG_SESSION_BASE}.session"))
            _save_telegram_creds(api_id, api_hash, phone)
            os.environ.update({
                "TG_API_ID": str(api_id),
                "TG_API_HASH": api_hash,
                "TG_PHONE": phone,
            })
            _write_telegram_authorized_marker()
            interaction_state_file.unlink(missing_ok=True)
            identity_file.unlink(missing_ok=True)

            started, _message = restart_telegram_worker()
            if not started or not _wait_until(bot_is_running, 15):
                raise RuntimeError("new_worker_not_ready")
        except Exception:
            restored = True
            try:
                _stop_telegram_worker()
                _remove_telegram_session_artifacts(TG_SESSION_BASE)
                for destination in backup_files:
                    _restore_optional_file(TG_SWITCH_ROLLBACK_DIR, destination)
            except Exception:
                restored = False
            for key, value in old_credentials.items():
                if value:
                    os.environ[key] = str(value)
                else:
                    os.environ.pop(key, None)

            old_ready = False
            if restored and old_was_running and _telegram_session_is_authorized():
                try:
                    started, _ = restart_telegram_worker()
                    old_ready = started and _wait_until(bot_is_running, 15)
                except Exception:
                    old_ready = False

            if restored:
                _remove_telegram_session_artifacts(TG_SWITCH_SESSION_BASE)
                shutil.rmtree(TG_SWITCH_ROLLBACK_DIR, ignore_errors=True)
                message = (
                    "No se pudo activar la cuenta nueva; la anterior fue restaurada."
                    if old_ready
                    else "La cuenta anterior fue restaurada, pero su servicio no volvió a quedar en línea."
                )
            else:
                message = (
                    "Falló la activación y la restauración automática. "
                    "El respaldo se conservó para recuperación manual."
                )
            return False, message, restored, old_ready

        _remove_telegram_session_artifacts(TG_SWITCH_SESSION_BASE)
        shutil.rmtree(TG_SWITCH_ROLLBACK_DIR, ignore_errors=True)
        return True, "Cuenta de Telegram cambiada y verificada.", True, True

@app.route("/")
def index():
    response = make_response(render_template_string(TEMPLATE))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/preview/<lang>")
def preview(lang):
    data = load_messages_json()
    lang_data = data.get(lang, data.get("en"))
    return render_template_string(
        PREVIEW_TEMPLATE,
        lang_name=lang_data.get("lang_name", lang.upper()),
        steps=lang_data.get("steps", [])[:2]
    )

@app.route("/api/data")
def api_data():
    messages = load_messages_json()
    running = bot_is_running()
    wa_running = wa_is_running()
    audios = {}
    for lang, lang_data in messages.items():
        audios[lang] = {}
        for step in lang_data.get("steps", []):
            audio_file = step.get("audio", "")
            audio_path = AUDIO_DIR / audio_file
            audios[lang][audio_file] = audio_path.exists()
    
    # Verificar si hay QR disponible
    qr_available = (BASE_DIR / "wa_qr.png").exists()
    
    return jsonify({
        "ok": True,
        "messages": messages,
        "bot_running": running,
        "bf_running": bf_is_running(),
        "wa_running": wa_running,
        "wa_qr": qr_available,
        "audios": audios,
        "telegram_audio_branding": load_telegram_audio_branding(),
    })


@app.route("/api/telegram_audio_branding", methods=["GET", "POST"])
def api_telegram_audio_branding():
    """Lee o guarda los textos mostrados en las tarjetas de audio de Telegram."""

    if request.method == "GET":
        response = jsonify({"ok": True, **load_telegram_audio_branding()})
        response.headers["Cache-Control"] = "no-store"
        return response

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "No se recibió una configuración válida."}), 400
    if "title" not in data and "performer" not in data:
        return jsonify({"ok": False, "error": "No se recibió ningún texto para actualizar."}), 400

    try:
        current = load_telegram_audio_branding()
        save_audio_branding_settings(
            TG_AUDIO_BRANDING_SETTINGS_FILE,
            title=data.get("title", current["title"]),
            performer=data.get("performer", current["performer"]),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "No fue posible guardar los textos del audio."}), 500

    return jsonify({
        "ok": True,
        **load_telegram_audio_branding(),
        "message": "Textos guardados; se aplicarán al próximo audio de Telegram.",
    })

@app.route("/api/wa_qr")
def api_wa_qr():
    """Compatibilidad: nunca expone un QR fuera de una sesión administradora."""
    if not _can_manage_channels():
        return jsonify({"ok": False, "error": "Sesión administrativa requerida."}), 403
    qr_path = BASE_DIR / "wa_qr.png"
    if qr_path.exists():
        response = send_from_directory(str(BASE_DIR), "wa_qr.png")
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return response
    return "QR not yet generated", 404


@app.route("/api/wa_log")
def api_wa_log():
    """Raw WhatsApp logs are intentionally never exposed over HTTP."""
    return jsonify({
        "ok": False,
        "status": "disabled",
        "replacement": "/api/wa_call_health",
    }), 410


_WA_CALL_HEALTH_DEFAULT = {
    "schema_version": 1,
    "available": False,
    "worker_running": False,
    "worker_revision": None,
    "connection": "unknown",
    "disconnect_reason": "unknown",
    "reauth_required": False,
    "listener": "unknown",
    "raw_listener": "unknown",
    "raw_call_revision": None,
    "parsed_call_revision": None,
    "pipeline_revision": None,
    "last_event": "never",
    "last_batch": {"payload": "never", "size": "never"},
    "pipeline": {
        "event": "never",
        "outcome": "never",
        "reason": "never",
        "offline": None,
        "video": None,
        "group": None,
        "reject": "never",
        "text": "never",
        "audio": "never",
        "target": "never",
        "target_kind": "never",
    },
}


def _safe_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        import uuid
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def _safe_enum(value, choices, fallback):
    return value if isinstance(value, str) and value in choices else fallback


def _read_wa_call_health(file_path: Path = WA_CALL_HEALTH_FILE):
    result = json.loads(json.dumps(_WA_CALL_HEALTH_DEFAULT))
    if not file_path.exists():
        return result
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, TypeError):
        return result
    if not isinstance(raw, dict):
        return result
    if raw.get("schema_version") != 1:
        return result

    result["available"] = True
    result["worker_revision"] = _safe_uuid(raw.get("worker_revision"))
    result["connection"] = _safe_enum(
        raw.get("connection"),
        {"starting", "connecting", "open", "closed", "unknown"},
        "unknown",
    )
    result["disconnect_reason"] = _safe_enum(
        raw.get("disconnect_reason"),
        {"never", "transient", "logged_out", "session_invalid", "timeout", "shutdown", "unknown"},
        "unknown",
    )
    result["reauth_required"] = raw.get("reauth_required") is True
    result["listener"] = _safe_enum(
        raw.get("listener"), {"pending", "registered", "unknown"}, "unknown"
    )
    result["raw_listener"] = _safe_enum(
        raw.get("raw_listener"),
        {"pending", "registered", "unavailable", "unknown"},
        "unknown",
    )
    for key in ("raw_call_revision", "parsed_call_revision", "pipeline_revision"):
        result[key] = _safe_uuid(raw.get(key))
    event_states = {
        "offer", "ringing", "preaccept", "transport", "relaylatency", "terminate",
        "timeout", "reject", "accept", "other", "missing", "never"
    }
    result["last_event"] = _safe_enum(raw.get("last_event"), event_states, "other")

    batch = raw.get("last_batch") if isinstance(raw.get("last_batch"), dict) else {}
    result["last_batch"] = {
        "payload": _safe_enum(
            batch.get("payload"), {"array", "object", "invalid", "never"}, "never"
        ),
        "size": _safe_enum(
            batch.get("size"), {"empty", "one", "multiple", "never"}, "never"
        ),
    }

    pipeline = raw.get("pipeline") if isinstance(raw.get("pipeline"), dict) else {}
    delivery = {
        "sent", "failed", "missing", "skipped", "skipped_offline",
        "not_applicable", "never"
    }
    result["pipeline"] = {
        "event": _safe_enum(
            pipeline.get("event"),
            event_states,
            "other",
        ),
        "outcome": _safe_enum(
            pipeline.get("outcome"), {"handled", "ignored", "failed", "never"}, "failed"
        ),
        "reason": _safe_enum(
            pipeline.get("reason"),
            {"completed", "non_offer", "invalid_event", "missing_identity", "group_call", "duplicate", "unexpected_error", "never"},
            "unexpected_error",
        ),
        "offline": pipeline.get("offline") if isinstance(pipeline.get("offline"), bool) else None,
        "video": pipeline.get("video") if isinstance(pipeline.get("video"), bool) else None,
        "group": pipeline.get("group") if isinstance(pipeline.get("group"), bool) else None,
        "reject": _safe_enum(pipeline.get("reject"), delivery, "not_applicable"),
        "text": _safe_enum(pipeline.get("text"), delivery, "not_applicable"),
        "audio": _safe_enum(pipeline.get("audio"), delivery, "not_applicable"),
        "target": _safe_enum(
            pipeline.get("target"),
            {"caller_pn", "chat_id", "from", "missing", "not_applicable", "never"},
            "not_applicable",
        ),
        "target_kind": _safe_enum(
            pipeline.get("target_kind"),
            {"pn", "lid", "other", "missing", "not_applicable", "never"},
            "not_applicable",
        ),
    }
    return result


@app.route("/api/wa_call_health")
def api_wa_call_health():
    """Returns only an allowlisted, identifier-free call pipeline snapshot."""
    health = _read_wa_call_health(WA_CALL_HEALTH_FILE)
    worker_running = _wa_process_running(DATA_DIR / "wa_bot.pid")
    health["worker_running"] = worker_running
    if not worker_running:
        health["connection"] = "closed"
    response = jsonify(health)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _read_tg_interaction_health() -> dict:
    result = {
        "available": False,
        "worker_running": False,
        "worker_revision": None,
        "connection": "unknown",
        "raw_phone_revision": None,
        "phone_subtype": "never",
        "phone_revisions": {
            key: None
            for key in (
                "requested", "waiting", "accepted", "active",
                "discarded", "empty", "other",
            )
        },
        "call_reject_revision": None,
        "call_reject_status": "never",
        "service_call_revision": None,
        "service_call_status": "never",
        "service_peer_source": "never",
        "missed_call_poll": "starting",
        "classified_revision": None,
        "last_kind": "never",
        "last_response": "never",
        "delivery": {
            "peer_resolution": "never",
            "text": "never",
            "audio": "never",
        },
    }
    file_path = DATA_DIR / "tg_interaction_health.json"
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return result
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return result

    result["available"] = True
    result["worker_revision"] = _safe_uuid(raw.get("worker_revision"))
    result["connection"] = _safe_enum(
        raw.get("connection"),
        {"starting", "connecting", "open", "closed", "unknown"},
        "unknown",
    )
    for key in (
        "raw_phone_revision",
        "call_reject_revision",
        "service_call_revision",
        "classified_revision",
    ):
        result[key] = _safe_uuid(raw.get(key))
    result["phone_subtype"] = _safe_enum(
        raw.get("phone_subtype"),
        {
            "requested", "waiting", "accepted", "active",
            "discarded", "empty", "other", "never",
        },
        "other",
    )
    raw_phone_revisions = (
        raw.get("phone_revisions")
        if isinstance(raw.get("phone_revisions"), dict)
        else {}
    )
    result["phone_revisions"] = {
        key: _safe_uuid(raw_phone_revisions.get(key))
        for key in result["phone_revisions"]
    }
    result["call_reject_status"] = _safe_enum(
        raw.get("call_reject_status"),
        {
            "never", "pending", "sent", "duplicate", "already_finished",
            "timed_out", "failed",
        },
        "failed",
    )
    result["service_call_status"] = _safe_enum(
        raw.get("service_call_status"),
        {"never", "seen", "ignored", "classified", "delivery_failed", "processed"},
        "never",
    )
    result["service_peer_source"] = _safe_enum(
        raw.get("service_peer_source"),
        {"never", "message", "dialog", "update_entities", "cache", "unresolved"},
        "never",
    )
    result["missed_call_poll"] = _safe_enum(
        raw.get("missed_call_poll"),
        {"starting", "healthy", "failed"},
        "failed",
    )
    result["last_kind"] = _safe_enum(
        raw.get("last_kind"), {"call", "content", "never"}, "never"
    )
    result["last_response"] = _safe_enum(
        raw.get("last_response"), {"call", "step1", "step2", "never"}, "never"
    )
    delivery = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
    delivery_states = {"never", "pending", "sent", "failed", "missing", "skipped"}
    result["delivery"] = {
        key: _safe_enum(delivery.get(key), delivery_states, "never")
        for key in ("peer_resolution", "text", "audio")
    }
    return result


@app.route("/api/tg_interaction_health")
def api_tg_interaction_health():
    """Diagnóstico allowlist de llamadas y entregas, sin IDs de clientes."""

    health = _read_tg_interaction_health()
    health["worker_running"] = bot_is_running()
    if not health["worker_running"]:
        health["connection"] = "closed"
    response = jsonify(health)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

@app.route("/api/messages", methods=["POST"])
def api_messages():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400

    messages = load_messages_json()
    lang = data.get("lang")
    action = data.get("action")

    if lang not in messages:
        return jsonify({"ok": False, "error": f"Idioma '{lang}' no existe"}), 400

    lang_data = messages[lang]
    steps = lang_data["steps"]

    if action == "edit_text":
        step = data.get("step")
        text = data.get("text", "").strip()
        if step is None or step < 0 or step >= len(steps):
            return jsonify({"ok": False, "error": "Invalid step"}), 400
        steps[step]["text"] = text
        save_messages_json(messages)
        return jsonify({"ok": True})

    elif action == "add_step":
        return jsonify({
            "ok": False,
            "error": "El flujo usa exactamente Paso 1 y Paso 2 en loop"
        }), 400

    elif action == "remove_step":
        return jsonify({
            "ok": False,
            "error": "Paso 1 y Paso 2 son obligatorios en este flujo"
        }), 400

    elif action == "edit_call_text":
        text = data.get("text", "").strip()
        if "call" not in lang_data:
            lang_data["call"] = {"text": "", "audio": ""}
        lang_data["call"]["text"] = text
        save_messages_json(messages)
        return jsonify({"ok": True})

    elif action == "edit_loop":
        step = data.get("step")
        loop = data.get("loop", False)
        if step is None or step < 0 or step >= len(steps):
            return jsonify({"ok": False, "error": "Invalid step"}), 400
        steps[step]["loop"] = bool(loop)
        save_messages_json(messages)
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400

@app.route("/api/upload_call_audio", methods=["POST"])
def api_upload_call_audio():
    """Sube audio para el mensaje de llamada."""
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "No audio file"}), 400
    
    lang = request.form.get("lang")
    if not lang:
        return jsonify({"ok": False, "error": "lang required"}), 400
    
    file = request.files["audio"]
    if not file.filename.endswith((".mp3", ".ogg", ".wav")):
        return jsonify({"ok": False, "error": "Solo MP3, OGG o WAV"}), 400
    
    messages = load_messages_json()
    if lang not in messages:
        return jsonify({"ok": False, "error": f"Idioma '{lang}' no existe"}), 400
    
    audio_filename = f"{lang}_call.mp3"
    audio_path = AUDIO_DIR / audio_filename
    file.save(str(audio_path))
    
    # Actualizar messages.json con el audio de llamada
    if "call" not in messages[lang]:
        messages[lang]["call"] = {"text": "📞 Llamada recibida", "audio": audio_filename}
    else:
        messages[lang]["call"]["audio"] = audio_filename
    save_messages_json(messages)
    
    return jsonify({"ok": True, "filename": audio_filename, "path": str(audio_path)})

@app.route("/api/upload_audio", methods=["POST"])
def api_upload_audio():
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "No audio file"}), 400

    lang = request.form.get("lang")
    step = request.form.get("step", type=int)
    if not lang or step is None:
        return jsonify({"ok": False, "error": "lang and step required"}), 400

    messages = load_messages_json()
    if lang not in messages:
        return jsonify({"ok": False, "error": f"Idioma '{lang}' no existe"}), 400

    steps = messages[lang]["steps"]
    if step < 0 or step >= len(steps):
        return jsonify({"ok": False, "error": "Invalid step"}), 400

    file = request.files["audio"]
    if not file.filename.endswith((".mp3", ".ogg", ".wav")):
        return jsonify({"ok": False, "error": "Solo MP3, OGG o WAV"}), 400

    # Usar el nombre esperado del step
    audio_filename = steps[step]["audio"]
    audio_path = AUDIO_DIR / audio_filename
    file.save(str(audio_path))

    return jsonify({"ok": True, "filename": audio_filename, "path": str(audio_path)})

@app.route("/api/audio/<filename>")
def api_audio(filename):
    """Sirve archivos de audio para preview."""
    return send_from_directory(str(AUDIO_DIR), filename)

# ── Chunked upload (bypass proxy size limits) ─────────────────────────

@app.route("/api/upload_chunk", methods=["POST"])
def api_upload_chunk():
    """Recibe un chunk de audio. Cada chunk ≤ 500KB."""
    if "chunk" not in request.files:
        return jsonify({"ok": False, "error": "No chunk"}), 400

    lang = request.form.get("lang", "unknown")
    chunk_index = request.form.get("chunk_index", type=int)
    total_chunks = request.form.get("total_chunks", type=int)
    upload_type = request.form.get("type", "step")
    step = request.form.get("step", type=int)
    original_name = request.form.get("original_name", "audio.mp3")

    if chunk_index is None or total_chunks is None:
        return jsonify({"ok": False, "error": "chunk_index and total_chunks required"}), 400

    # Temp dir: data/temp_chunks/<lang>_<type>_<step>/
    if upload_type == "call":
        temp_key = f"{lang}_call"
    else:
        temp_key = f"{lang}_step{step}"
    temp_dir = DATA_DIR / "temp_chunks" / temp_key
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunk = request.files["chunk"]
    chunk_path = temp_dir / f"chunk_{chunk_index:04d}"
    chunk.save(str(chunk_path))

    # Guardar metadata
    meta = {"total_chunks": total_chunks, "original_name": original_name,
            "type": upload_type, "lang": lang}
    if step is not None:
        meta["step"] = step
    with open(temp_dir / "meta.json", "w") as f:
        json.dump(meta, f)

    return jsonify({"ok": True, "chunk": chunk_index, "total": total_chunks})


@app.route("/api/upload_assemble", methods=["POST"])
def api_upload_assemble():
    """Reensambla los chunks en el archivo final de audio."""
    try:
        return _do_upload_assemble()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500


def _do_upload_assemble():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "JSON required"}), 400

    lang = data.get("lang")
    step_raw = data.get("step")
    step = int(step_raw) if step_raw is not None else None
    upload_type = data.get("type", "step")
    total_chunks = data.get("total_chunks")
    original_name = data.get("original_name", "")

    if not lang:
        return jsonify({"ok": False, "error": "lang required"}), 400

    # Encontrar temp dir
    if upload_type == "call":
        temp_key = f"{lang}_call"
    else:
        if step is None:
            return jsonify({"ok": False, "error": "step required"}), 400
        temp_key = f"{lang}_step{step}"

    temp_dir = DATA_DIR / "temp_chunks" / temp_key
    if not temp_dir.exists():
        return jsonify({"ok": False, "error": "No chunks found"}), 400

    # Verificar que todos los chunks están
    for i in range(total_chunks):
        if not (temp_dir / f"chunk_{i:04d}").exists():
            return jsonify({"ok": False, "error": f"Missing chunk {i}"}), 400

    # Determinar filename de salida
    if upload_type == "call":
        audio_filename = f"{lang}_call.mp3"
    else:
        messages = load_messages_json()
        if lang not in messages:
            return jsonify({"ok": False, "error": f"Idioma '{lang}' no existe"}), 400
        steps = messages[lang]["steps"]
        if step < 0 or step >= len(steps):
            return jsonify({"ok": False, "error": "Invalid step"}), 400
        audio_filename = steps[step]["audio"]

    # Ensamblar
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = AUDIO_DIR / audio_filename
    try:
        with open(audio_path, "wb") as out:
            for i in range(total_chunks):
                chunk_path = temp_dir / f"chunk_{i:04d}"
                with open(chunk_path, "rb") as f_in:
                    out.write(f_in.read())
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error escribiendo audio: {str(e)}"}), 500

    # Si es call, actualizar messages.json
    if upload_type == "call":
        messages = load_messages_json()
        if lang in messages:
            if "call" not in messages[lang]:
                messages[lang]["call"] = {"text": "📞 Llamada recibida", "audio": audio_filename}
            else:
                messages[lang]["call"]["audio"] = audio_filename
            save_messages_json(messages)

    # Limpiar temp
    shutil.rmtree(str(temp_dir), ignore_errors=True)

    return jsonify({"ok": True, "filename": audio_filename})

def is_bot_running():
    """Verifica si el bot responde haciendo un ping a Telegram API (user bot no tiene getMe)."""
    # Con user bot, verificamos que el proceso esté corriendo
    return bot_is_running()

@app.route("/api/tg_status")
def api_tg_status():
    """Estado no sensible de la vinculación de Telegram."""
    api_id = os.environ.get("TG_API_ID") or _read_env_var("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH") or _read_env_var("TG_API_HASH")
    phone = os.environ.get("TG_PHONE") or _read_env_var("TG_PHONE")
    configured = bool(api_id and api_hash and phone)
    session_authorized = _telegram_session_is_authorized()
    worker_running = bot_is_running()
    if worker_running:
        state = "ready"
    elif session_authorized:
        state = "authorized_offline"
    elif configured:
        state = "needs_verification"
    else:
        state = "unconfigured"
    identity = _read_safe_identity(DATA_DIR / "tg_identity.json")
    can_manage = _can_manage_channels()
    return jsonify({
        "configured": configured,
        "session_authorized": session_authorized,
        "linked": session_authorized,
        "worker_running": worker_running,
        "ready": worker_running,
        "state": state,
        "display_name": identity.get("display_name") if session_authorized and can_manage else None,
        "username": identity.get("username") if session_authorized and can_manage else None,
        "phone_hint": _mask_phone(phone) if session_authorized and can_manage else None,
        "can_switch": session_authorized and can_manage,
    })


@app.route("/api/admin_access/request", methods=["POST"])
def api_admin_access_request():
    """Legacy OTP entry point retained as an inert, fail-closed stub."""

    csrf_error = _channel_csrf_error()
    if csrf_error:
        return csrf_error
    return jsonify({
        "ok": False,
        "authorized": False,
        "error_code": "operator_recovery_required",
        "error": "La recuperación por Telegram fue deshabilitada. Usa la clave administrativa del operador.",
    }), 410


@app.route("/api/admin_access/status")
def api_admin_access_status():
    return jsonify({
        "ok": False,
        "authorized": False,
        "error_code": "operator_recovery_required",
        "error": "La recuperación por Telegram fue deshabilitada. Usa la clave administrativa del operador.",
    }), 410


@app.route("/api/admin_access/verify", methods=["POST"])
def api_admin_access_verify():
    csrf_error = _channel_csrf_error()
    if csrf_error:
        return csrf_error
    return jsonify({
        "ok": False,
        "authorized": False,
        "error_code": "operator_recovery_required",
        "error": "La recuperación por Telegram fue deshabilitada. Usa la clave administrativa del operador.",
    }), 410


@app.route("/api/admin_access/cancel", methods=["POST"])
def api_admin_access_cancel():
    csrf_error = _channel_csrf_error()
    if csrf_error:
        return csrf_error
    return jsonify({
        "ok": False,
        "authorized": False,
        "error_code": "operator_recovery_required",
        "error": "La recuperación por Telegram fue deshabilitada. Usa la clave administrativa del operador.",
    }), 410


@app.route("/api/admin_access/operator", methods=["POST"])
def api_admin_access_operator():
    """Authorize this browser with an operator-owned key, independently of channels."""

    csrf_error = _channel_csrf_error()
    if csrf_error:
        return csrf_error

    configured_key = _configured_operator_recovery_key()
    if configured_key is None:
        return jsonify({
            "ok": False,
            "authorized": False,
            "error_code": "operator_recovery_unconfigured",
            "error": "La clave administrativa del operador no está configurada.",
        }), 503

    data = request.get_json(silent=True) or {}
    supplied_key = data.get("key") if isinstance(data.get("key"), str) else ""
    matched = operator_key_matches(configured_key, supplied_key)
    decision = _operator_admin_recovery.evaluate(
        _operator_recovery_identity(),
        matched=matched,
    )
    if not decision.get("allowed"):
        error_code = decision.get("error_code")
        if error_code == "rate_limited":
            retry_after = max(1, int(decision.get("retry_after", 1) or 1))
            response = jsonify({
                "ok": False,
                "authorized": False,
                "error_code": "rate_limited",
                "retry_after": retry_after,
                "error": "Hay demasiados intentos. Espera antes de volver a probar.",
            })
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        if error_code in {"busy", "invalid_client"}:
            return jsonify({
                "ok": False,
                "authorized": False,
                "error_code": "operator_recovery_unavailable",
                "retry_after": max(1, int(decision.get("retry_after", 1) or 1)),
                "error": "No fue posible comprobar la clave en este momento.",
            }), 503
        return jsonify({
            "ok": False,
            "authorized": False,
            "error_code": "invalid_operator_key",
            "error": "La clave administrativa no es correcta.",
        }), 401

    session["channel_admin"] = True
    session.pop("telegram_admin", None)
    session.pop("wa_admin", None)
    session["operator_recovery_key_version"] = _operator_recovery_key_version(
        configured_key
    )
    session["operator_recovery_verified_at"] = time.time()
    session["operator_recovery_browser"] = secrets.token_urlsafe(32)
    session["channel_csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify({
        "ok": True,
        "authorized": True,
        "state": "verified",
        "csrf": session["channel_csrf"],
    })


def _finish_telegram_auth(outcome):
    """Persiste sólo credenciales verificadas e inicia el worker."""
    public = dict(outcome.public)
    if outcome.attempt_token:
        public["auth_attempt"] = outcome.attempt_token
    if not outcome.credentials:
        return public
    api_id, api_hash, phone = outcome.credentials
    _save_telegram_creds(api_id, api_hash, phone)
    _write_telegram_authorized_marker()
    os.environ.update({
        "TG_API_ID": str(api_id),
        "TG_API_HASH": api_hash,
        "TG_PHONE": phone,
    })
    session["channel_admin"] = True
    session.permanent = True
    started, message = restart_telegram_worker()
    public.update({
        "ok": started,
        "authorized": True,
        "worker_starting": started,
        "message": message,
    })
    if not started:
        public["error"] = message
    return public


def _finish_telegram_switch(outcome):
    """Promueve únicamente una sesión candidata ya autorizada."""

    public = dict(outcome.public)
    if outcome.attempt_token:
        public["auth_attempt"] = outcome.attempt_token
    if not outcome.credentials:
        return public

    api_id, api_hash, phone = outcome.credentials
    switched, message, previous_preserved, previous_ready = _promote_telegram_candidate(
        api_id, api_hash, phone
    )
    public.update({
        "ok": switched,
        "authorized": switched,
        "switched": switched,
        "message": message,
    })
    if switched:
        session["channel_admin"] = True
        session.permanent = True
    else:
        public.update({
            "error_code": "activation_failed",
            "error": message,
            "previous_account_preserved": previous_preserved,
            "previous_worker_ready": previous_ready,
        })
    return public


@app.route("/api/link_telegram", methods=["POST"])
def api_link_telegram():
    """Inicia una autorización y describe el canal elegido por Telegram."""
    data = request.get_json(silent=True) or {}

    # Una sesión TG existente nunca sirve para autenticar este navegador. La
    # clave del operador es el único flujo de recuperación administrativa y no
    # depende de credenciales ni de acceso a la cuenta del cliente final.
    if _telegram_session_is_authorized():
        if not _can_manage_channels():
            return jsonify({
                "ok": False,
                "authorized": False,
                "error_code": "operator_recovery_required",
                "error": "Telegram ya está vinculado. Usa la clave administrativa del operador.",
            }), 403
        if bot_is_running():
            started, message = True, "Telegram ya está vinculado y en línea."
        else:
            started, message = restart_telegram_worker()
        return jsonify({
            "ok": started,
            "already_authorized": True,
            "worker_starting": started,
            "message": message,
            "csrf": _channel_csrf_token(),
            **({} if started else {"error": message}),
        })

    api_id = data.get("api_id")
    api_hash = (data.get("api_hash") or "").strip()
    phone = _normalize_telegram_phone(data.get("phone") or "")

    if not _valid_telegram_credentials(api_id, api_hash, phone or ""):
        return jsonify({
            "ok": False,
            "error_code": "invalid_input",
            "error": "Revisa api_id, api_hash y el teléfono en formato internacional.",
        }), 400

    outcome = _telegram_auth.begin(
        int(api_id), api_hash, phone, data.get("auth_attempt")
    )
    return jsonify(_finish_telegram_auth(outcome))


@app.route("/api/verify_telegram_code", methods=["POST"])
def api_verify_telegram_code():
    """Verifica el código enviado por Telegram."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,31}", code):
        return jsonify({"ok": False, "error": "Ingresa el código recibido, sin caracteres especiales."}), 400

    outcome = _telegram_auth.verify_code(code, data.get("auth_attempt"))
    return jsonify(_finish_telegram_auth(outcome))


@app.route("/api/verify_telegram_password", methods=["POST"])
def api_verify_telegram_password():
    """Verifica la contraseña de 2FA."""
    data = request.get_json(silent=True) or {}
    password = (data.get("password") or "")
    if not password or len(password) > 256:
        return jsonify({"ok": False, "error": "Contraseña requerida"}), 400

    outcome = _telegram_auth.verify_password(password, data.get("auth_attempt"))
    return jsonify(_finish_telegram_auth(outcome))


@app.route("/api/cancel_telegram_auth", methods=["POST"])
def api_cancel_telegram_auth():
    """Cancela la autenticación pendiente."""
    data = request.get_json(silent=True) or {}
    return jsonify(_telegram_auth.cancel(data.get("auth_attempt")).public)


@app.route("/api/switch_telegram", methods=["POST"])
def api_switch_telegram():
    """Autoriza otra cuenta en staging sin detener la cuenta actual."""

    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    if not _telegram_session_is_authorized():
        return jsonify({
            "ok": False,
            "error_code": "not_linked",
            "error": "Telegram aún no está vinculado; usa el formulario inicial.",
        }), 409
    if _telegram_recovery_pending():
        return jsonify({
            "ok": False,
            "error_code": "recovery_required",
            "error": "Hay un respaldo de Telegram pendiente de recuperación manual.",
        }), 409

    data = request.get_json(silent=True) or {}
    phone = _normalize_telegram_phone(data.get("phone") or "")
    api_id = os.environ.get("TG_API_ID") or _read_env_var("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH") or _read_env_var("TG_API_HASH") or ""
    current_phone = os.environ.get("TG_PHONE") or _read_env_var("TG_PHONE") or ""
    if not _valid_telegram_credentials(api_id, api_hash, phone or ""):
        return jsonify({
            "ok": False,
            "error_code": "invalid_input",
            "error": "Ingresa el nuevo teléfono en formato internacional.",
        }), 400
    if secrets.compare_digest(phone or "", current_phone):
        return jsonify({
            "ok": False,
            "error_code": "same_account",
            "error": "Ese teléfono ya corresponde a la cuenta activa.",
        }), 409

    attempt_token = data.get("auth_attempt")
    with _telegram_switch_lock:
        if not attempt_token and not _telegram_switch_auth.has_pending():
            _remove_telegram_session_artifacts(TG_SWITCH_SESSION_BASE)
        outcome = _telegram_switch_auth.begin(
            int(api_id), api_hash, phone, attempt_token
        )
        public = _finish_telegram_switch(outcome)
    status = 503 if public.get("error_code") == "activation_failed" else 200
    return jsonify(public), status


@app.route("/api/switch_telegram/code", methods=["POST"])
def api_switch_telegram_code():
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,31}", code):
        return jsonify({"ok": False, "error": "Ingresa el código recibido."}), 400
    with _telegram_switch_lock:
        public = _finish_telegram_switch(
            _telegram_switch_auth.verify_code(code, data.get("auth_attempt"))
        )
    status = 503 if public.get("error_code") == "activation_failed" else 200
    return jsonify(public), status


@app.route("/api/switch_telegram/password", methods=["POST"])
def api_switch_telegram_password():
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not password or len(password) > 256:
        return jsonify({"ok": False, "error": "Contraseña requerida."}), 400
    with _telegram_switch_lock:
        public = _finish_telegram_switch(
            _telegram_switch_auth.verify_password(password, data.get("auth_attempt"))
        )
    status = 503 if public.get("error_code") == "activation_failed" else 200
    return jsonify(public), status


@app.route("/api/switch_telegram/cancel", methods=["POST"])
def api_switch_telegram_cancel():
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    data = request.get_json(silent=True) or {}
    with _telegram_switch_lock:
        outcome = _telegram_switch_auth.cancel(data.get("auth_attempt"))
        if outcome.public.get("ok"):
            _remove_telegram_session_artifacts(TG_SWITCH_SESSION_BASE)
    return jsonify(outcome.public)

@app.route("/api/status")
def api_status():
    """Estado de los bots (para polling del frontend)."""
    return jsonify({
        "bot_running": is_bot_running(),
        "bf_running": bf_is_running(),
        "wa_running": wa_is_running()
    })


@app.route("/api/bf_status")
def api_bf_status():
    """Estado del BotFather bot."""
    token = os.environ.get("AUTOREPLY_BOT_TOKEN") or _read_env_var("AUTOREPLY_BOT_TOKEN")
    linked = token is not None
    result = {"linked": linked}
    if linked:
        try:
            import subprocess
            r = subprocess.run(
                ["curl", "-s", "--connect-timeout", "5",
                 f"https://api.telegram.org/bot{token}/getMe"],
                capture_output=True, text=True, timeout=10
            )
            data = json.loads(r.stdout)
            if data.get("ok"):
                result["bot_name"] = data["result"].get("username", "")
        except:
            pass
    return jsonify(result)


@app.route("/api/link_botfather", methods=["POST"])
def api_link_botfather():
    """Valida y guarda el token de BotFather."""
    import subprocess
    data = request.get_json()
    token = (data.get("token") or "").strip()

    if not token or ":" not in token:
        return jsonify({"ok": False, "error": "Token inválido"}), 400

    # Validar con Telegram
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5",
             f"https://api.telegram.org/bot{token}/getMe"],
            capture_output=True, text=True, timeout=10
        )
        resp = json.loads(r.stdout)
        if not resp.get("ok"):
            return jsonify({"ok": False, "error": "Token inválido o revocado"}), 400
        bot_name = resp["result"].get("username", "desconocido")
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error validando: {str(e)}"}), 500

    # Guardar en .env.local
    env_file = DATA_DIR / ".env.local"
    lines = []
    if env_file.exists():
        with open(env_file, "r") as f:
            lines = f.readlines()
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("AUTOREPLY_BOT_TOKEN="):
            new_lines.append(f"AUTOREPLY_BOT_TOKEN={token}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"AUTOREPLY_BOT_TOKEN={token}\n")
    with open(env_file, "w") as f:
        f.writelines(new_lines)

    os.environ["AUTOREPLY_BOT_TOKEN"] = token
    return jsonify({"ok": True, "bot_name": bot_name})


def bf_is_running():
    """Verifica si el BotFather bot está corriendo (por archivo PID)."""
    pid_file = DATA_DIR / "botfather.pid"
    try:
        if not pid_file.exists():
            return False
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        # Verificar que el proceso siga vivo
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False

def _tracked_wa_pid(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        if pid <= 1 or pid == os.getpid():
            return None
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _is_wa_worker_pid(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
            return "wa_bot.mjs" in cmdline
        except OSError:
            return False
    return "wa_bot.mjs" in _windows_process_command_line(pid).lower()


def _wa_process_running(pid_file: Path) -> bool:
    pid = _tracked_wa_pid(pid_file)
    return bool(pid and _is_wa_worker_pid(pid))


def _stop_wa_process(pid_file: Path) -> None:
    pid = _tracked_wa_pid(pid_file)
    if pid and _is_wa_worker_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                if sys.platform != "win32":
                    os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    pid_file.unlink(missing_ok=True)


def _start_wa_process(
    *,
    auth_dir: Path,
    qr_file: Path,
    health_file: Path,
    identity_file: Path,
    pid_file: Path,
    link_only: bool,
    log_name: str,
) -> int | None:
    _secure_directory(auth_dir)
    health_file.unlink(missing_ok=True)
    qr_file.unlink(missing_ok=True)
    env = _channel_worker_environment({
        "WA_AUTH_DIR": str(auth_dir),
        "WA_QR_PATH": str(qr_file),
        "WA_HEALTH_FILE": str(health_file),
        "WA_IDENTITY_FILE": str(identity_file),
        "WA_LINK_ONLY": "1" if link_only else "0",
        "WA_LINK_TIMEOUT_MS": str(WA_SWITCH_TIMEOUT_SECONDS * 1000),
    })
    log_path = Path("/tmp") / log_name
    if sys.platform == "win32":
        log_path = DATA_DIR / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "cwd": str(BASE_DIR),
        "env": env,
        "start_new_session": True,
    }
    if sys.platform == "win32":
        kwargs.pop("start_new_session")
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    with open(log_path, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            ["node", str(BASE_DIR / "wa_bot.mjs")],
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    temporary_pid = pid_file.with_suffix(".tmp")
    temporary_pid.parent.mkdir(parents=True, exist_ok=True)
    temporary_pid.write_text(str(proc.pid), encoding="ascii")
    os.replace(temporary_pid, pid_file)
    time.sleep(0.75)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return None
    return proc.pid


def wa_is_running():
    """Reporta disponibilidad real, no sólo la existencia de un proceso."""

    pid_file = DATA_DIR / "wa_bot.pid"
    if _wa_connection_open(pid_file, WA_CALL_HEALTH_FILE):
        return True
    if WA_AUTH_DIR.exists() and any(WA_AUTH_DIR.iterdir()):
        return False
    return None


def _wa_connection_open(
    pid_file: Path = DATA_DIR / "wa_bot.pid",
    health_file: Path = WA_CALL_HEALTH_FILE,
) -> bool:
    return _wa_process_running(pid_file) and _read_wa_call_health(health_file).get("connection") == "open"


def restart_wa_bot() -> int | None:
    """Reinicia WhatsApp conservando íntegramente la cuenta vinculada."""

    with _wa_process_lock:
        pid_file = DATA_DIR / "wa_bot.pid"
        _stop_wa_process(pid_file)
        return _start_wa_process(
            auth_dir=WA_AUTH_DIR,
            qr_file=BASE_DIR / "wa_qr.png",
            health_file=WA_CALL_HEALTH_FILE,
            identity_file=WA_IDENTITY_FILE,
            pid_file=pid_file,
            link_only=False,
            log_name="bot_wa.log",
        )


def _test_mode_payload() -> dict:
    can_manage = _can_manage_channels()
    empty_summary = {"conversation_count": 0, "latest_updated_at": None}
    return {
        "ok": True,
        "enabled": load_test_mode(TEST_MODE_FILE),
        "can_manage": can_manage,
        "telegram": (
            interaction_state_summary(TG_INTERACTION_STATE_FILE)
            if can_manage else empty_summary
        ),
        "whatsapp": (
            interaction_state_summary(WA_INTERACTION_STATE_FILE)
            if can_manage else empty_summary
        ),
    }


def _restore_state_snapshot(path: Path, content: bytes | None) -> None:
    """Restore one state file atomically, including its previous absence."""

    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".restore.tmp")
    temporary.write_bytes(content)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _test_mode_switch_conflict(channels: list[str]) -> str | None:
    if "telegram" in channels:
        if _telegram_switch_auth.has_pending():
            return "Termina o cancela primero el cambio de cuenta de Telegram."
        if _telegram_recovery_pending():
            return "Resuelve primero la recuperación pendiente de Telegram."
    if "whatsapp" in channels:
        _reap_expired_wa_switch()
        if _load_wa_switch_operation():
            return "Termina o cancela primero el cambio de cuenta de WhatsApp."
        if _whatsapp_recovery_pending():
            return "Resuelve primero la recuperación pendiente de WhatsApp."
    return None


@app.route("/api/test_mode", methods=["GET", "POST"])
def api_test_mode():
    if request.method == "GET" and not _can_manage_channels():
        response = jsonify({
            "ok": False,
            "can_manage": False,
            "error_code": "admin_required",
            "error": "Usa la clave administrativa del operador para consultar el modo de prueba.",
        })
        response.headers["Cache-Control"] = "no-store, private"
        return response, 403

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify({"ok": False, "error": "enabled debe ser true o false."}), 400
        try:
            with _test_mode_lock:
                save_test_mode(TEST_MODE_FILE, data["enabled"])
        except OSError:
            return jsonify({
                "ok": False,
                "error_code": "test_mode_persist_failed",
                "error": "No fue posible guardar el modo de prueba.",
            }), 500

    response = jsonify(_test_mode_payload())
    response.headers["Cache-Control"] = "no-store, private"
    return response


@app.route("/api/test_mode/reset", methods=["POST"])
def api_test_mode_reset():
    if not load_test_mode(TEST_MODE_FILE):
        return jsonify({
            "ok": False,
            "error_code": "test_mode_disabled",
            "error": "Activa primero el modo de prueba.",
        }), 409

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "Confirma explícitamente el reinicio."}), 400
    selected = data.get("channel")
    if selected not in {"telegram", "whatsapp", "both"}:
        return jsonify({"ok": False, "error": "Canal de prueba inválido."}), 400
    selected_language = data.get("language", "auto")
    if selected_language not in {"auto", "es", "en", "fr"}:
        return jsonify({"ok": False, "error": "Idioma de prueba inválido."}), 400

    preset_language = None if selected_language == "auto" else selected_language
    channels = ["telegram", "whatsapp"] if selected == "both" else [selected]
    state_paths = {
        "telegram": TG_INTERACTION_STATE_FILE,
        "whatsapp": WA_INTERACTION_STATE_FILE,
    }
    results = {}
    restart_warnings = []
    reset_error = None
    rollback_complete = True
    state_snapshots = None

    # Account switches take the same locks, so they cannot begin in the middle
    # of a reset and the reset cannot touch a candidate session.
    with _test_mode_lock, _telegram_switch_lock, _wa_switch_lock, _wa_process_lock:
        conflict = _test_mode_switch_conflict(channels)
        if conflict:
            return jsonify({
                "ok": False,
                "error_code": "account_switch_in_progress",
                "error": conflict,
            }), 409

        telegram_was_running = False
        whatsapp_was_running = False
        if "telegram" in channels:
            telegram_pid = _tracked_telegram_pid()
            telegram_was_running = bool(
                telegram_pid and _is_telegram_worker_pid(telegram_pid)
            )
            if telegram_was_running:
                _stop_telegram_worker()
        if "whatsapp" in channels:
            whatsapp_pid_file = DATA_DIR / "wa_bot.pid"
            whatsapp_was_running = _wa_process_running(whatsapp_pid_file)
            if whatsapp_was_running:
                _stop_wa_process(whatsapp_pid_file)

        try:
            state_snapshots = {
                channel: (
                    state_paths[channel].read_bytes()
                    if state_paths[channel].exists() else None
                )
                for channel in channels
            }
            for channel in channels:
                result = reset_latest_interaction(
                    state_paths[channel],
                    channel=channel,
                    backup_dir=TEST_MODE_BACKUP_DIR,
                    language=preset_language,
                )
                results[channel] = {
                    "reset": result["reset"],
                    "remaining": result["remaining"],
                    "language": selected_language,
                    "backup_created": bool(result["backup"]),
                }
        except OSError:
            results = {}
            if state_snapshots is None:
                reset_error = "No fue posible preparar un respaldo del estado de prueba."
            else:
                reset_error = (
                    "No fue posible guardar el estado de prueba. "
                    "Se intentó restaurar el estado anterior de ambos canales."
                )
                for channel, content in state_snapshots.items():
                    try:
                        _restore_state_snapshot(state_paths[channel], content)
                    except OSError:
                        rollback_complete = False
        finally:
            if telegram_was_running:
                try:
                    started, message = restart_telegram_worker()
                    if not started:
                        restart_warnings.append(f"Telegram: {message}")
                except Exception:
                    restart_warnings.append("Telegram no pudo reiniciarse automáticamente.")
            if whatsapp_was_running:
                try:
                    if not restart_wa_bot():
                        restart_warnings.append("WhatsApp no pudo reiniciarse automáticamente.")
                except Exception:
                    restart_warnings.append("WhatsApp no pudo reiniciarse automáticamente.")

    response = {
        "ok": reset_error is None and not restart_warnings,
        "results": results,
        "restart_warnings": restart_warnings,
        "rollback_complete": rollback_complete,
        "state": _test_mode_payload(),
    }
    if reset_error:
        response["error"] = (
            reset_error
            if rollback_complete
            else (
                "El reinicio falló y la restauración no pudo completarse. "
                "Los respaldos se conservaron para recuperación manual."
            )
        )
        return jsonify(response), 500
    if restart_warnings:
        response["error"] = "El estado se reinició, pero un servicio requiere reinicio manual."
        return jsonify(response), 503
    return jsonify(response)


def _wa_switch_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _wa_qr_revision(path: Path | None = None) -> str | None:
    """Return an opaque content revision so clients reload only changed QRs."""

    target = path or WA_SWITCH_QR_FILE
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()[:24]
    except OSError:
        return None


def _load_wa_switch_operation() -> dict | None:
    try:
        parsed = json.loads(WA_SWITCH_OPERATION_FILE.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_wa_switch_operation(operation: dict) -> None:
    _secure_directory(WA_SWITCH_DIR)
    temporary = WA_SWITCH_OPERATION_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(operation, separators=(",", ":")), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, WA_SWITCH_OPERATION_FILE)


def _wa_switch_owned(operation: dict | None) -> bool:
    token = session.get("wa_switch_token")
    expected = operation.get("token_hash") if isinstance(operation, dict) else None
    return bool(
        isinstance(token, str)
        and isinstance(expected, str)
        and secrets.compare_digest(_wa_switch_token_digest(token), expected)
    )


def _cleanup_wa_switch_files() -> None:
    _stop_wa_process(WA_SWITCH_PID_FILE)
    if WA_SWITCH_DIR.exists():
        shutil.rmtree(WA_SWITCH_DIR)


def _cancel_wa_switch_expiry() -> None:
    global _wa_switch_expiry_timer
    if _wa_switch_expiry_timer is not None:
        _wa_switch_expiry_timer.cancel()
        _wa_switch_expiry_timer = None


def _cleanup_wa_switch_candidate() -> None:
    _cancel_wa_switch_expiry()
    _cleanup_wa_switch_files()
    session.pop("wa_switch_token", None)


def _schedule_wa_switch_expiry(operation: dict) -> None:
    """Detiene un QR abandonado aunque el navegador deje de consultar."""

    global _wa_switch_expiry_timer
    if _wa_switch_expiry_timer is not None:
        _wa_switch_expiry_timer.cancel()
    expected_hash = operation["token_hash"]
    expected_started_at = operation["started_at"]
    try:
        elapsed = max(0.0, time.time() - float(expected_started_at))
    except (TypeError, ValueError):
        elapsed = WA_SWITCH_TIMEOUT_SECONDS
    remaining = max(0.01, WA_SWITCH_TIMEOUT_SECONDS - elapsed)

    def expire() -> None:
        global _wa_switch_expiry_timer
        with _wa_switch_lock:
            current = _load_wa_switch_operation()
            if (
                isinstance(current, dict)
                and current.get("token_hash") == expected_hash
                and current.get("started_at") == expected_started_at
            ):
                _cleanup_wa_switch_files()
            _wa_switch_expiry_timer = None

    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    _wa_switch_expiry_timer = timer
    timer.start()


def _reap_expired_wa_switch() -> None:
    """Limpia staging vencido aunque Gunicorn se haya reiniciado."""

    operation = _load_wa_switch_operation()
    if not isinstance(operation, dict) or operation.get("status") == "recovery_required":
        return
    try:
        expired = time.time() - float(operation.get("started_at", 0)) >= WA_SWITCH_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        expired = True
    if expired:
        _cleanup_wa_switch_files()


def _promote_wa_candidate() -> tuple[bool, str, dict, bool, bool]:
    """Promueve la cuenta escaneada y restaura la anterior si no queda online."""

    with _wa_switch_lock, _wa_process_lock:
        operation = _load_wa_switch_operation()
        if not _wa_switch_owned(operation):
            return False, "El intento de cambio ya no pertenece a este navegador.", {}, True, _wa_connection_open()
        if operation.get("status") == "recovery_required" or _whatsapp_recovery_pending():
            return False, "Hay un respaldo pendiente de recuperación manual.", {}, True, _wa_connection_open()
        if not _wa_connection_open(WA_SWITCH_PID_FILE, WA_SWITCH_HEALTH_FILE):
            return False, "La cuenta nueva todavía no está conectada.", {}, True, _wa_connection_open()
        if not WA_SWITCH_AUTH_DIR.exists() or not any(WA_SWITCH_AUTH_DIR.iterdir()):
            return False, "WhatsApp no guardó la nueva sesión.", {}, True, _wa_connection_open()

        candidate_identity = _read_safe_identity(WA_SWITCH_IDENTITY_FILE)
        rollback_auth = WA_SWITCH_DIR / "rollback_auth"
        rollback_state = WA_SWITCH_DIR / "rollback_interaction_state.json"
        rollback_identity = WA_SWITCH_DIR / "rollback_identity.json"
        current_state = DATA_DIR / "wa_interaction_state.json"
        current_pid = DATA_DIR / "wa_bot.pid"
        current_qr = BASE_DIR / "wa_qr.png"
        old_was_running = _wa_process_running(current_pid)

        _stop_wa_process(WA_SWITCH_PID_FILE)
        _stop_wa_process(current_pid)
        try:
            if rollback_auth.exists():
                shutil.rmtree(rollback_auth)
            if WA_AUTH_DIR.exists():
                os.replace(WA_AUTH_DIR, rollback_auth)
            if current_state.exists():
                os.replace(current_state, rollback_state)
            if WA_IDENTITY_FILE.exists():
                os.replace(WA_IDENTITY_FILE, rollback_identity)
            os.replace(WA_SWITCH_AUTH_DIR, WA_AUTH_DIR)
            if WA_SWITCH_IDENTITY_FILE.exists():
                os.replace(WA_SWITCH_IDENTITY_FILE, WA_IDENTITY_FILE)
            WA_CALL_HEALTH_FILE.unlink(missing_ok=True)
            current_qr.unlink(missing_ok=True)
            started = restart_wa_bot()
            if not started or not _wait_until(_wa_connection_open, 15):
                raise RuntimeError("new_wa_worker_not_ready")
        except Exception:
            restored = True
            try:
                _stop_wa_process(current_pid)
                if WA_AUTH_DIR.exists():
                    shutil.rmtree(WA_AUTH_DIR)
                if rollback_auth.exists():
                    os.replace(rollback_auth, WA_AUTH_DIR)
                current_state.unlink(missing_ok=True)
                if rollback_state.exists():
                    os.replace(rollback_state, current_state)
                WA_IDENTITY_FILE.unlink(missing_ok=True)
                if rollback_identity.exists():
                    os.replace(rollback_identity, WA_IDENTITY_FILE)
            except Exception:
                restored = False

            old_ready = False
            if restored and old_was_running and WA_AUTH_DIR.exists():
                try:
                    started = restart_wa_bot()
                    old_ready = bool(started and _wait_until(_wa_connection_open, 15))
                except Exception:
                    old_ready = False

            if restored:
                _cleanup_wa_switch_candidate()
                message = (
                    "No se pudo activar la cuenta nueva; la anterior fue restaurada."
                    if old_ready
                    else "La cuenta anterior fue restaurada, pero su servicio no quedó en línea."
                )
            else:
                recovery_dir = WA_SWITCH_RECOVERY_ROOT / (
                    f"{int(time.time())}-{secrets.token_hex(4)}"
                )
                recovery_saved = False
                try:
                    recovery_dir.mkdir(parents=True, exist_ok=False)
                    try:
                        os.chmod(WA_SWITCH_RECOVERY_ROOT, 0o700)
                        os.chmod(recovery_dir, 0o700)
                    except OSError:
                        pass
                    for artifact in (rollback_auth, rollback_state, rollback_identity):
                        if artifact.exists():
                            os.replace(artifact, recovery_dir / artifact.name)
                    recovery_saved = any(recovery_dir.iterdir())
                except Exception:
                    recovery_saved = False
                if recovery_saved:
                    _cleanup_wa_switch_candidate()
                    message = (
                        "Falló la activación y la restauración automática. "
                        "El respaldo disponible se conservó para recuperación manual."
                    )
                else:
                    _cancel_wa_switch_expiry()
                    _stop_wa_process(WA_SWITCH_PID_FILE)
                    operation["status"] = "recovery_required"
                    _save_wa_switch_operation(operation)
                    message = (
                        "Falló la activación y no fue posible mover el respaldo. "
                        "Los archivos de recuperación permanecen en el directorio de cambio."
                    )
            return False, message, {}, restored, old_ready

        if rollback_auth.exists():
            shutil.rmtree(rollback_auth, ignore_errors=True)
        rollback_state.unlink(missing_ok=True)
        rollback_identity.unlink(missing_ok=True)
        _cleanup_wa_switch_candidate()
        session["channel_admin"] = True
        session.permanent = True
        return True, "Cuenta de WhatsApp cambiada y verificada.", candidate_identity, True, True

@app.route("/api/restart_bot", methods=["POST"])
def api_restart_bot():
    """Reinicia el worker rastreado y no devuelve logs internos."""
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    try:
        with _telegram_switch_lock:
            started, message = restart_telegram_worker()
        status = 200 if started else 409
        return jsonify({"ok": started, "message": message, **({} if started else {"error": message})}), status
    except Exception:
        return jsonify({"ok": False, "error": "No fue posible reiniciar Telegram."}), 500

@app.route("/api/restart_wa_bot", methods=["POST"])
def api_restart_wa_bot():
    """Reinicia el servicio sin borrar la cuenta vinculada."""
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    health = _read_wa_call_health(WA_CALL_HEALTH_FILE)
    connection_open = (
        _wa_process_running(DATA_DIR / "wa_bot.pid")
        and health.get("connection") == "open"
    )
    reauth_required = bool(health.get("reauth_required")) or health.get(
        "disconnect_reason"
    ) in {"logged_out", "session_invalid"}
    if not connection_open and reauth_required:
        return jsonify({
            "ok": False,
            "error_code": "reauth_required",
            "error": "WhatsApp cerró esta sesión. Usa «Volver a vincular» para generar un QR nuevo.",
        }), 409
    try:
        pid = restart_wa_bot()
        if not pid:
            return jsonify({"ok": False, "error": "WhatsApp no pudo iniciar."}), 503
        return jsonify({"ok": True, "message": "WhatsApp reiniciado sin cambiar la cuenta."})
    except Exception:
        return jsonify({"ok": False, "error": "No fue posible reiniciar WhatsApp."}), 500


@app.route("/api/reset_wa", methods=["POST"])
def api_reset_wa():
    """El reset destructivo fue sustituido por el cambio transaccional."""
    return jsonify({
        "ok": False,
        "error_code": "use_transactional_switch",
        "error": "Usa «Cambiar cuenta» para conservar la sesión anterior hasta verificar la nueva.",
    }), 410


@app.route("/api/switch_wa", methods=["POST"])
def api_switch_wa():
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "No se recibió una solicitud válida."}), 400
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "Confirma el cambio de cuenta."}), 400

    with _wa_switch_lock:
        _reap_expired_wa_switch()
        existing = _load_wa_switch_operation()
        if existing and not _wa_process_running(WA_SWITCH_PID_FILE):
            _cleanup_wa_switch_candidate()
            existing = None

        if _whatsapp_recovery_pending() or (
            existing and existing.get("status") == "recovery_required"
        ):
            return jsonify({
                "ok": False,
                "error_code": "recovery_required",
                "error": "Hay un respaldo pendiente de recuperación manual; no se inició otro cambio.",
            }), 409
        if existing:
            try:
                elapsed = max(0.0, time.time() - float(existing.get("started_at", 0)))
            except (TypeError, ValueError):
                elapsed = WA_SWITCH_TIMEOUT_SECONDS
            if elapsed < WA_SWITCH_TIMEOUT_SECONDS and _wa_process_running(WA_SWITCH_PID_FILE):
                owned = _wa_switch_owned(existing)
                return jsonify({
                    "ok": False,
                    "state": "switching" if owned else "switching_elsewhere",
                    "error_code": "switch_in_progress",
                    "owned_by_this_browser": owned,
                    "retry_after": max(1, int(WA_SWITCH_TIMEOUT_SECONDS - elapsed)),
                    "error": (
                        "Ya hay un cambio de WhatsApp en curso en este navegador."
                        if owned
                        else "Ya hay una vinculación de WhatsApp abierta en otro navegador."
                    ),
                }), 409
        _cleanup_wa_switch_candidate()


        _secure_directory(WA_SWITCH_AUTH_DIR)
        token = secrets.token_urlsafe(32)
        operation = {
            "version": 1,
            "token_hash": _wa_switch_token_digest(token),
            "started_at": time.time(),
            "status": "preparing",
        }
        _save_wa_switch_operation(operation)
        _schedule_wa_switch_expiry(operation)
        session["wa_switch_token"] = token
        session.permanent = True
        pid = _start_wa_process(
            auth_dir=WA_SWITCH_AUTH_DIR,
            qr_file=WA_SWITCH_QR_FILE,
            health_file=WA_SWITCH_HEALTH_FILE,
            identity_file=WA_SWITCH_IDENTITY_FILE,
            pid_file=WA_SWITCH_PID_FILE,
            link_only=True,
            log_name="bot_wa_switch.log",
        )
        if not pid:
            _cleanup_wa_switch_candidate()
            return jsonify({"ok": False, "error": "No fue posible preparar el QR."}), 503
    return jsonify({"ok": True, "state": "preparing"}), 202


@app.route("/api/switch_wa/claim", methods=["POST"])
def api_switch_wa_claim():
    """Transfiere a otro navegador administrador un emparejamiento vigente."""

    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "No se recibió una solicitud válida."}), 400
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "Confirma que deseas continuar la vinculación aquí."}), 400

    with _wa_switch_lock:
        _reap_expired_wa_switch()
        operation = _load_wa_switch_operation()
        if _whatsapp_recovery_pending() or (
            operation and operation.get("status") == "recovery_required"
        ):
            return jsonify({
                "ok": False,
                "error_code": "recovery_required",
                "error": "Hay un respaldo pendiente de recuperación manual.",
            }), 409
        if not operation:
            return jsonify({
                "ok": False,
                "error_code": "switch_missing",
                "error": "La vinculación anterior ya terminó o venció. Inicia un QR nuevo.",
            }), 404
        if not _wa_process_running(WA_SWITCH_PID_FILE):
            _cleanup_wa_switch_candidate()
            return jsonify({
                "ok": False,
                "error_code": "candidate_stopped",
                "error": "El intento anterior se detuvo. Pulsa nuevamente para generar un QR nuevo.",
            }), 410

        token = secrets.token_urlsafe(32)
        operation["token_hash"] = _wa_switch_token_digest(token)
        _save_wa_switch_operation(operation)
        _schedule_wa_switch_expiry(operation)
        session["wa_switch_token"] = token
        session.permanent = True
        qr_revision = _wa_qr_revision()
        qr_ready = qr_revision is not None
        scanned = _wa_connection_open(WA_SWITCH_PID_FILE, WA_SWITCH_HEALTH_FILE)
        return jsonify({
            "ok": True,
            "state": "scanned" if scanned else ("awaiting_qr" if qr_ready else "preparing"),
            "qr_ready": qr_ready,
            "qr_revision": qr_revision,
            "ready_to_commit": scanned,
        })


@app.route("/api/switch_wa/status")
def api_switch_wa_status():
    if not _can_manage_channels():
        return jsonify({"ok": False, "error": "Sesión administrativa requerida."}), 403
    with _wa_switch_lock:
        operation = _load_wa_switch_operation()
        if not _wa_switch_owned(operation):
            return jsonify({"ok": False, "error": "No hay un cambio activo en este navegador."}), 404
        if operation.get("status") == "recovery_required":
            return jsonify({
                "ok": False,
                "state": "recovery_required",
                "error": "La recuperación automática no terminó; el respaldo sigue conservado.",
            }), 503
        if time.time() - float(operation.get("started_at", 0)) >= WA_SWITCH_TIMEOUT_SECONDS:
            _cleanup_wa_switch_candidate()
            return jsonify({"ok": False, "state": "expired", "error": "El QR venció; inicia otro cambio."}), 410

        if _wa_connection_open(WA_SWITCH_PID_FILE, WA_SWITCH_HEALTH_FILE):
            return jsonify({
                "ok": True,
                "state": "scanned",
                "ready_to_commit": True,
            })
        if not _wa_process_running(WA_SWITCH_PID_FILE):
            _cleanup_wa_switch_candidate()
            return jsonify({
                "ok": False,
                "state": "failed",
                "error": "El emparejamiento se detuvo; la cuenta anterior sigue activa.",
            }), 503
        qr_revision = _wa_qr_revision()
        return jsonify({
            "ok": True,
            "state": "awaiting_qr" if qr_revision is not None else "preparing",
            "qr_ready": qr_revision is not None,
            "qr_revision": qr_revision,
        })


@app.route("/api/switch_wa/commit", methods=["POST"])
def api_switch_wa_commit():
    """Promueve exactamente una vez la cuenta candidata ya verificada."""

    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    with _wa_switch_lock:
        operation = _load_wa_switch_operation()
        if not _wa_switch_owned(operation):
            return jsonify({"ok": False, "error": "No hay un cambio activo en este navegador."}), 404
        if operation.get("status") == "recovery_required" or _whatsapp_recovery_pending():
            return jsonify({
                "ok": False,
                "error_code": "recovery_required",
                "error": "Hay un respaldo pendiente de recuperación manual.",
            }), 409
        if not _wa_connection_open(WA_SWITCH_PID_FILE, WA_SWITCH_HEALTH_FILE):
            return jsonify({
                "ok": False,
                "error_code": "candidate_not_ready",
                "error": "La cuenta nueva todavía no terminó de vincularse.",
            }), 409
        switched, message, identity, previous_preserved, previous_ready = _promote_wa_candidate()
    status = 200 if switched else 503
    return jsonify({
        "ok": switched,
        "state": "ready" if switched else (
            "rollback_restored" if previous_preserved else "recovery_required"
        ),
        "message": message,
        "identity": identity if switched else None,
        **({} if switched else {
            "error": message,
            "previous_account_preserved": previous_preserved,
            "previous_worker_ready": previous_ready,
        }),
    }), status


@app.route("/api/switch_wa/qr")
def api_switch_wa_qr():
    if not _can_manage_channels():
        return jsonify({"ok": False, "error": "Sesión administrativa requerida."}), 403
    operation = _load_wa_switch_operation()
    if not _wa_switch_owned(operation) or not WA_SWITCH_QR_FILE.is_file():
        return jsonify({"ok": False, "error": "QR no disponible."}), 404
    response = send_from_directory(str(WA_SWITCH_DIR), WA_SWITCH_QR_FILE.name)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/switch_wa/cancel", methods=["POST"])
def api_switch_wa_cancel():
    security_error = _channel_mutation_error()
    if security_error:
        return security_error
    with _wa_switch_lock:
        operation = _load_wa_switch_operation()
        if not _wa_switch_owned(operation):
            return jsonify({"ok": False, "error": "No hay un cambio activo en este navegador."}), 404
        if operation.get("status") == "recovery_required":
            return jsonify({
                "ok": False,
                "error_code": "recovery_required",
                "error": "No se eliminó el respaldo pendiente de recuperación.",
            }), 409
        _cleanup_wa_switch_candidate()
    return jsonify({"ok": True, "message": "Cambio cancelado; la cuenta anterior sigue activa."})


@app.route("/api/channels")
def api_channels():
    """Resumen seguro para la UX de vinculación y cambio de cuentas."""

    can_manage = _can_manage_channels()
    tg_authorized = _telegram_session_is_authorized()
    tg_ready = bot_is_running()
    tg_identity = _read_safe_identity(DATA_DIR / "tg_identity.json")
    tg_phone = os.environ.get("TG_PHONE") or _read_env_var("TG_PHONE")

    wa_auth_present = WA_AUTH_DIR.exists() and any(WA_AUTH_DIR.iterdir())
    wa_worker_running = _wa_process_running(DATA_DIR / "wa_bot.pid")
    wa_ready = _wa_connection_open()
    wa_health = _read_wa_call_health(WA_CALL_HEALTH_FILE)
    wa_identity = _read_safe_identity(WA_IDENTITY_FILE)
    with _wa_switch_lock:
        _reap_expired_wa_switch()
        wa_operation = _load_wa_switch_operation()
    wa_switch_active = bool(wa_operation)
    wa_switch_owned = bool(wa_operation and _wa_switch_owned(wa_operation))
    wa_switch_elsewhere = bool(can_manage and wa_switch_active and not wa_switch_owned)
    tg_recovery_required = _telegram_recovery_pending()
    wa_recovery_required = _whatsapp_recovery_pending() or bool(
        wa_operation and wa_operation.get("status") == "recovery_required"
    )
    wa_reauth_required = bool(wa_auth_present) and not wa_ready and (
        bool(wa_health.get("reauth_required"))
        or wa_health.get("disconnect_reason") in {"logged_out", "session_invalid"}
    )

    response = jsonify({
        "ok": True,
        "csrf": _channel_csrf_token(),
        "can_manage": can_manage,
        "telegram": {
            "linked": tg_authorized,
            "ready": tg_ready,
            "state": "recovery_required" if tg_recovery_required else (
                "ready" if tg_ready else ("offline" if tg_authorized else "unlinked")
            ),
            "display_name": tg_identity.get("display_name") if tg_authorized and can_manage else None,
            "username": tg_identity.get("username") if tg_authorized and can_manage else None,
            "phone_hint": _mask_phone(tg_phone) if tg_authorized and can_manage else None,
        },
        "whatsapp": {
            "linked": wa_auth_present,
            "ready": wa_ready,
            "worker_running": wa_worker_running,
            "state": (
                "recovery_required" if wa_recovery_required
                else "switching" if wa_switch_owned
                else "switching_elsewhere" if wa_switch_elsewhere
                else "reauth_required" if wa_reauth_required
                else "ready" if wa_ready
                else "offline" if wa_auth_present
                else "unlinked"
            ),
            "display_name": wa_identity.get("display_name") if wa_auth_present and can_manage else None,
            "phone_hint": wa_identity.get("phone_hint") if wa_auth_present and can_manage else None,
            "reauth_required": wa_reauth_required,
            "switch_in_progress": wa_switch_active if can_manage else False,
            "switch_owned_by_this_browser": wa_switch_owned if can_manage else False,
        },
    })
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/start_botfather", methods=["POST"])
def api_start_botfather():
    """Arranca el proceso botfather_bot.py si no está corriendo."""
    if bf_is_running():
        return jsonify({"ok": True, "message": "BotFather ya está corriendo"})

    token = os.environ.get("AUTOREPLY_BOT_TOKEN") or _read_env_var("AUTOREPLY_BOT_TOKEN")
    if not token:
        return jsonify({"ok": False, "error": "Token no configurado"}), 400

    try:
        import time, subprocess, sys
        
        # Asegurar que python-telegram-bot esté instalado
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "python-telegram-bot>=21.0", "-q"],
            capture_output=True, timeout=30
        )
        
        bot_script = str(BASE_DIR / "botfather_bot.py")
        log_file = str(BASE_DIR / "botfather_bot.log")
        pid_file = str(DATA_DIR / "botfather.pid")
        with open(log_file, "a") as f:
            f.write(f"\n--- Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            proc = subprocess.Popen(
                [sys.executable, bot_script],
                stdout=f, stderr=subprocess.STDOUT,
                env=_channel_worker_environment(),
                start_new_session=True
            )
            # Guardar PID
            with open(pid_file, "w") as pf:
                pf.write(str(proc.pid))
        return jsonify({"ok": True, "message": "BotFather iniciado"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_supervisor_thread_started = False
_supervisor_lock = threading.Lock()

def _supervise_background_services_once() -> None:
    """Run one coordinated supervisor pass without racing account/test changes."""

    with _wa_switch_lock:
        wa_auth_present = WA_AUTH_DIR.exists() and any(WA_AUTH_DIR.iterdir())
        wa_switching = bool(_load_wa_switch_operation())
        if wa_auth_present and not wa_switching and not _wa_process_running(DATA_DIR / "wa_bot.pid"):
            health = _read_wa_call_health()
            if not health.get("reauth_required"):
                print("[Supervisor] WhatsApp no está corriendo pero hay sesión. Reiniciando servicio...")
                restart_wa_bot()

    with _telegram_switch_lock:
        if (
            _telegram_session_is_authorized()
            and not bot_is_running()
            and not _telegram_switch_auth.has_pending()
        ):
            print("[Supervisor] Telegram UserBot no está corriendo pero está autorizado. Reiniciando servicio...")
            restart_telegram_worker()

    token = os.environ.get("AUTOREPLY_BOT_TOKEN") or _read_env_var("AUTOREPLY_BOT_TOKEN")
    if token and not bf_is_running():
        print("[Supervisor] BotFather bot no está corriendo pero hay token. Reiniciando servicio...")
        api_start_botfather()


def _background_service_supervisor():
    """Supervisa de forma continua los procesos de fondo en producción.
    Si algún servicio cae pero la sesión sigue activa, lo reinicia automáticamente."""
    time.sleep(10)
    while True:
        try:
            _supervise_background_services_once()
        except Exception:
            pass

        time.sleep(20)

def _ensure_supervisor_running():
    global _supervisor_thread_started
    with _supervisor_lock:
        if not _supervisor_thread_started:
            _supervisor_thread_started = True
            t = threading.Thread(target=_background_service_supervisor, daemon=True)
            t.start()

_ensure_supervisor_running()


# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")  # 0.0.0.0 para acceder desde la red
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"🚀 Panel AutoReply corriendo en http://localhost:{port}")
    print(f"📁 Mensajes: {MESSAGES_FILE}")
    print(f"🎵 Audios: {AUDIO_DIR}")
    print(f"🔄 Para producción: set HOST=0.0.0.0 PORT=5000 FLASK_SECRET=...")
    print(f"   Y usa gunicorn o espera a que te ayude a configurarlo.")

    app.run(host=host, port=port, debug=debug)

