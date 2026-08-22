import { DisconnectReason } from '@whiskeysockets/baileys';

const INVALID_SESSION_STATUSES = new Set([
  DisconnectReason.loggedOut,
  DisconnectReason.badSession,
  DisconnectReason.connectionReplaced,
  DisconnectReason.multideviceMismatch,
  DisconnectReason.forbidden,
]);

const DISCONNECT_DIAGNOSTICS = new Map([
  [429, {
    statusName: 'provider_rate_limited',
    category: 'provider_restriction',
    action: 'retry_backoff',
  }],
  [DisconnectReason.loggedOut, {
    statusName: 'logged_out',
    category: 'authorization',
    action: 'relink',
  }],
  [DisconnectReason.badSession, {
    statusName: 'bad_session',
    category: 'authorization',
    action: 'relink_cleanly',
  }],
  [DisconnectReason.connectionReplaced, {
    statusName: 'connection_replaced',
    category: 'session_conflict',
    action: 'check_duplicate_session',
  }],
  [DisconnectReason.multideviceMismatch, {
    statusName: 'multidevice_mismatch',
    category: 'session_mismatch',
    action: 'relink_cleanly',
  }],
  [DisconnectReason.forbidden, {
    statusName: 'forbidden',
    category: 'provider_restriction',
    action: 'review_provider',
  }],
  [DisconnectReason.connectionClosed, {
    statusName: 'connection_closed',
    category: 'transport',
    action: 'automatic_reconnect',
  }],
  [DisconnectReason.connectionLost, {
    // Baileys assigns 408 to both connectionLost and timedOut. Persist the
    // ambiguity instead of inventing a more precise cause after the fact.
    statusName: 'connection_lost_or_timeout',
    category: 'transport',
    action: 'automatic_reconnect',
  }],
  [DisconnectReason.restartRequired, {
    statusName: 'restart_required',
    category: 'process',
    action: 'automatic_reconnect',
  }],
  [DisconnectReason.unavailableService, {
    statusName: 'service_unavailable',
    category: 'provider_availability',
    action: 'retry_backoff',
  }],
]);

function normalizedStatusCode(status) {
  const parsed = Number.parseInt(String(status ?? ''), 10);
  return Number.isFinite(parsed) && parsed >= 100 && parsed <= 599 ? parsed : null;
}

/**
 * Returns a low-cardinality forensic description. It deliberately accepts
 * only a numeric provider status and never persists raw errors or payloads.
 */
export function diagnoseWhatsAppDisconnect(status) {
  const statusCode = normalizedStatusCode(status);
  const decision = classifyWhatsAppDisconnect(statusCode);
  const diagnostic = DISCONNECT_DIAGNOSTICS.get(statusCode) || {
    statusName: 'unknown',
    category: 'unknown',
    action: decision.shouldReconnect ? 'automatic_reconnect' : 'inspect_worker',
  };
  return {
    statusCode,
    ...diagnostic,
    terminal: decision.terminateWorker,
    reauthRequired: decision.reauthRequired,
    shouldReconnect: decision.shouldReconnect,
  };
}

/** Invalid credentials require a fresh QR and must not leave a dead worker alive. */
export function classifyWhatsAppDisconnect(status) {
  const statusCode = normalizedStatusCode(status);
  const reauthRequired = INVALID_SESSION_STATUSES.has(statusCode);
  return {
    reauthRequired,
    shouldReconnect: !reauthRequired,
    terminateWorker: reauthRequired,
    reason: reauthRequired
      ? (statusCode === DisconnectReason.loggedOut ? 'logged_out' : 'session_invalid')
      : 'transient',
  };
}
