import test from 'node:test';
import assert from 'node:assert/strict';

import { detectSupportedLanguage } from '../language_detection.mjs';

test('solo acepta un idioma con puntuación máxima única', () => {
  assert.equal(detectSupportedLanguage('photo'), null);
  assert.equal(detectSupportedLanguage('video'), null);
  assert.equal(detectSupportedLanguage('ok'), null);
  assert.equal(detectSupportedLanguage('Are you available now?'), 'en');
  assert.equal(detectSupportedLanguage('bonjour'), 'fr');
});

test('un marcador explícito rompe una ambigüedad salvo que exista empate real', () => {
  assert.equal(detectSupportedLanguage('english photo'), 'en');
  assert.equal(detectSupportedLanguage('english français'), null);
});
