import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { PersistentInteractionState } from '../interaction_state.mjs';
import { reduceLanguageState } from '../language_adaptation.mjs';
import { detectLanguageEvidence } from '../language_detection.mjs';

test('pre-rollout provisional candidate confirms on the next matching observation', () => {
  const previous = {
    language: 'es', language_source: 'provisional', language_provisional: true,
    language_candidate: 'en', language_candidate_streak: 1,
  };
  const result = reduceLanguageState(previous, { languageEvidence: detectLanguageEvidence('How much?') });
  assert.equal(result.language, 'en');
  assert.equal(result.language_source, 'detected');
  assert.equal(result.language_provisional, false);
  assert.equal(result.language_candidate, null);
  assert.equal(result.language_candidate_streak, 0);
  assert.equal(previous.language, 'es');
});

test('pre-rollout candidate survives store reload and confirms once', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'language-candidate-migration-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, 'state.json');
  const store = new PersistentInteractionState({ filePath });
  store.register({ contactId: 'a', eventId: 'image:1', kind: 'content', provisionalLanguage: 'es' });
  const saved = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  const contact = Object.values(saved.contacts)[0];
  // Old policy retained provisional ES after its first weak EN text.
  contact.language_candidate = 'en';
  contact.language_candidate_streak = 1;
  fs.writeFileSync(filePath, JSON.stringify(saved), 'utf8');

  const reloaded = new PersistentInteractionState({ filePath });
  const result = reloaded.register({
    contactId: 'a', eventId: 'message:next', kind: 'content',
    languageEvidence: detectLanguageEvidence('How much?'),
  });
  assert.equal(result.language, 'en');
  const persisted = Object.values(JSON.parse(fs.readFileSync(filePath, 'utf8')).contacts)[0];
  assert.equal(persisted.language_source, 'detected');
  assert.equal(persisted.language_provisional, false);
  assert.equal(persisted.language_candidate, null);
  assert.equal(persisted.language_candidate_streak, 0);
});

test('legacy, operator_seed and confirmed states still require two weak observations', () => {
  for (const source of ['legacy', 'operator_seed', 'detected']) {
    const first = reduceLanguageState({
      language: 'es', language_source: source, language_provisional: false,
    }, { languageEvidence: detectLanguageEvidence('Hi') });
    assert.equal(first.language, 'es', source);
    assert.equal(first.language_source, source);
    assert.equal(first.language_candidate, 'en');
    const second = reduceLanguageState(first, { languageEvidence: detectLanguageEvidence('How much?') });
    assert.equal(second.language, 'en', source);
    assert.equal(second.language_source, 'detected');
    assert.equal(second.language_candidate, null);
  }
});

test('an unannotated persisted language remains legacy, not provisional', () => {
  const result = reduceLanguageState({ language: 'es' }, { languageEvidence: detectLanguageEvidence('Hi') });
  assert.equal(result.language, 'es');
  assert.equal(result.language_source, 'legacy');
  assert.equal(result.language_provisional, false);
});

test('first weak text can be replaced without premature confirmation in every language', () => {
  const weak = language => ({ language, strong: false, explicit: false, score: 4, margin: 4 });
  for (const [initial, incoming] of [['es', 'en'], ['en', 'fr'], ['fr', 'es']]) {
    const original = reduceLanguageState(null, { languageEvidence: weak(initial) });
    const changed = reduceLanguageState(original, { languageEvidence: weak(incoming) });
    assert.equal(changed.language, incoming);
    assert.equal(changed.language_provisional, true);
    assert.equal(changed.language_candidate, incoming);
    assert.equal(changed.language_candidate_streak, 1);
    assert.equal(original.language, initial);

    const confirmed = reduceLanguageState(changed, { languageEvidence: weak(incoming) });
    assert.equal(confirmed.language, incoming);
    assert.equal(confirmed.language_provisional, false);
  }
});
