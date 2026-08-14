import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { PersistentInteractionState } from '../interaction_state.mjs';
import { detectLanguageEvidence } from '../language_detection.mjs';

function createStore(directory, overrides = {}) {
  return new PersistentInteractionState({
    filePath: path.join(directory, 'state.json'),
    logger: { error() {} },
    ...overrides,
  });
}

test('cada llamada distinta usa call y el contenido posterior usa step2', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);

  const first = store.register({ contactId: 'a', eventId: 'call:1', kind: 'call' });
  const second = store.register({ contactId: 'a', eventId: 'call:2', kind: 'call' });
  const third = store.register({ contactId: 'a', eventId: 'message:3', kind: 'content' });

  assert.equal(first.responseKey, 'call');
  assert.equal(second.responseKey, 'call');
  assert.equal(third.responseKey, 'step2');
  assert.deepEqual([first.phase, second.phase, third.phase], [1, 2, 2]);
});

test('contenido, llamada y contenido producen step1, call y step2', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);

  const first = store.register({ contactId: 'a', eventId: 'message:1', kind: 'content' });
  const call = store.register({ contactId: 'a', eventId: 'call:2', kind: 'call' });
  const following = store.register({ contactId: 'a', eventId: 'message:3', kind: 'content' });

  assert.deepEqual(
    [first.responseKey, call.responseKey, following.responseKey],
    ['step1', 'call', 'step2'],
  );
  assert.deepEqual([first.phase, call.phase, following.phase], [1, 2, 2]);
});

test('primer contenido usa step1 y los contactos quedan aislados', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);

  assert.equal(store.register({ contactId: 'a', eventId: 'message:1', kind: 'content' }).responseKey, 'step1');
  assert.equal(store.register({ contactId: 'b', eventId: 'message:1', kind: 'content' }).responseKey, 'step1');
  assert.equal(store.register({ contactId: 'a', eventId: 'message:2', kind: 'content' }).responseKey, 'step2');
});

test('deduplicación y fase sobreviven un reinicio', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  store.register({ contactId: 'a', eventId: 'message:1', kind: 'content' });
  assert.equal(store.register({ contactId: 'a', eventId: 'message:1', kind: 'content' }).duplicate, true);

  const reloaded = createStore(directory);
  assert.equal(reloaded.register({ contactId: 'a', eventId: 'message:1', kind: 'content' }).duplicate, true);
  assert.equal(reloaded.register({ contactId: 'a', eventId: 'message:2', kind: 'content' }).responseKey, 'step2');
});

test('un evento sin texto usa idioma por defecto y el primer texto puede fijarlo', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory, { defaultLanguage: 'es' });

  const call = store.register({ contactId: 'a', eventId: 'call:1', kind: 'call' });
  const text = store.register({
    contactId: 'a',
    eventId: 'message:2',
    kind: 'content',
    detectedLanguage: 'fr',
  });

  assert.equal(call.language, 'es');
  assert.equal(text.language, 'fr');
});

test('un idioma provisional se guarda y el primer texto detectado lo confirma o reemplaza', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const store = createStore(directory);

  const image = store.register({
    contactId: 'a',
    eventId: 'message:image',
    kind: 'content',
    provisionalLanguage: 'es',
  });
  const english = store.register({
    contactId: 'a',
    eventId: 'message:text',
    kind: 'content',
    detectedLanguage: 'en',
    languageEvidence: detectLanguageEvidence('Are you available now?'),
    provisionalLanguage: 'es',
  });
  const laterFrench = store.register({
    contactId: 'a',
    eventId: 'message:later',
    kind: 'content',
    detectedLanguage: 'fr',
  });

  assert.equal(image.language, 'es');
  assert.equal(english.language, 'en');
  assert.equal(laterFrench.language, 'en');
  const persisted = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  const state = Object.values(persisted.contacts)[0];
  assert.equal(state.language, 'en');
  assert.equal(state.language_provisional, false);
});

test('el texto inicial confirma su idioma y no queda provisional', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);

  const first = store.register({
    contactId: 'a',
    eventId: 'message:text',
    kind: 'content',
    detectedLanguage: 'fr',
    languageEvidence: detectLanguageEvidence('bonjour'),
    provisionalLanguage: 'es',
  });

  assert.equal(first.language, 'fr');
  const state = Object.values(store.contacts)[0];
  assert.equal(state.language_provisional, false);
});

