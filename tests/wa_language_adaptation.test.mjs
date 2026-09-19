import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { PersistentInteractionState } from '../interaction_state.mjs';
import { detectLanguageEvidence } from '../language_detection.mjs';
import { createWhatsAppMessageHandler } from '../wa_message_handler.mjs';

function harness(t, phone = '34600123456') {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-language-regression-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, 'state.json');
  const store = new PersistentInteractionState({ filePath });
  const sent = [];
  const handler = createWhatsAppMessageHandler({
    routeInteraction: details => store.register(details),
    detectLanguage: detectLanguageEvidence,
    getResponseMessage: (language, step) => ({ text: `${language}-${step}`, audio: '' }),
    sendMessage: async (_jid, content) => sent.push(content.text),
    readAudio: async () => null,
    logger: { info() {}, warn() {}, error() {} },
  });
  let eventId = 0;
  return {
    sent,
    contact: () => Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0],
    send: message => handler({
      type: 'notify',
      messages: [{
        key: { id: `synthetic-${++eventId}`, remoteJid: `${phone}@s.whatsapp.net`, fromMe: false },
        message,
      }],
    }),
  };
}

for (const phone of ['34600123456', '573001234567']) {
  for (const text of ['Hi', 'How much?']) {
    test(`first ${text} replaces provisional Spanish from +${phone.slice(0, 2)}`, async t => {
      const client = harness(t, phone);
      await client.send({ imageMessage: {} });
      assert.equal(client.contact().language, 'es');
      assert.equal(client.contact().language_provisional, true);
      assert.equal(detectLanguageEvidence(text).strong, false);

      await client.send({ conversation: text });

      assert.deepEqual(client.sent, ['es-step1', 'en-step2']);
      assert.equal(client.contact().language, 'en');
      assert.equal(client.contact().language_provisional, true);
      assert.equal(client.contact().language_candidate_streak, 1);
    });
  }
}

test('a short English media caption also replaces provisional Spanish', async t => {
  const client = harness(t);
  await client.send({ imageMessage: {} });
  await client.send({ imageMessage: { caption: 'How much?' } });
  assert.deepEqual(client.sent, ['es-step1', 'en-step2']);
});

test('confirmed Spanish requires two weak English observations', async t => {
  const client = harness(t);
  await client.send({ conversation: 'Hola, necesito ayuda' });
  assert.equal(client.contact().language_provisional, false);
  await client.send({ conversation: 'Hi' });
  assert.equal(client.contact().language, 'es');
  await client.send({ conversation: 'How much?' });

  assert.deepEqual(client.sent, ['es-step1', 'es-step2', 'en-step2']);
  assert.equal(client.contact().language_source, 'detected');
  assert.equal(client.contact().language_candidate, null);
});

for (const text of ['ok 👍', "I don't speak English"]) {
  test(`${text} does not change provisional Spanish`, async t => {
    const client = harness(t);
    await client.send({ imageMessage: {} });
    await client.send({ conversation: text });
    assert.deepEqual(client.sent, ['es-step1', 'es-step2']);
    assert.equal(client.contact().language, 'es');
    assert.equal(client.contact().language_provisional, true);
  });

  test(`${text} interrupts an unconfirmed switch from confirmed Spanish`, async t => {
    const client = harness(t);
    await client.send({ conversation: 'Hola, necesito ayuda' });
    await client.send({ conversation: 'Hi' });
    assert.equal(client.contact().language_candidate, 'en');
    await client.send({ conversation: text });
    assert.equal(client.contact().language_candidate, null);
    await client.send({ conversation: 'How much?' });
    assert.deepEqual(client.sent, ['es-step1', 'es-step2', 'es-step2', 'es-step2']);
    assert.equal(client.contact().language, 'es');
  });
}

test('an explicit English request overrides confirmed Spanish immediately', async t => {
  const client = harness(t);
  await client.send({ conversation: 'Hola, necesito ayuda' });
  await client.send({ conversation: 'English please' });
  assert.deepEqual(client.sent, ['es-step1', 'en-step2']);
  assert.equal(client.contact().language_source, 'detected');
});

for (const text of ['Are you free tonight?', 'Looking for a girl tonight']) {
  test(`a previously missed natural inquiry is English: ${text}`, async t => {
    const client = harness(t);
    await client.send({ imageMessage: {} });
    await client.send({ conversation: text });
    assert.deepEqual(client.sent, ['es-step1', 'en-step2']);
    assert.equal(client.contact().language, 'en');
  });
}
