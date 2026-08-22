import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const STATE_SCHEMA = 1;
const LEDGER_SCHEMA = 1;
const RECENT_RETENTION_MS = 24 * 60 * 60 * 1000;
const MAX_RECENT_EVENTS = 1_000;
const DUPLICATE_DISCONNECT_MS = 5_000;
const LEASE_FRESH_MS = 90_000;

const CONDITIONS = new Set(['starting', 'healthy', 'warning', 'degraded', 'critical', 'unknown']);
const CONNECTIONS = new Set(['starting', 'connecting', 'open', 'closed', 'unknown']);
const LIKELIHOODS = new Set(['low', 'elevated', 'high', 'unknown']);
const SIGNALS = new Set([
  'auth_unregistered',
  'auth_write_failure',
  'backoff_active',
  'circuit_open',
  'connection_timeout',
  'connection_unstable',
  'delivery_failures',
  'frequent_reconnects',
  'ledger_integrity_invalid',
  'local_rate_limit',
  'operator_paused',
  'provider_forbidden',
  'provider_rate_limited',
  'slow_connect',
  'startup_failure',
  'terminal_session_failure',
  'worker_lease_conflict',
]);
const FAILURE_MODES = new Set([
  'authentication_loss',
  'delivery_suspension',
  'duplicate_session',
  'local_protection',
  'provider_restriction',
  'rate_limit',
  'session_mismatch',
  'transport_instability',
  'unknown',
]);
const EVENT_TYPES = new Set([
  'auth_unregistered',
  'auth_write_failure',
  'connection_timeout',
  'delivery_failure',
  'delivery_timeout',
  'disconnect_terminal',
  'disconnect_transient',
  'local_rate_limit',
  'provider_forbidden',
  'provider_rate_limited',
  'queue_full',
  'queue_timeout',
  'reconnect_scheduled',
  'slow_connect',
  'startup_failure',
  'worker_lease_conflict',
]);
const LEDGER_EVENTS = new Set([
  'connection_closed',
  'connection_open',
  'health_transition',
  'operational_signal',
  'reconnect_scheduled',
  'worker_shutdown',
  'worker_started',
]);
const LIFECYCLES = new Set(['opened', 'updated', 'resolved', 'signal', 'observation']);
const STATUS_NAMES = new Set([
  'auth_unregistered',
  'auth_write_failure',
  'bad_session',
  'connection_closed',
  'connection_lost_or_timeout',
  'connection_replaced',
  'connection_timeout',
  'delivery_failure',
  'delivery_timeout',
  'forbidden',
  'local_rate_limit',
  'logged_out',
  'multidevice_mismatch',
  'provider_forbidden',
  'provider_rate_limited',
  'queue_full',
  'queue_timeout',
  'restart_required',
  'service_unavailable',
  'slow_connect',
  'startup_failure',
  'unknown',
  'worker_lease_conflict',
  'worker_shutdown',
]);
const CATEGORIES = new Set([
  'authorization',
  'delivery',
  'local_protection',
  'process',
  'provider_availability',
  'provider_restriction',
  'session_conflict',
  'session_mismatch',
  'transport',
  'unknown',
]);
const ACTIONS = new Set([
  'automatic_reconnect',
  'check_duplicate_session',
  'inspect_delivery',
  'inspect_worker',
  'none',
  'relink',
  'relink_cleanly',
  'retry_backoff',
  'review_provider',
]);
const SHUTDOWN_REASONS = new Set(['SIGINT', 'SIGTERM', 'link-timeout', 'unknown']);