test('un idioma confirmado heredado no se reemplaza como si fuera provisional', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const contactKey = crypto
    .createHash('sha256')
    .update('contact\0a', 'utf8')
    .digest('hex');
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [contactKey]: {
        phase: 1,
        language: 'es',
        recent_events: [],
        updated_at: 1,
      },
    },
    aliases: {},
  }));
  const store = createStore(directory);

  const decision = store.register({
    contactId: 'a',
    eventId: 'message:text',
    kind: 'content',
    detectedLanguage: 'en',
  });

  assert.equal(decision.language, 'es');
});

test('evidencia fuerte reemplaza inmediatamente un idioma confirmado u operator seed', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const contactKey = crypto.createHash('sha256').update('contact\0a', 'utf8').digest('hex');
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [contactKey]: {
        phase: 1,
        language: 'fr',
        language_source: 'operator_seed',
        recent_events: [],
        updated_at: 1,
      },
    },
    aliases: {},
  }));
  const store = createStore(directory);

  const decision = store.register({
    contactId: 'a',
    eventId: 'message:spanish',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('que chicas están disponibles por Rubí'),
  });

  const contact = Object.values(store.contacts)[0];
  assert.equal(decision.language, 'es');
  assert.equal(contact.language_source, 'detected');
  assert.equal(contact.language_candidate, null);
  assert.doesNotMatch(fs.readFileSync(filePath, 'utf8'), /operator_seed/);
});

test('candidato débil requiere dos eventos, sobrevive reload y duplicado/no-texto no lo mutan', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const store = createStore(directory);
  store.register({
    contactId: 'a',
    eventId: 'message:french',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('bonjour'),
  });
  const weakEnglish = detectLanguageEvidence('want');
  const first = store.register({
    contactId: 'a',
    eventId: 'message:weak-1',
    kind: 'content',
    languageEvidence: weakEnglish,
  });
  const duplicate = store.register({
    contactId: 'a',
    eventId: 'message:weak-1',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('hola'),
  });

  assert.equal(first.language, 'fr');
  assert.equal(duplicate.duplicate, true);
  assert.equal(Object.values(store.contacts)[0].language_candidate, 'en');
  assert.equal(Object.values(store.contacts)[0].language_candidate_streak, 1);

  const reloaded = createStore(directory);
  reloaded.register({ contactId: 'a', eventId: 'call:between', kind: 'call' });
  assert.equal(Object.values(reloaded.contacts)[0].language_candidate, 'en');
  const second = reloaded.register({
    contactId: 'a',
    eventId: 'message:weak-2',
    kind: 'content',
    languageEvidence: weakEnglish,
  });
  assert.equal(second.language, 'en');
  assert.equal(Object.values(reloaded.contacts)[0].language_candidate, null);
  assert.doesNotMatch(fs.readFileSync(filePath, 'utf8'), /want|bonjour/);
});

test('texto ambiguo corta la racha y una convergencia PN/LID conserva sólo la canónica', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  const weakEnglish = detectLanguageEvidence('want');
  store.register({
    contactId: '123@lid',
    eventId: 'message:french',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('bonjour'),
  });
  store.register({
    contactId: '123@lid',
    eventId: 'message:weak-1',
    kind: 'content',
    languageEvidence: weakEnglish,
  });
  store.register({
    contactId: '123@lid',
    eventId: 'message:ambiguous',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('photo video'),
  });
  store.register({
    contactId: '123@lid',
    eventId: 'message:weak-after',
    kind: 'content',
    languageEvidence: weakEnglish,
  });
  assert.equal(Object.values(store.contacts)[0].language_candidate_streak, 1);

  store.register({
    contactId: '573001234567@s.whatsapp.net',
    contactAliases: ['123@lid'],
    eventId: 'call:merge',
    kind: 'call',
  });
  const contact = Object.values(store.contacts)[0];
  assert.equal(contact.language, 'fr');
  assert.equal(contact.language_candidate, 'en');
  assert.equal(contact.language_candidate_streak, 1);
});

test('fusionar dos candidatos iguales no suma sus rachas ni confirma idioma', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const lid = '123@lid';
  const pn = '573001234567@s.whatsapp.net';
  const fingerprint = value => crypto
    .createHash('sha256')
    .update(`contact\0${value}`, 'utf8')
    .digest('hex');
  const lidKey = fingerprint(lid);
  const pnKey = fingerprint(pn);
  const contact = {
    phase: 1,
    language: 'fr',
    language_provisional: false,
    language_source: 'detected',
    language_candidate: 'en',
    language_candidate_streak: 1,
    recent_events: [],
    updated_at: 1,
  };
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: { [pnKey]: contact, [lidKey]: { ...contact, updated_at: 2 } },
    aliases: { [pnKey]: pnKey, [lidKey]: lidKey },
  }));

  const store = createStore(directory);
  const decision = store.register({
    contactId: pn,
    contactAliases: [lid, pn],
    eventId: 'call:merge-candidates',
    kind: 'call',
  });

  const merged = Object.values(store.contacts)[0];
  assert.equal(decision.language, 'fr');
  assert.equal(merged.language_candidate, 'en');
  assert.equal(merged.language_candidate_streak, 1);
  assert.equal(Object.keys(store.contacts).length, 1);
});

