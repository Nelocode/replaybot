import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';

import { detectLanguageEvidence, detectSupportedLanguage } from '../language_detection.mjs';

test('Python y Node comparten el mismo corpus contractual', () => {
  const contract = JSON.parse(fs.readFileSync(
    new URL('../language_contract_cases.json', import.meta.url),
    'utf8',
  ));
  for (const entry of contract.cases) {
    assert.deepEqual(detectLanguageEvidence(entry.text), entry.expected, entry.id);
  }
  for (const entry of contract.generated_cases) {
    const text = `${entry.prefix.repeat(entry.repeat)}${entry.suffix}`;
    assert.deepEqual(detectLanguageEvidence(text), entry.expected, entry.id);
  }
});

test('solo acepta un idioma con puntuación máxima única', () => {
  assert.equal(detectSupportedLanguage('photo'), null);
  assert.equal(detectSupportedLanguage('video'), null);
  assert.equal(detectSupportedLanguage('ok'), null);
  assert.equal(detectSupportedLanguage('Are you available now?'), 'en');
  assert.equal(detectSupportedLanguage('bonjour'), 'fr');
});

test('una solicitud explícita rompe una ambigüedad salvo que exista empate real', () => {
  assert.equal(detectSupportedLanguage('English'), 'en');
  assert.equal(detectSupportedLanguage('english français'), null);
});

test('los límites de texto y tokens acotan el trabajo del detector', () => {
  assert.equal(detectSupportedLanguage(`${'x'.repeat(4096)} hello`), null);
  assert.equal(detectSupportedLanguage(`${'x '.repeat(256)}hello`), null);
});
