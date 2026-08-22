import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  createWhatsAppDeliverySafety,
  readWhatsAppSafetyConfig,
  recordWhatsAppProviderSignal,
  WhatsAppDeliveryBlockedError,
} from '../wa_delivery_safety.mjs';
import { createWhatsAppSafetyHealth } from '../wa_safety_health.mjs';

function createHarness(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-delivery-safety-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const healthFile = path.join(directory, 'health.json');
  let currentTime = 1_700_000_000_000;
  const delays = [];
  const health = createWhatsAppSafetyHealth({
    filePath: healthFile,
    controlFilePath: path.join(directory, 'control.json'),
    now: () => currentTime,
    logger: { warn() {} },
  });
  const config = {
    ...readWhatsAppSafetyConfig({}),
    readDelayMinMs: 0,
    readDelayMaxMs: 0,
    textDelayMinMs: 0,
    textDelayMaxMs: 0,
    textMsPerCharacter: 0,
    audioDelayMinMs: 0,
    audioDelayMaxMs: 0,
    jitterRatio: 0,
    minimumSendIntervalMs: 0,
    auxiliaryTimeoutMs: 50,
    sendTimeoutMs: 100,
    ...overrides,
  };
  const sleep = async (ms, signal) => {
    if (signal?.aborted) throw signal.reason;
    delays.push(ms);
    currentTime += ms;
  };
  return {
    health,
    healthFile,
    config,
    delays,
    sleep,
    now: () => currentTime,
    advance(ms) { currentTime += ms; },
    logger: { warn() {} },
  };
}

test('configuration is bounded and min/max pairs stay coherent', () => {
  const config = readWhatsAppSafetyConfig({
    WA_TEXT_DELAY_MIN_MS: '8000',
    WA_TEXT_DELAY_MAX_MS: '10',
    WA_MAX_PENDING_SENDS: '99999',
  });
  assert.equal(config.textDelayMaxMs, 8_000);
  assert.equal(config.maxPendingSends, 500);
});

test('text and voice delivery publish bounded composing and recording UX', async (t) => {
  const harness = createHarness(t, {
    textDelayMinMs: 100,
    textDelayMaxMs: 500,
    textMsPerCharacter: 10,
    audioDelayMinMs: 300,
    audioDelayMaxMs: 300,
  });
  const effects = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    random: () => 0.5,
    sendPresenceUpdate: async (presence) => effects.push(presence),
    sendMessage: async (_jid, content) => effects.push(content.text ? 'text' : 'audio'),
  });
  await safety.send('contact-a', { text: 'hola' });
  await safety.send('contact-a', { audio: Buffer.alloc(4_800), ptt: true });
  assert.deepEqual(effects, ['composing', 'text', 'paused', 'recording', 'audio', 'paused']);
  assert.equal(harness.delays.reduce((total, value) => total + value, 0), 440);
});

test('bounded pacing remains when presence publication is disabled', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    textDelayMinMs: 300,
    textDelayMaxMs: 300,
  });
  const safety = createWhatsAppDeliverySafety({ ...harness, sendMessage: async () => {} });
  await safety.send('contact-a', { text: 'hola' });
  assert.equal(harness.delays.reduce((total, value) => total + value, 0), 300);
});

test('403 pauses future delivery and persists no private payload', async (t) => {
  const harness = createHarness(t);
  const restricted = new Error('private provider details');
  restricted.output = { statusCode: 403 };
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async () => { throw restricted; },
  });
  await assert.rejects(safety.send('573001234567@s.whatsapp.net', { text: 'secret text' }));
  assert.equal(harness.health.snapshot().operator_paused, true);
  await assert.rejects(
    safety.send('contact-b', { text: 'next' }),
    error => error instanceof WhatsAppDeliveryBlockedError && error.code === 'operator_paused',
  );
  const persisted = fs.readFileSync(harness.healthFile, 'utf8');
  for (const value of ['573001234567', 'secret text', 'private provider details']) {
    assert.equal(persisted.includes(value), false);
  }
});

test('429 in read receipt activates backoff before content is sent', async (t) => {
  const harness = createHarness(t);
  const limited = new Error('limited');
  limited.output = { statusCode: 429 };
  let sent = false;
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    readMessages: async () => { throw limited; },
    sendMessage: async () => { sent = true; },
  });
  assert.equal((await safety.markRead({ id: 'one' })).status, 'skipped');
  await assert.rejects(safety.send('contact-a', { text: 'hola' }));
  assert.equal(sent, false);
  assert.ok(harness.health.snapshot().backoff_until);
});