test('al converger dos idiomas detected del mismo rango gana el estado más reciente', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const pn = '573001234567@s.whatsapp.net';
  const lid = '123@lid';
  const fingerprint = value => crypto
    .createHash('sha256')
    .update(`contact\0${value}`, 'utf8')
    .digest('hex');
  const pnKey = fingerprint(pn);
  const lidKey = fingerprint(lid);
  const contact = (language, updatedAt) => ({
    phase: 1,
    language,
    language_provisional: false,
    language_source: 'detected',
    language_candidate: null,
    language_candidate_streak: 0,
    recent_events: [],
    updated_at: updatedAt,
  });
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [pnKey]: contact('es', 100),
      [lidKey]: contact('en', 200),
    },
    aliases: { [pnKey]: pnKey, [lidKey]: lidKey },
  }));

  const store = createStore(directory);
  const decision = store.register({
    contactId: pn,
    contactAliases: [lid],
    eventId: 'call:merge-newer',
    kind: 'call',
  });

  assert.equal(decision.language, 'en');
  assert.equal(Object.values(store.contacts)[0].language, 'en');
  assert.equal(Object.keys(store.contacts).length, 1);
});

test('idioma provisional requiere dos observaciones débiles concordantes', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  store.register({
    contactId: 'a',
    eventId: 'message:image',
    kind: 'content',
    provisionalLanguage: 'fr',
  });
  const weak = detectLanguageEvidence('want');
  const first = store.register({
    contactId: 'a',
    eventId: 'message:weak-1',
    kind: 'content',
    languageEvidence: weak,
    provisionalLanguage: 'fr',
  });
  let contact = Object.values(store.contacts)[0];
  assert.equal(first.language, 'fr');
  assert.equal(contact.language_provisional, true);
  assert.equal(contact.language_candidate, 'en');
  assert.equal(contact.language_candidate_streak, 1);

  const second = store.register({
    contactId: 'a',
    eventId: 'message:weak-2',
    kind: 'content',
    languageEvidence: weak,
    provisionalLanguage: 'fr',
  });
  contact = Object.values(store.contacts)[0];
  assert.equal(second.language, 'en');
  assert.equal(contact.language_provisional, false);
  assert.equal(contact.language_candidate, null);
});

test('español natural fuerte corrige de inmediato un provisional francés', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  store.register({
    contactId: 'a',
    eventId: 'message:image',
    kind: 'content',
    provisionalLanguage: 'fr',
  });

  const decision = store.register({
    contactId: 'a',
    eventId: 'message:spanish',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('Estoy buscando una chica disponible'),
  });

  assert.equal(decision.language, 'es');
  assert.equal(Object.values(store.contacts)[0].language_source, 'detected');
});

test('evidence malformada falla cerrada y no cambia idioma ni candidato', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  store.register({
    contactId: 'a',
    eventId: 'message:french',
    kind: 'content',
    languageEvidence: detectLanguageEvidence('bonjour'),
  });
  const decision = store.register({
    contactId: 'a',
    eventId: 'message:malformed',
    kind: 'content',
    languageEvidence: { language: 'es', strong: true },
  });

  const contact = Object.values(store.contacts)[0];
  assert.equal(decision.language, 'fr');
  assert.equal(contact.language_candidate, null);
  assert.equal(contact.language_candidate_streak, 0);
});

test('el archivo persistente no expone identificadores crudos', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const store = createStore(directory);
  store.register({
    contactId: '573001234567@s.whatsapp.net',
    eventId: 'sensitive-event-id',
    kind: 'content',
  });

  const serialized = fs.readFileSync(path.join(directory, 'state.json'), 'utf8');
  assert.doesNotMatch(serialized, /573001234567|sensitive-event-id/);
});

