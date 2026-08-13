import test from 'node:test';
import assert from 'node:assert/strict';

import { provisionalLanguageFromWhatsAppIdentity } from '../whatsapp_language_hint.mjs';

test('deriva los idiomas soportados desde PN de WhatsApp', () => {
  assert.equal(provisionalLanguageFromWhatsAppIdentity('573001234567@s.whatsapp.net'), 'es');
  assert.equal(provisionalLanguageFromWhatsAppIdentity('34600123456@hosted'), 'es');
  assert.equal(provisionalLanguageFromWhatsAppIdentity('33612345678@s.whatsapp.net'), 'fr');
  assert.equal(provisionalLanguageFromWhatsAppIdentity('447700900123@s.whatsapp.net'), 'en');
});

test('aplica excepciones NANP antes del prefijo general +1', () => {
  assert.equal(provisionalLanguageFromWhatsAppIdentity('17875550123@s.whatsapp.net'), 'es');
  assert.equal(provisionalLanguageFromWhatsAppIdentity('18095550123@s.whatsapp.net'), 'es');
  assert.equal(provisionalLanguageFromWhatsAppIdentity('14155550123@s.whatsapp.net'), 'en');
});

test('no infiere desde LID, grupos, texto libre ni código ambiguo', () => {
  assert.equal(provisionalLanguageFromWhatsAppIdentity('573001234567@lid'), null);
  assert.equal(provisionalLanguageFromWhatsAppIdentity('573001234567@g.us'), null);
  assert.equal(provisionalLanguageFromWhatsAppIdentity('+57 300 123 4567'), null);
  assert.equal(provisionalLanguageFromWhatsAppIdentity('41791234567@s.whatsapp.net'), null);
});

test('encuentra PN dentro del conjunto de alias', () => {
  assert.equal(provisionalLanguageFromWhatsAppIdentity(
    ['123456789@lid', '573001234567@s.whatsapp.net'],
  ), 'es');
});