test('connection 403 and 429 use the common provider signal mapping', (t) => {
  const forbidden = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 403, health: forbidden.health, config: forbidden.config, now: forbidden.now,
  }), 'forbidden');
  assert.equal(forbidden.health.snapshot().operator_paused, true);

  const limited = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 429, health: limited.health, config: limited.config, now: limited.now,
  }), 'rate_limited');
  assert.ok(limited.health.snapshot().backoff_until);
});

test('global send reservation is atomic and pending queue is bounded', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    maxSendsPerMinute: 1,
    maxPendingSends: 2,
  });
  const effects = [];
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async jid => effects.push(jid),
  });
  const results = await Promise.allSettled([
    safety.send('contact-a', { text: 'one' }),
    safety.send('contact-b', { text: 'two' }),
    safety.send('contact-c', { text: 'three' }),
  ]);
  assert.deepEqual(effects, ['contact-a']);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].reason.code, 'local_rate_limit');
  assert.equal(results[2].reason.code, 'queue_full');
});

test('a timed-out transport keeps the global gate until its final outcome is known', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    sendTimeoutMs: 15,
    circuitOpenMs: 20,
  });
  const effects = [];
  let releaseTransport;
  let markStarted;
  const transportGate = new Promise(resolve => { releaseTransport = resolve; });
  const started = new Promise(resolve => { markStarted = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async jid => {
      effects.push(`start:${jid}`);
      markStarted();
      await transportGate;
      effects.push(`finish:${jid}`);
    },
  });

  const first = safety.send('contact-a', { text: 'one' });
  await started;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.ok(harness.health.snapshot().circuit_open_until);

  harness.advance(21);
  let secondSettled = false;
  const second = safety.send('contact-b', { text: 'two' }).then(() => {
    secondSettled = true;
  });
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(secondSettled, false);
  assert.deepEqual(effects, ['start:contact-a']);

  releaseTransport();
  await assert.rejects(first, /send_timeout/);
  await second;
  assert.deepEqual(effects, [
    'start:contact-a',
    'finish:contact-a',
    'start:contact-b',
    'finish:contact-b',
  ]);
});

test('operator pause cannot consume the later socket-close release of an unknown transport', async (t) => {
  const harness = createHarness(t, {
    presenceEnabled: false,
    sendTimeoutMs: 15,
  });
  let markStarted;
  const started = new Promise(resolve => { markStarted = resolve; });
  const safety = createWhatsAppDeliverySafety({
    ...harness,
    sendMessage: async () => {
      markStarted();
      await new Promise(() => {});
    },
  });
  let settled = false;
  const outcome = safety.send('contact-a', { text: 'one' }).then(
    value => ({ value }),
    error => ({ error }),
  ).finally(() => { settled = true; });

  await started;
  await new Promise(resolve => setTimeout(resolve, 30));
  harness.health.setOperatorPaused(true);
  safety.refreshControls();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(settled, false);

  safety.cancelAll('connection_closed');
  const result = await outcome;
  assert.match(result.error.message, /send_timeout/);
});

