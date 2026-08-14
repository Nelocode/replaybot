import fs from 'fs';

const LANGUAGES = Object.freeze(['es', 'en', 'fr']);
const RULES_URL = new URL('./language_rules.json', import.meta.url);
const APOSTROPHES = /[’‘`´]/gu;
const TOKEN_PATTERN = /[\p{L}\p{N}]+/gu;

function normalizeUnbounded(text) {
  return text
    .normalize('NFKC')
    .toLowerCase()
    .replace(APOSTROPHES, "'")
    .normalize('NFD')
    .replace(/\p{M}/gu, '');
}

function tokensUnbounded(text) {
  return normalizeUnbounded(text).match(TOKEN_PATTERN) || [];
}

function positiveInteger(value, label) {
  if (!Number.isInteger(value) || value < 1) throw new TypeError(`${label} must be a positive integer`);
  return value;
}

function hasExactKeys(value, expected) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}

function loadRules() {
  const parsed = JSON.parse(fs.readFileSync(RULES_URL, 'utf8'));
  const rootKeys = ['explicit_context', 'language_names', 'limits', 'negation', 'neutral_terms', 'rules', 'thresholds', 'version'];
  if (!hasExactKeys(parsed, rootKeys) || parsed.version !== 1) {
    throw new TypeError('language rules must use schema version 1');
  }
  const {
    limits,
    thresholds,
    language_names: names,
    explicit_context: explicitContext,
    negation,
    rules,
  } = parsed;
  if (
    !hasExactKeys(limits, ['max_text_chars', 'max_tokens'])
    || !hasExactKeys(thresholds, ['explicit_weight', 'strong_margin', 'strong_score', 'weak_margin', 'weak_score'])
    || !hasExactKeys(explicitContext, ['contrastive_connectors', 'courtesy_suffixes', 'prefixes'])
    || !hasExactKeys(negation, ['lookback_tokens', 'sequences', 'tokens'])
    || !names || typeof names !== 'object' || Array.isArray(names)
    || !rules || typeof rules !== 'object' || Array.isArray(rules)
    || !Array.isArray(parsed.neutral_terms)
    || !Array.isArray(explicitContext.prefixes)
    || !Array.isArray(explicitContext.courtesy_suffixes)
    || !Array.isArray(explicitContext.contrastive_connectors)
    || !Array.isArray(negation.tokens)
    || !Array.isArray(negation.sequences)
  ) {
    throw new TypeError('language rules sections are invalid');
  }
  positiveInteger(limits.max_text_chars, 'max_text_chars');
  positiveInteger(limits.max_tokens, 'max_tokens');
  for (const name of ['weak_score', 'weak_margin', 'strong_score', 'strong_margin', 'explicit_weight']) {
    positiveInteger(thresholds[name], name);
  }
  if (thresholds.strong_score < thresholds.weak_score) {
    throw new TypeError('strong_score must not be below weak_score');
  }
  if (thresholds.strong_margin < thresholds.weak_margin) {
    throw new TypeError('strong_margin must not be below weak_margin');
  }
  positiveInteger(negation.lookback_tokens, 'negation lookback_tokens');
  if (negation.lookback_tokens > 8) {
    throw new TypeError('negation lookback_tokens must be at most 8');
  }

  const neutral = new Set();
  for (const term of parsed.neutral_terms) {
    const tokens = typeof term === 'string' ? tokensUnbounded(term) : [];
    if (tokens.length !== 1 || neutral.has(tokens[0])) {
      throw new TypeError('neutral terms must each normalize to one unique token');
    }
    neutral.add(tokens[0]);
  }
  if (
    Object.keys(names).sort().join(',') !== [...LANGUAGES].sort().join(',')
    || Object.keys(rules).sort().join(',') !== [...LANGUAGES].sort().join(',')
  ) {
    throw new TypeError('language rules must define exactly es, en, and fr');
  }

  const nameTargets = new Map();
  const compiledRules = {};
  for (const language of LANGUAGES) {
    if (!Array.isArray(names[language]) || !names[language].length || !Array.isArray(rules[language])) {
      throw new TypeError(`rules for ${language} must be arrays`);
    }
    for (const name of names[language]) {
      const tokens = typeof name === 'string' ? tokensUnbounded(name) : [];
      if (tokens.length !== 1 || nameTargets.has(tokens[0])) {
        throw new TypeError(`language names must be unique single tokens: ${language}`);
      }
      nameTargets.set(tokens[0], language);
    }
    const seen = new Set();
    compiledRules[language] = rules[language].map(rule => {
      const tokens = rule && typeof rule.term === 'string' ? tokensUnbounded(rule.term) : [];
      const key = tokens.join('\0');
      positiveInteger(rule?.weight, 'rule weight');
      if (
        !hasExactKeys(rule, ['decisive', 'term', 'weight'])
        || !tokens.length
        || typeof rule.decisive !== 'boolean'
        || seen.has(key)
      ) {
        throw new TypeError(`invalid or duplicate rule for ${language}`);
      }
      if (tokens.length === 1 && neutral.has(tokens[0])) {
        throw new TypeError(`neutral token cannot score for ${language}: ${tokens[0]}`);
      }
      seen.add(key);
      return Object.freeze({ tokens: Object.freeze(tokens), weight: rule.weight, decisive: rule.decisive });
    });
  }

  const negationTokens = new Set();
  for (const term of negation.tokens) {
    const tokens = typeof term === 'string' ? tokensUnbounded(term) : [];
    if (tokens.length !== 1 || negationTokens.has(tokens[0])) {
      throw new TypeError('negation tokens must each normalize to one unique token');
    }
    negationTokens.add(tokens[0]);
  }
  const negationSequences = [];
  const seenNegationSequences = new Set();
  for (const term of negation.sequences) {
    const tokens = typeof term === 'string' ? tokensUnbounded(term) : [];
    const key = tokens.join('\0');
    if (
      tokens.length < 2
      || tokens.length > negation.lookback_tokens
      || seenNegationSequences.has(key)
    ) {
      throw new TypeError('negation sequences must be unique and fit the lookback window');
    }
    seenNegationSequences.add(key);
    negationSequences.push(Object.freeze(tokens));
  }

  const compileUniquePhrases = (rawTerms, label) => {
    const compiled = [];
    const seen = new Set();
    for (const term of rawTerms) {
      const tokens = typeof term === 'string' ? tokensUnbounded(term) : [];
      const key = tokens.join('\0');
      if (!tokens.length || tokens.length > negation.lookback_tokens || seen.has(key)) {
        throw new TypeError(`${label} must be unique and fit the bounded context`);
      }
      seen.add(key);
      compiled.push(Object.freeze(tokens));
    }
    if (!compiled.length) throw new TypeError(`${label} must not be empty`);
    return Object.freeze(compiled);
  };
  const prefixes = compileUniquePhrases(explicitContext.prefixes, 'explicit prefixes');
  const courtesySuffixes = compileUniquePhrases(
    explicitContext.courtesy_suffixes,
    'courtesy suffixes',
  );
  const connectors = new Set();
  for (const connector of explicitContext.contrastive_connectors) {
    const tokens = typeof connector === 'string' ? tokensUnbounded(connector) : [];
    if (tokens.length !== 1 || connectors.has(tokens[0])) {
      throw new TypeError('contrastive connectors must be unique single tokens');
    }
    connectors.add(tokens[0]);
  }
  if (!connectors.size) throw new TypeError('contrastive connectors must not be empty');
  return Object.freeze({
    limits: Object.freeze({ ...limits }),
    thresholds: Object.freeze({ ...thresholds }),
    neutral,
    nameTargets,
    explicitContext: Object.freeze({ prefixes, courtesySuffixes }),
    contrastiveConnectors: connectors,
    negation: Object.freeze({
      lookbackTokens: negation.lookback_tokens,
      tokens: negationTokens,
      sequences: Object.freeze(negationSequences),
    }),
    rules: Object.freeze(compiledRules),
  });
}

const RULES = loadRules();

export function normalizeLanguageText(text) {
  if (typeof text !== 'string') return '';
  let codePoints = 0;
  let end = 0;
  for (const character of text) {
    if (codePoints >= RULES.limits.max_text_chars) break;
    end += character.length;
    codePoints += 1;
  }
  return normalizeUnbounded(text.slice(0, end));
}

export function tokenizeLanguageText(text) {
  return (normalizeLanguageText(text).match(TOKEN_PATTERN) || []).slice(0, RULES.limits.max_tokens);
}

function tokenizeLanguageClauses(text) {
  const normalized = normalizeLanguageText(text);
  const rawClauses = normalized.split(/[^\p{L}\p{N}'\s]+/gu);
  const clauses = [];
  let remaining = RULES.limits.max_tokens;
  for (const clause of rawClauses) {
    if (remaining <= 0) break;
    const tokens = (clause.match(TOKEN_PATTERN) || []).slice(0, remaining);
    if (tokens.length) {
      clauses.push(tokens);
      remaining -= tokens.length;
    }
  }
  return clauses;
}

function containsSequence(tokens, phrase) {
  if (phrase.length > tokens.length) return false;
  for (let index = 0; index <= tokens.length - phrase.length; index += 1) {
    if (phrase.every((token, offset) => tokens[index + offset] === token)) return true;
  }
  return false;
}

function isNegatedLanguageName(tokens, nameIndex) {
  let start = Math.max(0, nameIndex - RULES.negation.lookbackTokens);
  for (let index = nameIndex - 1; index >= start; index -= 1) {
    if (RULES.nameTargets.has(tokens[index])) {
      start = index + 1;
      break;
    }
  }
  const window = tokens.slice(start, nameIndex);
  return (
    window.some(token => RULES.negation.tokens.has(token))
    || RULES.negation.sequences.some(sequence => containsSequence(window, sequence))
  );
}

function endsWithSequence(tokens, end, phrase) {
  const start = end - phrase.length;
  return start >= 0 && phrase.every((token, offset) => tokens[start + offset] === token);
}

function startsWithSequence(tokens, start, phrase) {
  return start + phrase.length <= tokens.length
    && phrase.every((token, offset) => tokens[start + offset] === token);
}

function isLanguageRequest(tokens, nameIndex) {
  const nonNames = tokens.filter((token, index) => (
    index !== nameIndex && !RULES.nameTargets.has(token)
  ));
  if (
    !nonNames.length
    || RULES.explicitContext.courtesySuffixes.some(courtesy => (
      courtesy.length === nonNames.length
      && courtesy.every((token, index) => nonNames[index] === token)
    ))
  ) {
    return true;
  }
  if (RULES.explicitContext.prefixes.some(prefix => endsWithSequence(tokens, nameIndex, prefix))) {
    return true;
  }
  return RULES.explicitContext.courtesySuffixes.some(courtesy => (
    startsWithSequence(tokens, nameIndex + 1, courtesy)
  ));
}

function directiveSegments(clauses) {
  const segments = [];
  for (const clause of clauses) {
    let current = [];
    for (const token of clause) {
      if (RULES.contrastiveConnectors.has(token)) {
        if (current.length) {
          segments.push(current);
          current = [];
        }
      } else {
        current.push(token);
      }
    }
    if (current.length) segments.push(current);
  }
  return segments;
}

export function detectLanguageEvidence(text) {
  const clauses = tokenizeLanguageClauses(text);
  const tokens = clauses.flat();
  const scores = { es: 0, en: 0, fr: 0 };
  const decisive = { es: false, en: false, fr: false };
  const lastDirective = new Map();
  let lastPositiveGroup = new Set();

  for (const segment of directiveSegments(clauses)) {
    const segmentPositive = new Set();
    const negativeNameIndices = new Set();
    let previousNameIndex = null;
    for (let index = 0; index < segment.length; index += 1) {
      const target = RULES.nameTargets.get(segment[index]);
      if (!target) continue;
      if (isNegatedLanguageName(segment, index)) {
        lastDirective.set(target, 'negative');
        negativeNameIndices.add(index);
      } else {
        let request = isLanguageRequest(segment, index);
        if (!request && negativeNameIndices.has(previousNameIndex)) {
          const tail = segment.slice(previousNameIndex + 1);
          request = isLanguageRequest(tail, index - previousNameIndex - 1);
        }
        if (request) {
          lastDirective.set(target, 'positive');
          segmentPositive.add(target);
        }
      }
      previousNameIndex = index;
    }
    if (segmentPositive.size) lastPositiveGroup = segmentPositive;
  }
  const explicitHits = new Set(
    [...lastPositiveGroup].filter(language => lastDirective.get(language) === 'positive'),
  );
  const negatedTargets = new Set(
    [...lastDirective].filter(([, directive]) => directive === 'negative').map(([language]) => language),
  );

  for (const language of LANGUAGES) {
    if (explicitHits.has(language)) scores[language] += RULES.thresholds.explicit_weight;
    for (const rule of RULES.rules[language]) {
      if (containsSequence(tokens, rule.tokens)) {
        scores[language] += rule.weight;
        decisive[language] ||= rule.decisive;
      }
    }
  }

  for (const language of negatedTargets) {
    scores[language] = 0;
    decisive[language] = false;
  }

  const orderedScores = Object.values(scores).sort((left, right) => right - left);
  const score = orderedScores[0];
  let margin = score - orderedScores[1];
  const winners = LANGUAGES.filter(language => scores[language] === score);
  let language = score > 0 && winners.length === 1 ? winners[0] : null;
  if (explicitHits.size > 1) {
    language = null;
    margin = 0;
  }
  const valid = Boolean(
    language
    && score >= RULES.thresholds.weak_score
    && margin >= RULES.thresholds.weak_margin
  );
  if (!valid) language = null;
  const explicit = Boolean(language && explicitHits.size === 1 && explicitHits.has(language));
  const strong = Boolean(
    language
    && (
      explicit
      || (
        score >= RULES.thresholds.strong_score
        && margin >= RULES.thresholds.strong_margin
        && decisive[language]
      )
    )
  );
  return Object.freeze({ language, strong, explicit, score, margin });
}

export function detectSupportedLanguage(text) {
  return detectLanguageEvidence(text).language;
}

export function detectLanguage(text) {
  return detectSupportedLanguage(text);
}
