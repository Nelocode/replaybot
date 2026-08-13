import fs from 'fs';
import path from 'path';

const EVENT_TYPES = new Set([
  'outgoing',
  'delivery_failure',
  'rate_limited',
  'forbidden',
  'local_rate_limit',
  'reconnect',
]);

const RETENTION_MS = 60 * 60 * 1000;
const MAX_EVENTS_PER_TYPE = 500;

function finiteTimestamp(value) {
  return Number.isFinite(value) && value > 0 ? Math.trunc(value) : null;
}

function initialState() {
  return {
    schema_version: 1,
    updated_at: null,
    operator_paused: false,
    pause_updated_at: null,
    backoff_until: null,
    circuit_open_until: null,
    events: [],
  };
}

function boundedEvents(events, now) {
  const floor = now - RETENTION_MS;
  const sanitized = events.filter(event => (
    event && EVENT_TYPES.has(event.type)
    && finiteTimestamp(event.at) !== null
    && event.at >= floor
    && event.at <= now + 60_000
  ));
  return [...EVENT_TYPES]
    .flatMap(type => sanitized.filter(event => event.type === type).slice(-MAX_EVENTS_PER_TYPE))
    .map(event => ({ type: event.type, at: Math.trunc(event.at) }))
    .sort((left, right) => left.at - right.at);
}