test('diagnostic callbacks emit only allowlisted private signals and never affect delivery', async (t) => {
  const diagnostics = [];
  const onDiagnostic = diagnostic => diagnostics.push(diagnostic);

  const forbiddenHarness = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 403,
    health: forbiddenHarness.health,
    config: forbiddenHarness.config,
    now: forbiddenHarness.now,
    attempt: 3,
    onDiagnostic,
  }), 'forbidden');

  const limitedHarness = createHarness(t);
  assert.equal(recordWhatsAppProviderSignal({
    statusCode: 429,
    health: limitedHarness.health,
    config: limitedHarness.config,
    now: limitedHarness.now,
    attempt: 2,
    onDiagnostic,
  }), 'rate_limited');

  const failureHarness = createHarness(t, { presenceEnabled: false });
  const privateFailure = new Error('private provider response');
  privateFailure.payload = { jid: '573001234567@s.whatsapp.net', text: 'secret text' };
  const failureSafety = createWhatsAppDeliverySafety({
    ...failureHarness,
    sendMessage: async () => { throw privateFailure; },
    onDiagnostic,
  });
  await assert.rejects(
    failureSafety.send('573001234567@s.whatsapp.net', { text: 'secret text' }),
    error => error === privateFailure,
  );

  const rateHarness = createHarness(t, {
    presenceEnabled: false,
    maxSendsPerMinute: 1,
  });
  const rateSafety = createWhatsAppDeliverySafety({
    ...rateHarness,
    sendMessage: async () => {},
    onDiagnostic,
  });
  await rateSafety.send('contact-a', { text: 'one' });
  await assert.rejects(
    rateSafety.send('contact-b', { text: 'two' }),
    error => error instanceof WhatsAppDeliveryBlockedError && error.code === 'local_rate_limit',
  );

  const fullHarness = createHarness(t, {
    presenceEnabled: false,
    maxPendingSends: 1,
  });
  let releaseFullQueue;
  const fullQueueGate = new Promise(resolve => { releaseFullQueue = resolve; });
  const fullSafety = createWhatsAppDeliverySafety({
    ...fullHarness,
    sendMessage: async () => fullQueueGate,
    onDiagnostic,
  });
  const pendingSend = fullSafety.send('contact-a', { text: 'one' });
  await assert.rejects(
    fullSafety.send('contact-b', { text: 'two' }),
    error => error instanceof WhatsAppDeliveryBlockedError && error.code === 'queue_full',
  );
  releaseFullQueue();
  await pendingSend;

  const expiredHarness = createHarness(t, {
    presenceEnabled: false,
    textDelayMinMs: 2_000,
    textDelayMaxMs: 2_000,
    queueWaitTimeoutMs: 1_000,
  });
  const expiredSafety = createWhatsAppDeliverySafety({
    ...expiredHarness,
    sendMessage: async () => {},
    onDiagnostic,
  });
  const expiredResults = await Promise.allSettled([
    expiredSafety.send('contact-a', { text: 'one' }),
    expiredSafety.send('contact-b', { text: 'two' }),
  ]);
  assert.equal(expiredResults[1].status, 'rejected');
  assert.equal(expiredResults[1].reason.code, 'queue_timeout');

  const timeoutHarness = createHarness(t, {
    presenceEnabled: false,
    sendTimeoutMs: 10,
  });
  let releaseTimedOutSend;
  let announceTimedOutSendStarted;
  const timeoutGate = new Promise(resolve => { releaseTimedOutSend = resolve; });
  const timeoutStarted = new Promise(resolve => { announceTimedOutSendStarted = resolve; });
  const timeoutSafety = createWhatsAppDeliverySafety({
    ...timeoutHarness,
    sendMessage: async () => {
      announceTimedOutSendStarted();
      return timeoutGate;
    },
    onDiagnostic,
  });
  const timedOutSend = timeoutSafety.send('contact-a', { text: 'one' });
  await timeoutStarted;
  await new Promise(resolve => setTimeout(resolve, 25));
  releaseTimedOutSend();
  await assert.rejects(timedOutSend, /send_timeout/);

  assert.deepEqual(diagnostics, [
    { type: 'provider_forbidden', statusCode: 403, attempt: 3 },
    { type: 'provider_rate_limited', statusCode: 429, attempt: 2 },
    { type: 'delivery_failure' },
    { type: 'local_rate_limit' },
    { type: 'queue_full' },
    { type: 'queue_timeout' },
    { type: 'delivery_timeout' },
  ]);
  const encoded = JSON.stringify(diagnostics);
  assert.doesNotMatch(encoded, /573001234567|@s\.whatsapp\.net|secret text|private provider response|payload|jid/);
  for (const diagnostic of diagnostics) {
    assert.ok(Object.keys(diagnostic).every(key => ['type', 'statusCode', 'attempt'].includes(key)));
  }

  const isolatedHarness = createHarness(t, { presenceEnabled: false });
  const originalError = new Error('original delivery failure');
  const isolatedSafety = createWhatsAppDeliverySafety({
    ...isolatedHarness,
    sendMessage: async () => { throw originalError; },
    onDiagnostic() { throw new Error('diagnostic callback failed'); },
  });
  await assert.rejects(
    isolatedSafety.send('contact-a', { text: 'one' }),
    error => error === originalError,
  );
});