const OPERATIONAL_DIAGNOSTICS = {
  provider_forbidden: {
    statusCode: 403, statusName: 'provider_forbidden', category: 'provider_restriction',
    action: 'review_provider', terminal: false, reauthRequired: false,
  },
  provider_rate_limited: {
    statusCode: 429, statusName: 'provider_rate_limited', category: 'provider_restriction',
    action: 'retry_backoff', terminal: false, reauthRequired: false,
  },
  delivery_failure: {
    statusCode: null, statusName: 'delivery_failure', category: 'delivery',
    action: 'inspect_delivery', terminal: false, reauthRequired: false,
  },
  delivery_timeout: {
    statusCode: null, statusName: 'delivery_timeout', category: 'delivery',
    action: 'inspect_delivery', terminal: false, reauthRequired: false,
  },
  local_rate_limit: {
    statusCode: null, statusName: 'local_rate_limit', category: 'local_protection',
    action: 'retry_backoff', terminal: false, reauthRequired: false,
  },
  queue_full: {
    statusCode: null, statusName: 'queue_full', category: 'local_protection',
    action: 'inspect_delivery', terminal: false, reauthRequired: false,
  },
  queue_timeout: {
    statusCode: null, statusName: 'queue_timeout', category: 'local_protection',
    action: 'inspect_delivery', terminal: false, reauthRequired: false,
  },
  connection_timeout: {
    statusCode: null, statusName: 'connection_timeout', category: 'transport',
    action: 'automatic_reconnect', terminal: false, reauthRequired: false,
  },
  slow_connect: {
    statusCode: null, statusName: 'slow_connect', category: 'transport',
    action: 'automatic_reconnect', terminal: false, reauthRequired: false,
  },
  startup_failure: {
    statusCode: null, statusName: 'startup_failure', category: 'process',
    action: 'inspect_worker', terminal: false, reauthRequired: false,
  },
  auth_unregistered: {
    statusCode: null, statusName: 'auth_unregistered', category: 'authorization',
    action: 'relink', terminal: false, reauthRequired: true,
  },
  auth_write_failure: {
    statusCode: null, statusName: 'auth_write_failure', category: 'authorization',
    action: 'inspect_worker', terminal: false, reauthRequired: false,
  },
  worker_lease_conflict: {
    statusCode: null, statusName: 'worker_lease_conflict', category: 'session_conflict',
    action: 'check_duplicate_session', terminal: false, reauthRequired: false,
  },
};

const LEDGER_BASE_KEYS = [
  'schema_version', 'sequence', 'record_id', 'at', 'event', 'lifecycle',
  'incident_id', 'condition', 'status_code', 'status_name', 'category',
  'action', 'terminal', 'reauth_required', 'reconnect_attempt',
  'reconnect_delay_ms', 'signals', 'previous_hash',
];

function enumValue(value, choices, fallback) {
  return typeof value === 'string' && choices.has(value) ? value : fallback;
}

function timestamp(value) {
  return Number.isFinite(value) && value > 0 ? Math.trunc(value) : null;
}

function statusCode(value) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed >= 100 && parsed <= 599 ? parsed : null;
}

function boundedInteger(value, fallback = 0, maximum = 1_000_000) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? Math.min(Math.max(parsed, 0), maximum) : fallback;
}

function identifier(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,96}$/.test(value) ? value : null;
}

function recordHash(value) {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : null;
}

function signalList(values) {
  return [...new Set(Array.isArray(values) ? values : [])]
    .filter(value => SIGNALS.has(value))
    .sort();
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function atomicWrite(filePath, payload, fsModule, logger) {
  const temporary = `${filePath}.${process.pid}.tmp`;
  try {
    fsModule.mkdirSync(path.dirname(filePath), { recursive: true });
    fsModule.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
      encoding: 'utf8', mode: 0o600,
    });
    if (typeof fsModule.fsyncSync === 'function' && typeof fsModule.openSync === 'function') {
      try {
        const descriptor = fsModule.openSync(temporary, 'r+');
        try { fsModule.fsyncSync(descriptor); } finally { fsModule.closeSync(descriptor); }
      } catch {
        // The atomic snapshot remains useful when a platform rejects fsync.
      }
    }
    try {
      fsModule.renameSync(temporary, filePath);
    } catch {
      fsModule.rmSync(filePath, { force: true });
      fsModule.renameSync(temporary, filePath);
    }
    return true;
  } catch {
    try { fsModule.rmSync(temporary, { force: true }); } catch {}
    logger.warn?.('[WA INCIDENT] Health state could not be persisted');
    return false;
  }
}

function ledgerBase(record) {
  return Object.fromEntries(LEDGER_BASE_KEYS.map(key => [key, record[key]]));
}

function hashRecord(base) {
  return createHash('sha256').update(JSON.stringify(base), 'utf8').digest('hex');
}