test('fusiona LID y PN y conserva el alias después de reiniciar', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const store = new PersistentInteractionState({ filePath });

  assert.equal(store.register({
    contactId: '123@lid',
    eventId: 'message:1',
    kind: 'content',
  }).responseKey, 'step1');
  assert.equal(store.register({
    contactId: '573001234567@s.whatsapp.net',
    contactAliases: ['123@lid', '573001234567@s.whatsapp.net'],
    eventId: 'call:2',
    kind: 'call',
  }).responseKey, 'call');

  const reloaded = new PersistentInteractionState({ filePath });
  assert.equal(reloaded.register({
    contactId: '123@lid',
    eventId: 'message:3',
    kind: 'content',
  }).responseKey, 'step2');
  assert.equal(Object.keys(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts).length, 1);
});

test('un reset pendiente por PN prevalece una sola vez sobre historial LID', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const lid = '123@lid';
  const pn = '573001234567@s.whatsapp.net';
  const fingerprint = value => crypto
    .createHash('sha256')
    .update(`contact\0${value}`, 'utf8')
    .digest('hex');

  const lidKey = fingerprint(lid);
  const pnKey = fingerprint(pn);
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [lidKey]: {
        phase: 2,
        language: 'es',
        recent_events: ['old-event'],
        updated_at: 50,
      },
      [pnKey]: {
        phase: 0,
        language: 'en',
        recent_events: [],
        updated_at: 0,
        reset_pending: true,
      },
    },
    aliases: { [lidKey]: lidKey, [pnKey]: pnKey },
  }));

  const store = new PersistentInteractionState({ filePath });
  const first = store.register({
    contactId: pn,
    contactAliases: [lid, pn],
    eventId: 'message:after-reset',
    kind: 'content',
    detectedLanguage: 'fr',
  });
  const second = store.register({
    contactId: lid,
    contactAliases: [lid, pn],
    eventId: 'message:after-reset-2',
    kind: 'content',
    detectedLanguage: 'fr',
  });

  assert.equal(first.responseKey, 'step1');
  assert.equal(first.language, 'en');
  assert.equal(second.responseKey, 'step2');
  assert.equal(second.language, 'fr');
  const serialized = fs.readFileSync(filePath, 'utf8');
  assert.doesNotMatch(serialized, /reset_pending|573001234567|123@lid/);
  assert.equal(Object.keys(JSON.parse(serialized).contacts).length, 1);
});

test('ignora un reset pendiente manipulado fuera de fase cero', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const contact = 'contacto';
  const contactKey = crypto
    .createHash('sha256')
    .update(`contact\0${contact}`, 'utf8')
    .digest('hex');
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [contactKey]: {
        phase: 2,
        language: 'es',
        recent_events: [],
        updated_at: 50,
        reset_pending: true,
      },
    },
    aliases: { [contactKey]: contactKey },
  }));

  const store = new PersistentInteractionState({ filePath });
  const decision = store.register({
    contactId: contact,
    eventId: 'message:new',
    kind: 'content',
    detectedLanguage: 'en',
  });

  assert.equal(decision.responseKey, 'step2');
  assert.equal(decision.language, 'es');
  assert.doesNotMatch(fs.readFileSync(filePath, 'utf8'), /reset_pending/);
});

test('encuentra el reset PN aunque el LID sea primario y un alias oculte su clave directa', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'interaction-state-'));
  const filePath = path.join(directory, 'state.json');
  const lid = '123@lid';
  const pn = '573001234567@s.whatsapp.net';
  const fingerprint = value => crypto
    .createHash('sha256')
    .update(`contact\0${value}`, 'utf8')
    .digest('hex');
  const lidKey = fingerprint(lid);
  const pnKey = fingerprint(pn);
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    contacts: {
      [lidKey]: {
        phase: 2,
        language: 'es',
        recent_events: ['old-event'],
        updated_at: 50,
      },
      [pnKey]: {
        phase: 0,
        language: 'fr',
        recent_events: [],
        updated_at: 0,
        reset_pending: true,
      },
    },
    // Simulates an older convergence that points PN at LID while the direct
    // PN state still carries the number-specific reset.
    aliases: { [lidKey]: lidKey, [pnKey]: lidKey },
  }));

  const store = new PersistentInteractionState({ filePath });
  const decision = store.register({
    contactId: lid,
    contactAliases: [lid, pn],
    eventId: 'message:after-reset',
    kind: 'content',
    detectedLanguage: 'en',
  });

  assert.equal(decision.responseKey, 'step1');
  assert.equal(decision.language, 'fr');
  const serialized = fs.readFileSync(filePath, 'utf8');
  assert.doesNotMatch(serialized, /reset_pending|573001234567|123@lid/);
  assert.equal(Object.keys(JSON.parse(serialized).contacts).length, 1);
});
