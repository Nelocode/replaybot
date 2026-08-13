// Country-code hints are deliberately provisional. A later message whose
// text identifies a supported language always replaces this value.
const PREFIX_LANGUAGES = new Map([
  // Spanish-speaking destinations. NANP territories are listed before +1.
  ['1787', 'es'], ['1939', 'es'], ['1809', 'es'], ['1829', 'es'], ['1849', 'es'],
  ['34', 'es'], ['51', 'es'], ['52', 'es'], ['53', 'es'], ['54', 'es'],
  ['56', 'es'], ['57', 'es'], ['58', 'es'], ['591', 'es'],
  ['593', 'es'], ['595', 'es'], ['598', 'es'], ['502', 'es'], ['503', 'es'],
  ['504', 'es'], ['505', 'es'], ['506', 'es'], ['507', 'es'],

  // French-speaking destinations among the languages supported by the bot.
  ['33', 'fr'], ['377', 'fr'], ['590', 'fr'], ['594', 'fr'], ['596', 'fr'],
  ['262', 'fr'], ['508', 'fr'], ['509', 'fr'], ['687', 'fr'], ['689', 'fr'],

  // English-speaking destinations among the languages supported by the bot.
  ['1', 'en'], ['44', 'en'], ['61', 'en'], ['64', 'en'], ['353', 'en'],
]);

const ORDERED_PREFIXES = [...PREFIX_LANGUAGES.keys()]
  .sort((left, right) => right.length - left.length);

function phoneDigitsFromJid(value) {
  if (typeof value !== 'string') return '';
  const normalized = value.trim().toLowerCase();
  if (!normalized.endsWith('@s.whatsapp.net') && !normalized.endsWith('@hosted')) return '';
  const localPart = normalized.split('@', 1)[0].split(':', 1)[0];
  return /^\d{7,15}$/.test(localPart) ? localPart : '';
}

export function provisionalLanguageFromWhatsAppIdentity(...identityValues) {
  for (const value of identityValues.flat()) {
    const digits = phoneDigitsFromJid(value);
    if (!digits) continue;
    const prefix = ORDERED_PREFIXES.find(candidate => digits.startsWith(candidate));
    if (prefix) return PREFIX_LANGUAGES.get(prefix);
  }
  return null;
}