function auditLedger(ledgerPath, fsModule) {
  let raw;
  try {
    raw = fsModule.readFileSync(ledgerPath, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') {
      return { integrity: 'verified', sequence: 0, head: null, records: 0 };
    }
    return { integrity: 'unavailable', sequence: 0, head: null, records: 0 };
  }
  let expectedHash = null;
  let expectedSequence = 0;
  let records = 0;
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let parsed;
    try { parsed = JSON.parse(line); } catch {
      return { integrity: 'invalid', sequence: expectedSequence, head: expectedHash, records };
    }
    const base = ledgerBase(parsed);
    const keys = Object.keys(parsed).sort();
    const expectedKeys = [...LEDGER_BASE_KEYS, 'record_hash'].sort();
    if (
      keys.length !== expectedKeys.length
      || keys.some((key, index) => key !== expectedKeys[index])
      || parsed.schema_version !== LEDGER_SCHEMA
      || parsed.sequence !== expectedSequence + 1
      || parsed.previous_hash !== expectedHash
      || parsed.record_hash !== hashRecord(base)
    ) {
      return { integrity: 'invalid', sequence: expectedSequence, head: expectedHash, records };
    }
    expectedHash = parsed.record_hash;
    expectedSequence = parsed.sequence;
    records += 1;
  }
  return { integrity: 'verified', sequence: expectedSequence, head: expectedHash, records };
}

function sanitizedFailure(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const at = timestamp(raw.at);
  const statusName = enumValue(raw.status_name, STATUS_NAMES, 'unknown');
  if (!at) return null;
  return {
    failure_id: identifier(raw.failure_id),
    incident_id: identifier(raw.incident_id),
    at,
    status_code: statusCode(raw.status_code),
    status_name: statusName,
    category: enumValue(raw.category, CATEGORIES, 'unknown'),
    action: enumValue(raw.action, ACTIONS, 'inspect_worker'),
    terminal: raw.terminal === true,
    reauth_required: raw.reauth_required === true,
  };
}

function sanitizedIncident(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const incidentId = identifier(raw.incident_id);
  const startedAt = timestamp(raw.started_at);
  if (!incidentId || !startedAt) return null;
  return {
    incident_id: incidentId,
    started_at: startedAt,
    last_event_at: timestamp(raw.last_event_at) || startedAt,
    resolved_at: timestamp(raw.resolved_at),
    status_code: statusCode(raw.status_code),
    status_name: enumValue(raw.status_name, STATUS_NAMES, 'unknown'),
    category: enumValue(raw.category, CATEGORIES, 'unknown'),
    action: enumValue(raw.action, ACTIONS, 'inspect_worker'),
    terminal: raw.terminal === true,
    reauth_required: raw.reauth_required === true,
    disconnect_count: boundedInteger(raw.disconnect_count, 1, 10_000),
    reconnect_attempts: boundedInteger(raw.reconnect_attempts, 0, 10_000),
    precursors: signalList(raw.precursors),
  };
}

function sanitizedRecentEvents(raw, currentTime) {
  const floor = currentTime - RECENT_RETENTION_MS;
  return (Array.isArray(raw) ? raw : [])
    .filter(event => event && EVENT_TYPES.has(event.type))
    .map(event => ({
      type: event.type,
      at: timestamp(event.at),
      status_code: statusCode(event.status_code),
    }))
    .filter(event => event.at && event.at >= floor && event.at <= currentTime + 60_000)
    .slice(-MAX_RECENT_EVENTS);
}

function loadPriorState(filePath, fsModule, currentTime) {
  let raw;
  try { raw = JSON.parse(fsModule.readFileSync(filePath, 'utf8')); } catch { raw = null; }
  if (!raw || raw.schema_version !== STATE_SCHEMA) return null;
  return {
    worker_revision: identifier(raw.worker_revision),
    heartbeat_at: timestamp(raw.heartbeat_at),
    connection: enumValue(raw.connection, CONNECTIONS, 'unknown'),
    connection_since: timestamp(raw.connection_since),
    last_open_at: timestamp(raw.last_open_at),
    last_disconnect_at: timestamp(raw.last_disconnect_at),
    recent_events: sanitizedRecentEvents(raw.recent_events, currentTime),
    active_incident: sanitizedIncident(raw.active_incident),
    last_incident: sanitizedIncident(raw.last_incident),
    last_failure: sanitizedFailure(raw.last_failure),
    ledger_sequence: boundedInteger(raw.ledger_sequence),
    ledger_head: recordHash(raw.ledger_head),
  };
}