function loadControl(filePath, fsModule) {
  let raw;
  try {
    raw = JSON.parse(fsModule.readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
  if (!raw || raw.schema_version !== 1) return null;
  return {
    operator_paused: raw.operator_paused === true,
    pause_updated_at: finiteTimestamp(raw.pause_updated_at),
  };
}

function writeControl(filePath, state, fsModule) {
  const temporary = `${filePath}.${process.pid}.tmp`;
  fsModule.mkdirSync(path.dirname(filePath), { recursive: true });
  fsModule.writeFileSync(temporary, `${JSON.stringify({
    schema_version: 1,
    operator_paused: state.operator_paused,
    pause_updated_at: state.pause_updated_at,
  }, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  try {
    fsModule.renameSync(temporary, filePath);
  } catch {
    fsModule.rmSync(filePath, { force: true });
    fsModule.renameSync(temporary, filePath);
  }
}

function loadState(filePath, fsModule, now) {
  let raw;
  try {
    raw = JSON.parse(fsModule.readFileSync(filePath, 'utf8'));
  } catch {
    return initialState();
  }
  if (!raw || raw.schema_version !== 1 || !Array.isArray(raw.events)) {
    return initialState();
  }
  return {
    schema_version: 1,
    updated_at: finiteTimestamp(raw.updated_at),
    operator_paused: raw.operator_paused === true,
    pause_updated_at: finiteTimestamp(raw.pause_updated_at),
    backoff_until: finiteTimestamp(raw.backoff_until),
    circuit_open_until: finiteTimestamp(raw.circuit_open_until),
    events: boundedEvents(raw.events, now),
  };
}

function countSince(events, type, since) {
  return events.reduce((total, event) => (
    event.type === type && event.at >= since ? total + 1 : total
  ), 0);
}

function evaluate(state, now) {
  const counts = {
    outgoing_1m: countSince(state.events, 'outgoing', now - 60_000),
    delivery_failures_15m: countSince(state.events, 'delivery_failure', now - 15 * 60_000),
    reconnects_15m: countSince(state.events, 'reconnect', now - 15 * 60_000),
    rate_limits_60m: countSince(state.events, 'rate_limited', now - 60 * 60_000),
    forbidden_60m: countSince(state.events, 'forbidden', now - 60 * 60_000),
    local_rate_limits_15m: countSince(state.events, 'local_rate_limit', now - 15 * 60_000),
  };
  const backoffActive = Boolean(state.backoff_until && state.backoff_until > now);
  const circuitOpen = Boolean(state.circuit_open_until && state.circuit_open_until > now);
  const reasons = [];

  if (counts.forbidden_60m > 0) reasons.push('forbidden_response');
  if (counts.rate_limits_60m > 0) reasons.push('rate_limited_response');
  if (counts.delivery_failures_15m >= 5) reasons.push('delivery_failures');
  if (counts.reconnects_15m >= 5) reasons.push('frequent_reconnects');
  if (circuitOpen) reasons.push('circuit_open');

  let level = reasons.length ? 'high' : 'low';
  if (level === 'low') {
    if (counts.delivery_failures_15m >= 2) reasons.push('delivery_failures');
    if (counts.reconnects_15m >= 3) reasons.push('frequent_reconnects');
    if (counts.outgoing_1m >= 15) reasons.push('elevated_activity');
    if (counts.local_rate_limits_15m > 0) reasons.push('local_rate_limit');
    if (backoffActive) reasons.push('backoff_active');
    if (state.operator_paused) reasons.push('operator_paused');
    if (reasons.length) level = 'moderate';
  }

  return {
    level,
    reasons,
    counts,
    operator_paused: state.operator_paused,
    backoff_until: backoffActive ? state.backoff_until : null,
    circuit_open_until: circuitOpen ? state.circuit_open_until : null,
    delivery_blocked: state.operator_paused || backoffActive || circuitOpen,
  };
}

/**
 * Stores only low-cardinality operational events. It deliberately excludes
 * JIDs, phone numbers, message contents, raw errors and Baileys payloads.
 */
export function createWhatsAppSafetyHealth({
  filePath,
  controlFilePath = path.join(path.dirname(filePath || '.'), 'wa_safety_control.json'),
  logger = console,
  fsModule = fs,
  now = () => Date.now(),
} = {}) {
  if (!filePath) throw new TypeError('filePath is required');
  if (typeof now !== 'function') throw new TypeError('now must be a function');

  let state = loadState(filePath, fsModule, now());
  const existingControl = loadControl(controlFilePath, fsModule);
  if (existingControl) {
    state.operator_paused = existingControl.operator_paused;
    state.pause_updated_at = existingControl.pause_updated_at;
  } else {
    state.pause_updated_at ||= now();
    try {
      writeControl(controlFilePath, state, fsModule);
    } catch {
      logger.warn?.('[WA SAFETY] Pause control could not be initialized');
    }
  }

  function syncExternalPause() {
    const control = loadControl(controlFilePath, fsModule);
    if (!control) return;
    state.operator_paused = control.operator_paused;
    state.pause_updated_at = control.pause_updated_at;
  }

  function prune(currentTime) {
    state.events = boundedEvents(state.events, currentTime);
    if (state.backoff_until && state.backoff_until <= currentTime) state.backoff_until = null;
    if (state.circuit_open_until && state.circuit_open_until <= currentTime) {
      state.circuit_open_until = null;
    }
  }

  function persist() {
    const currentTime = now();
    syncExternalPause();
    prune(currentTime);
    state.updated_at = currentTime;
    const directory = path.dirname(filePath);
    const temporary = `${filePath}.${process.pid}.tmp`;
    try {
      fsModule.mkdirSync(directory, { recursive: true });
      fsModule.writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, {
        encoding: 'utf8',
        mode: 0o600,
      });
      try {
        fsModule.renameSync(temporary, filePath);
      } catch {
        fsModule.rmSync(filePath, { force: true });
        fsModule.renameSync(temporary, filePath);
      }
    } catch {
      try {
        fsModule.rmSync(temporary, { force: true });
      } catch {
        // Diagnostics and safety state must never crash the worker.
      }
      logger.warn?.('[WA SAFETY] State could not be persisted');
    }
  }

  function record(type) {
    if (!EVENT_TYPES.has(type)) return;
    syncExternalPause();
    state.events.push({ type, at: now() });
    persist();
  }

  function setBackoffUntil(value) {
    const timestamp = finiteTimestamp(value);
    state.backoff_until = timestamp && timestamp > now() ? timestamp : null;
    persist();
  }

  function setCircuitOpenUntil(value) {
    const timestamp = finiteTimestamp(value);
    state.circuit_open_until = timestamp && timestamp > now() ? timestamp : null;
    persist();
  }

  function setOperatorPaused(value) {
    state.operator_paused = value === true;
    state.pause_updated_at = now();
    try {
      writeControl(controlFilePath, state, fsModule);
    } catch {
      logger.warn?.('[WA SAFETY] Pause control could not be persisted');
    }
    persist();
  }

  function refresh() {
    syncExternalPause();
    prune(now());
    return evaluate(state, now());
  }

  function snapshot() {
    const health = refresh();
    return JSON.parse(JSON.stringify({
      schema_version: 1,
      updated_at: state.updated_at,
      ...health,
    }));
  }

  function recentEventTimes(type, windowMs) {
    const floor = now() - windowMs;
    return state.events
      .filter(event => event.type === type && event.at >= floor)
      .map(event => event.at);
  }

  function touch() {
    persist();
  }

  persist();
  return {
    record,
    refresh,
    snapshot,
    recentEventTimes,
    setBackoffUntil,
    setCircuitOpenUntil,
    setOperatorPaused,
    touch,
  };
}

export const __test = { evaluate, loadState };
