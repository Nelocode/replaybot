import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { diagnoseWhatsAppDisconnect } from '../wa_disconnect_policy.mjs';
import {
  createWhatsAppIncidentMonitor,
  __test as incidentMonitorTest,
} from '../wa_incident_monitor.mjs';

function harness(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-incident-monitor-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const stateFile = path.join(directory, 'health.json');
  const ledgerFile = path.join(directory, 'incidents.jsonl');
  let currentTime = 1_700_000_000_000;
  let nextIdentifier = 0;
  return {
    stateFile,
    ledgerFile,
    advance(milliseconds) { currentTime += milliseconds; },
    create(workerRevision, overrides = {}) {
      return createWhatsAppIncidentMonitor({
        stateFile,
        ledgerFile,
        workerRevision,
        now: () => currentTime,
        idFactory: () => `test_${++nextIdentifier}`,
        logger: { warn() {} },
        ...overrides,
      });
    },
  };
}

function ledgerRecords(filePath) {
  return fs.readFileSync(filePath, 'utf8')
    .split(/\r?\n/)
    .filter(Boolean)
    .map(line => JSON.parse(line));
}

test('terminal evidence survives restart and a confirmed opening resolves it', (t) => {
  const h = harness(t);
  const first = h.create('worker_a');
  first.recordConnectionClosed(diagnoseWhatsAppDisconnect(440));
  const terminal = first.snapshot();

  assert.equal(terminal.condition, 'critical');
  assert.equal(terminal.failure_likelihood, 'high');
  assert.ok(terminal.signals.includes('terminal_session_failure'));
  assert.deepEqual(
    {
      code: terminal.active_incident.status_code,
      name: terminal.active_incident.status_name,
      category: terminal.active_incident.category,
      terminal: terminal.active_incident.terminal,
      reauth: terminal.active_incident.reauth_required,
    },
    {
      code: 440,
      name: 'connection_replaced',
      category: 'session_conflict',
      terminal: true,
      reauth: true,
    },
  );

  h.advance(91_000);
  const restarted = h.create('worker_b');
  const persisted = restarted.snapshot();
  assert.equal(persisted.active_incident.incident_id, terminal.active_incident.incident_id);
  assert.equal(persisted.last_disconnect_at, terminal.last_disconnect_at);
  assert.equal(persisted.last_failure.status_name, 'connection_replaced');
  assert.equal(persisted.condition, 'critical');

  h.advance(2_000);
  restarted.recordConnectionOpen();
  const recovered = restarted.snapshot();
  const resolution = ledgerRecords(h.ledgerFile).at(-1);
  assert.equal(recovered.connection, 'open');
  assert.equal(recovered.active_incident, null);
  assert.equal(recovered.last_incident.incident_id, terminal.active_incident.incident_id);
  assert.ok(recovered.last_incident.resolved_at);
  assert.ok(!recovered.signals.includes('terminal_session_failure'));
  assert.notEqual(recovered.condition, 'critical');
  assert.equal(resolution.event, 'connection_open');
  assert.equal(resolution.lifecycle, 'resolved');
});

test('ledger hashes verify and any mutation fails closed without extending it', (t) => {
  const h = harness(t);
  const monitor = h.create('worker_ledger');
  monitor.recordConnectionOpen();
  h.advance(1_000);
  monitor.recordConnectionClosed(diagnoseWhatsAppDisconnect(503));

  const records = ledgerRecords(h.ledgerFile);
  let previousHash = null;
  for (const record of records) {
    assert.equal(record.previous_hash, previousHash);
    assert.match(record.record_hash, /^[a-f0-9]{64}$/);
    assert.equal(
      record.record_hash,
      incidentMonitorTest.hashRecord(incidentMonitorTest.ledgerBase(record)),
    );
    previousHash = record.record_hash;
  }
  assert.equal(incidentMonitorTest.auditLedger(h.ledgerFile, fs).integrity, 'verified');

  records[0].action = records[0].action === 'none' ? 'inspect_worker' : 'none';
  fs.writeFileSync(h.ledgerFile, `${records.map(JSON.stringify).join('\n')}\n`);
  const tampered = fs.readFileSync(h.ledgerFile, 'utf8');
  h.advance(91_000);
  const restarted = h.create('worker_tampered');
  const snapshot = restarted.snapshot();
  assert.equal(snapshot.ledger_integrity, 'invalid');
  assert.equal(snapshot.condition, 'critical');
  assert.ok(snapshot.signals.includes('ledger_integrity_invalid'));
  assert.equal(fs.readFileSync(h.ledgerFile, 'utf8'), tampered);
});

test('truncating only the ledger tail is detected as rollback and cannot extend the chain', (t) => {
  const h = harness(t);
  const monitor = h.create('worker_before_truncation');
  monitor.recordConnectionOpen();
  h.advance(1_000);
  monitor.recordConnectionClosed(diagnoseWhatsAppDisconnect(503));
  const before = monitor.snapshot();
  const records = ledgerRecords(h.ledgerFile);
  assert.ok(records.length >= 3);
  assert.equal(before.ledger_sequence, records.length);

  fs.writeFileSync(h.ledgerFile, `${records.slice(0, -1).map(JSON.stringify).join('\n')}\n`);
  const truncated = fs.readFileSync(h.ledgerFile, 'utf8');
  h.advance(91_000);
  const restarted = h.create('worker_after_truncation');
  const snapshot = restarted.snapshot();

  assert.equal(snapshot.ledger_integrity, 'invalid');
  assert.equal(snapshot.condition, 'critical');
  assert.ok(snapshot.signals.includes('ledger_integrity_invalid'));
  assert.equal(fs.readFileSync(h.ledgerFile, 'utf8'), truncated);
});

