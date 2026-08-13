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