function normalizedDiagnostic(raw = {}) {
  return {
    statusCode: statusCode(raw.statusCode ?? raw.status_code),
    statusName: enumValue(raw.statusName ?? raw.status_name, STATUS_NAMES, 'unknown'),
    category: enumValue(raw.category, CATEGORIES, 'unknown'),
    action: enumValue(raw.action, ACTIONS, 'inspect_worker'),
    terminal: raw.terminal === true,
    reauthRequired: (raw.reauthRequired ?? raw.reauth_required) === true,
  };
}

function failureModesFor(signals) {
  const modes = new Set();
  if (signals.includes('terminal_session_failure')) modes.add('authentication_loss');
  if (signals.includes('worker_lease_conflict')) modes.add('duplicate_session');
  if (signals.includes('provider_forbidden')) modes.add('provider_restriction');
  if (signals.includes('provider_rate_limited')) modes.add('rate_limit');
  if (signals.some(value => ['frequent_reconnects', 'connection_timeout', 'connection_unstable', 'slow_connect'].includes(value))) {
    modes.add('transport_instability');
  }
  if (signals.includes('auth_unregistered') || signals.includes('auth_write_failure')) {
    modes.add('authentication_loss');
  }
  if (signals.includes('operator_paused')) modes.add('delivery_suspension');
  if (signals.some(value => ['local_rate_limit', 'circuit_open', 'backoff_active'].includes(value))) {
    modes.add('local_protection');
  }
  if (signals.includes('ledger_integrity_invalid')) modes.add('unknown');
  return [...modes].filter(value => FAILURE_MODES.has(value)).sort();
}