test('duplicate closes inside the dedupe window produce one incident record', (t) => {
  const h = harness(t);
  const monitor = h.create('worker_dedupe');
  const diagnostic = diagnoseWhatsAppDisconnect(428);
  monitor.recordConnectionClosed(diagnostic);
  const first = monitor.snapshot();
  h.advance(1_000);
  monitor.recordConnectionClosed(diagnostic);
  const duplicate = monitor.snapshot();

  assert.equal(duplicate.active_incident.incident_id, first.active_incident.incident_id);
  assert.equal(duplicate.active_incident.disconnect_count, 1);
  assert.equal(duplicate.ledger_sequence, first.ledger_sequence);
  assert.equal(
    ledgerRecords(h.ledgerFile).filter(record => record.event === 'connection_closed').length,
    1,
  );
});

test('state and ledger never serialize injected private fields', (t) => {
  const h = harness(t);
  const monitor = h.create('worker_private');
  const privateFields = {
    phone: '+573001234567',
    jid: '573001234567@s.whatsapp.net',
    message: 'PRIVATE_MESSAGE_SENTINEL',
    stack: 'PRIVATE_STACK_SENTINEL',
    qr: 'PRIVATE_QR_SENTINEL',
    token: 'PRIVATE_TOKEN_SENTINEL',
  };
  monitor.recordConnectionClosed({
    ...diagnoseWhatsAppDisconnect(500),
    ...privateFields,
  });
  monitor.recordOperationalSignal({ type: 'startup_failure', ...privateFields });
  monitor.heartbeat({
    connection: 'closed',
    safety: { counts: { delivery_failures_15m: 2 }, ...privateFields },
  });

  const serialized = `${fs.readFileSync(h.stateFile, 'utf8')}\n${fs.readFileSync(h.ledgerFile, 'utf8')}`;
  for (const secret of Object.values(privateFields)) assert.ok(!serialized.includes(secret));
  assert.doesNotMatch(serialized, /"(?:phone|jid|message|stack|qr|token)"\s*:/i);
});

test('a fresh writer lease blocks a second monitor and explicit shutdown hands it over', (t) => {
  const h = harness(t);
  const first = h.create('worker_first');
  const beforeSecond = fs.readFileSync(h.ledgerFile, 'utf8');
  const second = h.create('worker_second');

  second.recordConnectionClosed(diagnoseWhatsAppDisconnect(428));
  second.heartbeat({ connection: 'closed' });
  assert.equal(fs.readFileSync(h.ledgerFile, 'utf8'), beforeSecond);
  assert.equal(incidentMonitorTest.auditLedger(h.ledgerFile, fs).integrity, 'verified');
  assert.ok(second.snapshot().signals.includes('worker_lease_conflict'));
  assert.equal(fs.readFileSync(h.ledgerFile, 'utf8'), beforeSecond);

  first.recordWorkerShutdown('SIGTERM');
  const sequenceAfterShutdown = incidentMonitorTest.auditLedger(h.ledgerFile, fs).sequence;
  second.recordConnectionOpen();

  const audit = incidentMonitorTest.auditLedger(h.ledgerFile, fs);
  const records = ledgerRecords(h.ledgerFile);
  assert.equal(audit.integrity, 'verified');
  assert.equal(audit.sequence, sequenceAfterShutdown + 1);
  assert.equal(records.at(-1).sequence, sequenceAfterShutdown + 1);
  assert.equal(records.at(-1).previous_hash, records.at(-2).record_hash);
  assert.equal(records.at(-1).event, 'connection_open');
});

test('ledger appends call fsync on the real filesystem descriptor', (t) => {
  const h = harness(t);
  const openPaths = new Map();
  const syncedPaths = [];
  const observedFs = new Proxy(fs, {
    get(target, property) {
      if (property === 'openSync') {
        return (...args) => {
          const descriptor = target.openSync(...args);
          openPaths.set(descriptor, String(args[0]));
          return descriptor;
        };
      }
      if (property === 'fsyncSync') {
        return descriptor => {
          syncedPaths.push(openPaths.get(descriptor) || null);
          return target.fsyncSync(descriptor);
        };
      }
      if (property === 'closeSync') {
        return descriptor => {
          try { return target.closeSync(descriptor); } finally { openPaths.delete(descriptor); }
        };
      }
      const value = target[property];
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  const monitor = h.create('worker_fsync', { fsModule: observedFs });
  monitor.recordConnectionClosed(diagnoseWhatsAppDisconnect(503));

  assert.ok(syncedPaths.filter(filePath => filePath === h.ledgerFile).length >= 2);
  assert.equal(incidentMonitorTest.auditLedger(h.ledgerFile, fs).integrity, 'verified');
});