export function createWhatsAppIncidentMonitor({
  stateFile,
  ledgerFile,
  workerRevision,
  logger = console,
  fsModule = fs,
  now = () => Date.now(),
  idFactory = randomUUID,
} = {}) {
  if (!stateFile || !ledgerFile) throw new TypeError('stateFile and ledgerFile are required');
  if (typeof now !== 'function' || typeof idFactory !== 'function') {
    throw new TypeError('now and idFactory must be functions');
  }

  const currentTime = now();
  const previous = loadPriorState(stateFile, fsModule, currentTime);
  const revision = identifier(workerRevision) || identifier(idFactory()) || 'worker';
  const writerLeaseFile = `${ledgerFile}.writer`;
  const writerConflictFile = `${ledgerFile}.conflict`;
  const auditedLedger = auditLedger(ledgerFile, fsModule);
  const ledgerRolledBack = Boolean(
    previous
    && auditedLedger.integrity === 'verified'
    && (
      auditedLedger.sequence < previous.ledger_sequence
      || (
        auditedLedger.sequence === previous.ledger_sequence
        && auditedLedger.head !== previous.ledger_head
      )
    )
  );
  const ledger = ledgerRolledBack
    ? { ...auditedLedger, integrity: 'invalid' }
    : auditedLedger;
  let ownsWriterLease = false;
  let writerLeaseConflict = false;
  const state = {
    schema_version: STATE_SCHEMA,
    worker_revision: revision,
    updated_at: currentTime,
    heartbeat_at: null,
    connection: 'starting',
    connection_since: currentTime,
    last_open_at: previous?.last_open_at || null,
    last_disconnect_at: previous?.last_disconnect_at || null,
    condition: 'starting',
    failure_likelihood: 'unknown',
    signals: [],
    likely_failure_modes: [],
    recent_events: previous?.recent_events || [],
    safety_snapshot: null,
    active_incident: previous?.active_incident || null,
    last_incident: previous?.last_incident || null,
    last_failure: previous?.last_failure || null,
    ledger_integrity: ledger.integrity,
    ledger_sequence: ledgerRolledBack ? previous.ledger_sequence : ledger.sequence,
    ledger_head: ledgerRolledBack ? previous.ledger_head : ledger.head,
    last_health_signature: null,
  };

  function readWriterLease() {
    try {
      const raw = JSON.parse(fsModule.readFileSync(writerLeaseFile, 'utf8'));
      return {
        worker_revision: identifier(raw?.worker_revision),
        heartbeat_at: timestamp(raw?.heartbeat_at),
      };
    } catch {
      try {
        const stats = fsModule.statSync(writerLeaseFile);
        return { worker_revision: null, heartbeat_at: timestamp(stats.mtimeMs) };
      } catch {
        return null;
      }
    }
  }

  function writeWriterLease({ exclusive = false } = {}) {
    try {
      fsModule.mkdirSync(path.dirname(writerLeaseFile), { recursive: true });
      fsModule.writeFileSync(writerLeaseFile, `${JSON.stringify({
        schema_version: 1,
        worker_revision: revision,
        heartbeat_at: now(),
      })}\n`, {
        encoding: 'utf8', mode: 0o600, flag: exclusive ? 'wx' : 'w',
      });
      ownsWriterLease = true;
      writerLeaseConflict = false;
      return true;
    } catch {
      return false;
    }
  }

  function markWriterConflict() {
    try {
      fsModule.mkdirSync(path.dirname(writerConflictFile), { recursive: true });
      fsModule.writeFileSync(writerConflictFile, `${JSON.stringify({
        schema_version: 1,
        detected_at: now(),
      })}\n`, { encoding: 'utf8', mode: 0o600 });
    } catch {
      // A diagnostic marker must never affect the worker lifecycle.
    }
  }

  function hasFreshWriterConflict() {
    try {
      const raw = JSON.parse(fsModule.readFileSync(writerConflictFile, 'utf8'));
      const detectedAt = timestamp(raw?.detected_at);
      return raw?.schema_version === 1
        && detectedAt !== null
        && now() - detectedAt >= -60_000
        && now() - detectedAt <= LEASE_FRESH_MS;
    } catch {
      return false;
    }
  }

  function ensureWriterLease() {
    const lease = readWriterLease();
    if (ownsWriterLease) {
      if (lease?.worker_revision !== revision) {
        ownsWriterLease = false;
        writerLeaseConflict = true;
        return false;
      }
      return writeWriterLease();
    }
    if (!lease) return writeWriterLease({ exclusive: true });
    if (lease.worker_revision === revision) return writeWriterLease();
    if (lease.heartbeat_at && now() - lease.heartbeat_at <= LEASE_FRESH_MS) {
      writerLeaseConflict = true;
      markWriterConflict();
      return false;
    }
    try { fsModule.rmSync(writerLeaseFile, { force: true }); } catch { return false; }
    const acquired = writeWriterLease({ exclusive: true });
    writerLeaseConflict = !acquired;
    return acquired;
  }

  function releaseWriterLease() {
    if (!ownsWriterLease) return;
    const lease = readWriterLease();
    if (lease?.worker_revision === revision) {
      try { fsModule.rmSync(writerLeaseFile, { force: true }); } catch {}
    }
    ownsWriterLease = false;
  }

  function prune() {
    state.recent_events = sanitizedRecentEvents(state.recent_events, now());
  }

  function countRecent(types, windowMs) {
    const selected = new Set(Array.isArray(types) ? types : [types]);
    const floor = now() - windowMs;
    return state.recent_events.filter(event => selected.has(event.type) && event.at >= floor).length;
  }

  function addRecent(type, code = null) {
    if (!EVENT_TYPES.has(type)) return;
    state.recent_events.push({ type, at: now(), status_code: statusCode(code) });
    prune();
  }

  function recompute() {
    prune();
    const signals = new Set();
    const safety = state.safety_snapshot || {};
    const counts = safety.counts || {};
    const disconnects15m = countRecent(['disconnect_transient', 'disconnect_terminal'], 15 * 60_000);
    const reconnects15m = Math.max(
      countRecent('reconnect_scheduled', 15 * 60_000),
      boundedInteger(counts.reconnects_15m),
    );
    const deliveryFailures15m = Math.max(
      countRecent(['delivery_failure', 'delivery_timeout'], 15 * 60_000),
      boundedInteger(counts.delivery_failures_15m),
    );

    if (state.active_incident?.terminal) signals.add('terminal_session_failure');
    if (state.connection === 'closed' && state.active_incident) signals.add('connection_unstable');
    if (disconnects15m >= 1 || reconnects15m >= 3) signals.add('frequent_reconnects');
    if (deliveryFailures15m >= 2) signals.add('delivery_failures');
    if (countRecent('connection_timeout', 15 * 60_000)) signals.add('connection_timeout');
    if (countRecent('slow_connect', 15 * 60_000)) signals.add('slow_connect');
    if (countRecent('auth_unregistered', RECENT_RETENTION_MS)) signals.add('auth_unregistered');
    if (countRecent('auth_write_failure', RECENT_RETENTION_MS)) signals.add('auth_write_failure');
    if (countRecent('worker_lease_conflict', RECENT_RETENTION_MS)) signals.add('worker_lease_conflict');
    if (writerLeaseConflict || hasFreshWriterConflict()) signals.add('worker_lease_conflict');
    if (countRecent('startup_failure', 15 * 60_000)) signals.add('startup_failure');
    if (countRecent('provider_forbidden', RECENT_RETENTION_MS) || boundedInteger(counts.forbidden_60m)) {
      signals.add('provider_forbidden');
    }
    if (countRecent('provider_rate_limited', 60 * 60_000) || boundedInteger(counts.rate_limits_60m)) {
      signals.add('provider_rate_limited');
    }
    if (countRecent(['local_rate_limit', 'queue_full', 'queue_timeout'], 15 * 60_000)
        || boundedInteger(counts.local_rate_limits_15m)) {
      signals.add('local_rate_limit');
    }
    if (safety.backoff_until) signals.add('backoff_active');
    if (safety.circuit_open_until) signals.add('circuit_open');
    if (safety.operator_paused === true) signals.add('operator_paused');
    if (state.ledger_integrity !== 'verified') signals.add('ledger_integrity_invalid');

    state.signals = signalList([...signals]);
    state.likely_failure_modes = failureModesFor(state.signals);
    const critical = state.signals.some(value => [
      'terminal_session_failure', 'provider_forbidden', 'ledger_integrity_invalid',
      'auth_unregistered', 'worker_lease_conflict',
    ].includes(value));
    const degraded = critical || state.signals.some(value => [
      'circuit_open', 'provider_rate_limited', 'connection_unstable',
      'connection_timeout', 'auth_write_failure', 'startup_failure',
    ].includes(value)) || reconnects15m >= 5 || deliveryFailures15m >= 5;
    const warning = degraded || state.signals.length > 0;
    if (critical) state.condition = 'critical';
    else if (degraded) state.condition = 'degraded';
    else if (warning) state.condition = 'warning';
    else if (state.connection === 'open') state.condition = 'healthy';
    else if (state.connection === 'starting' || state.connection === 'connecting') state.condition = 'starting';
    else state.condition = 'unknown';

    if (critical || degraded) state.failure_likelihood = 'high';
    else if (warning) state.failure_likelihood = 'elevated';
    else if (state.condition === 'healthy') state.failure_likelihood = 'low';
    else state.failure_likelihood = 'unknown';
  }

  function persist() {
    recompute();
    state.updated_at = now();
    if (!ensureWriterLease()) return false;
    atomicWrite(stateFile, state, fsModule, logger);
    return true;
  }

  function appendLedger({
    event,
    lifecycle,
    incidentId = null,
    diagnostic = {},
    reconnectAttempt = 0,
    reconnectDelayMs = 0,
  }) {
    if (!ensureWriterLease()) return false;
    const currentLedger = auditLedger(ledgerFile, fsModule);
    const rolledBack = currentLedger.integrity === 'verified' && (
      currentLedger.sequence < state.ledger_sequence
      || (
        currentLedger.sequence === state.ledger_sequence
        && currentLedger.head !== state.ledger_head
      )
    );
    if (rolledBack) {
      state.ledger_integrity = 'invalid';
      return false;
    }
    state.ledger_integrity = currentLedger.integrity;
    state.ledger_sequence = currentLedger.sequence;
    state.ledger_head = currentLedger.head;
    if (state.ledger_integrity !== 'verified') return false;
    const normalized = normalizedDiagnostic(diagnostic);
    const base = {
      schema_version: LEDGER_SCHEMA,
      sequence: state.ledger_sequence + 1,
      record_id: identifier(idFactory()) || `record_${state.ledger_sequence + 1}`,
      at: now(),
      event: enumValue(event, LEDGER_EVENTS, 'health_transition'),
      lifecycle: enumValue(lifecycle, LIFECYCLES, 'observation'),
      incident_id: identifier(incidentId),
      condition: enumValue(state.condition, CONDITIONS, 'unknown'),
      status_code: normalized.statusCode,
      status_name: normalized.statusName,
      category: normalized.category,
      action: normalized.action,
      terminal: normalized.terminal,
      reauth_required: normalized.reauthRequired,
      reconnect_attempt: boundedInteger(reconnectAttempt, 0, 10_000),
      reconnect_delay_ms: boundedInteger(reconnectDelayMs, 0, 24 * 60 * 60_000),
      signals: signalList(state.signals),
      previous_hash: state.ledger_head,
    };
    const record = { ...base, record_hash: hashRecord(base) };
    try {
      fsModule.mkdirSync(path.dirname(ledgerFile), { recursive: true });
      const descriptor = fsModule.openSync(ledgerFile, 'a', 0o600);
      try {
        fsModule.writeSync(descriptor, `${JSON.stringify(record)}\n`, null, 'utf8');
        if (typeof fsModule.fsyncSync === 'function') fsModule.fsyncSync(descriptor);
      } finally {
        fsModule.closeSync(descriptor);
      }
      state.ledger_sequence = record.sequence;
      state.ledger_head = record.record_hash;
      return true;
    } catch {
      state.ledger_integrity = 'unavailable';
      logger.warn?.('[WA INCIDENT] Forensic ledger could not be appended');
      return false;
    }
  }

  function diagnosticFailure(diagnostic, incidentId = null) {
    const normalized = normalizedDiagnostic(diagnostic);
    return {
      failure_id: identifier(idFactory()) || `failure_${now()}`,
      incident_id: identifier(incidentId),
      at: now(),
      status_code: normalized.statusCode,
      status_name: normalized.statusName,
      category: normalized.category,
      action: normalized.action,
      terminal: normalized.terminal,
      reauth_required: normalized.reauthRequired,
    };
  }

  function recordWorkerStarted() {
    state.connection = 'starting';
    state.connection_since = now();
    recompute();
    appendLedger({ event: 'worker_started', lifecycle: 'observation' });
    persist();
  }

  function recordConnecting({ registered = true } = {}) {
    state.connection = 'connecting';
    state.connection_since = now();
    if (!registered) recordOperationalSignal({ type: 'auth_unregistered' });
    else persist();
  }

  function recordConnectionOpen() {
    const incident = state.active_incident;
    state.connection = 'open';
    state.connection_since = now();
    state.last_open_at = now();
    if (incident) {
      incident.resolved_at = now();
      incident.last_event_at = now();
      state.last_incident = clone(incident);
      state.active_incident = null;
    }
    recompute();
    appendLedger({
      event: 'connection_open',
      lifecycle: incident ? 'resolved' : 'observation',
      incidentId: incident?.incident_id,
      diagnostic: incident || {},
    });
    persist();
  }

  function recordConnectionClosed(rawDiagnostic = {}) {
    const diagnostic = normalizedDiagnostic(rawDiagnostic);
    const current = now();
    const eventType = diagnostic.terminal ? 'disconnect_terminal' : 'disconnect_transient';
    const signature = `${diagnostic.statusCode ?? 'none'}:${diagnostic.statusName}`;
    const duplicate = Boolean(
      state.active_incident
      && state.active_incident.last_signature === signature
      && current - state.active_incident.last_event_at <= DUPLICATE_DISCONNECT_MS
    );
    state.connection = 'closed';
    state.connection_since = current;
    state.last_disconnect_at = current;
    if (!duplicate) addRecent(eventType, diagnostic.statusCode);

    let incident = state.active_incident;
    const lifecycle = incident ? 'updated' : 'opened';
    if (!incident) {
      incident = {
        incident_id: identifier(idFactory()) || `incident_${current}`,
        started_at: current,
        last_event_at: current,
        resolved_at: null,
        status_code: diagnostic.statusCode,
        status_name: diagnostic.statusName,
        category: diagnostic.category,
        action: diagnostic.action,
        terminal: diagnostic.terminal,
        reauth_required: diagnostic.reauthRequired,
        disconnect_count: 1,
        reconnect_attempts: 0,
        precursors: signalList(state.signals),
        last_signature: signature,
      };
      state.active_incident = incident;
    } else if (!duplicate) {
      incident.last_event_at = current;
      incident.disconnect_count += 1;
      if (diagnostic.statusName !== 'unknown' || incident.status_name === 'unknown') {
        incident.status_code = diagnostic.statusCode;
        incident.status_name = diagnostic.statusName;
        incident.category = diagnostic.category;
        incident.action = diagnostic.action;
      }
      incident.terminal ||= diagnostic.terminal;
      incident.reauth_required ||= diagnostic.reauthRequired;
      incident.precursors = signalList([...incident.precursors, ...state.signals]);
      incident.last_signature = signature;
    }
    state.last_failure = diagnosticFailure(diagnostic, incident.incident_id);
    recompute();
    if (!duplicate) {
      appendLedger({
        event: 'connection_closed', lifecycle, incidentId: incident.incident_id, diagnostic,
        reconnectAttempt: incident.reconnect_attempts,
      });
    }
    persist();
  }

  function recordReconnectScheduled({ attempt = 0, delayMs = 0, statusCode: rawStatus = null } = {}) {
    addRecent('reconnect_scheduled', rawStatus);
    if (state.active_incident) {
      state.active_incident.reconnect_attempts = Math.max(
        state.active_incident.reconnect_attempts,
        boundedInteger(attempt, 0, 10_000),
      );
      state.active_incident.last_event_at = now();
    }
    recompute();
    appendLedger({
      event: 'reconnect_scheduled', lifecycle: 'updated',
      incidentId: state.active_incident?.incident_id,
      diagnostic: state.active_incident || {},
      reconnectAttempt: attempt, reconnectDelayMs: delayMs,
    });
    persist();
  }

  function recordOperationalSignal(raw = {}) {
    const type = EVENT_TYPES.has(raw.type) ? raw.type : null;
    if (!type) return;
    const defaults = OPERATIONAL_DIAGNOSTICS[type] || OPERATIONAL_DIAGNOSTICS.delivery_failure;
    const diagnostic = normalizedDiagnostic({
      ...defaults,
      statusCode: statusCode(raw.statusCode) ?? defaults.statusCode,
    });
    addRecent(type, diagnostic.statusCode);
    state.last_failure = diagnosticFailure(diagnostic, state.active_incident?.incident_id);
    recompute();
    appendLedger({
      event: 'operational_signal', lifecycle: 'signal',
      incidentId: state.active_incident?.incident_id, diagnostic,
      reconnectAttempt: raw.attempt,
    });
    persist();
  }

  function heartbeat({ safety = {}, connection = null } = {}) {
    state.heartbeat_at = now();
    if (CONNECTIONS.has(connection)) state.connection = connection;
    state.safety_snapshot = {
      counts: {
        delivery_failures_15m: boundedInteger(safety?.counts?.delivery_failures_15m),
        reconnects_15m: boundedInteger(safety?.counts?.reconnects_15m),
        rate_limits_60m: boundedInteger(safety?.counts?.rate_limits_60m),
        forbidden_60m: boundedInteger(safety?.counts?.forbidden_60m),
        local_rate_limits_15m: boundedInteger(safety?.counts?.local_rate_limits_15m),
      },
      operator_paused: safety?.operator_paused === true,
      backoff_until: timestamp(safety?.backoff_until),
      circuit_open_until: timestamp(safety?.circuit_open_until),
    };
    recompute();
    const signature = `${state.condition}|${state.signals.join(',')}`;
    if (state.last_health_signature !== null && state.last_health_signature !== signature) {
      appendLedger({ event: 'health_transition', lifecycle: 'observation' });
    }
    state.last_health_signature = signature;
    persist();
  }

  function recordWorkerShutdown(reason = 'unknown') {
    const safeReason = enumValue(reason, SHUTDOWN_REASONS, 'unknown');
    state.connection = 'closed';
    state.connection_since = now();
    recompute();
    appendLedger({
      event: 'worker_shutdown', lifecycle: 'observation',
      diagnostic: {
        statusName: 'worker_shutdown', category: 'process', action: 'none',
        terminal: false, reauthRequired: false,
      },
    });
    state.shutdown_reason = safeReason;
    persist();
    releaseWriterLease();
  }

  function snapshot() {
    persist();
    return clone(state);
  }

  const initiallyAcquiredLease = ensureWriterLease();
  recordWorkerStarted();
  if (!initiallyAcquiredLease || writerLeaseConflict) {
    logger.warn?.('[WA INCIDENT] Another live worker owns the forensic writer lease');
    recordOperationalSignal({ type: 'worker_lease_conflict' });
  }

  return {
    recordConnecting,
    recordConnectionOpen,
    recordConnectionClosed,
    recordReconnectScheduled,
    recordOperationalSignal,
    recordWorkerShutdown,
    heartbeat,
    snapshot,
  };
}

export const __test = {
  auditLedger,
  hashRecord,
  ledgerBase,
  normalizedDiagnostic,
};
