const LANGUAGE_VALUES = ['es', 'en', 'fr'];
export const VALID_LANGUAGES = new Set(LANGUAGE_VALUES);
const VALID_SOURCES = new Set(['detected', 'provisional', 'operator_seed', 'legacy']);
const EVIDENCE_KEYS = ['explicit', 'language', 'margin', 'score', 'strong'];

export function sanitizeLanguageState(state) {
  const raw = state && typeof state === 'object' && !Array.isArray(state) ? state : {};
  const language = VALID_LANGUAGES.has(raw.language) ? raw.language : null;
  const provisional = Boolean(language && raw.language_provisional === true);
  let source = VALID_SOURCES.has(raw.language_source)
    ? raw.language_source
    : (provisional ? 'provisional' : (language ? 'legacy' : null));
  if (provisional) source = 'provisional';
  let candidate = raw.language_candidate;
  let streak = raw.language_candidate_streak;
  const candidateAllowed = Boolean(
    VALID_LANGUAGES.has(language)
    && VALID_LANGUAGES.has(candidate)
    && Number.isInteger(streak)
    && streak === 1
    && (provisional || candidate !== language)
  );
  if (!candidateAllowed) {
    candidate = null;
    streak = 0;
  }
  return {
    language,
    language_provisional: provisional,
    language_source: source,
    language_candidate: candidate,
    language_candidate_streak: streak,
  };
}

function normalisedObservation(detectedLanguage, languageEvidence) {
  if (languageEvidence && typeof languageEvidence === 'object' && !Array.isArray(languageEvidence)) {
    const keys = Object.keys(languageEvidence).sort();
    const language = languageEvidence.language;
    const valid = Boolean(
      keys.join(',') === EVIDENCE_KEYS.join(',')
      && (language === null || VALID_LANGUAGES.has(language))
      && typeof languageEvidence.strong === 'boolean'
      && typeof languageEvidence.explicit === 'boolean'
      && Number.isInteger(languageEvidence.score)
      && languageEvidence.score >= 0
      && Number.isInteger(languageEvidence.margin)
      && languageEvidence.margin >= 0
      && !(language === null && (languageEvidence.strong || languageEvidence.explicit))
      && !(languageEvidence.explicit && !languageEvidence.strong)
    );
    if (!valid) return { language: null, strong: false, observedText: true };
    return {
      language,
      strong: Boolean(language && languageEvidence.strong),
      observedText: true,
    };
  }
  const language = VALID_LANGUAGES.has(detectedLanguage) ? detectedLanguage : null;
  return { language, strong: false, observedText: Boolean(language) };
}

export function reduceLanguageState(state, {
  detectedLanguage = null,
  languageEvidence = null,
  provisionalLanguage = null,
} = {}) {
  const nextState = sanitizeLanguageState(state);
  const provisional = VALID_LANGUAGES.has(provisionalLanguage) ? provisionalLanguage : null;
  const observation = normalisedObservation(detectedLanguage, languageEvidence);
  const current = nextState.language;
  const clearCandidate = () => {
    nextState.language_candidate = null;
    nextState.language_candidate_streak = 0;
  };
  const setDetected = language => {
    nextState.language = language;
    nextState.language_provisional = false;
    nextState.language_source = 'detected';
    clearCandidate();
  };
  const recordCandidate = language => {
    const streak = nextState.language_candidate === language
      ? nextState.language_candidate_streak + 1
      : 1;
    if (streak >= 2) {
      setDetected(language);
      return true;
    }
    nextState.language_candidate = language;
    nextState.language_candidate_streak = streak;
    return false;
  };

  if (!VALID_LANGUAGES.has(current)) {
    if (observation.language && observation.strong) {
      setDetected(observation.language);
    } else if (observation.language) {
      nextState.language = observation.language;
      nextState.language_provisional = true;
      nextState.language_source = 'provisional';
      nextState.language_candidate = observation.language;
      nextState.language_candidate_streak = 1;
    } else if (provisional) {
      nextState.language = provisional;
      nextState.language_provisional = true;
      nextState.language_source = 'provisional';
      clearCandidate();
    }
    return nextState;
  }

  if (!observation.observedText) return nextState;
  if (!observation.language) {
    clearCandidate();
    return nextState;
  }
  if (observation.strong) {
    setDetected(observation.language);
    return nextState;
  }
  if (nextState.language_provisional && observation.language !== current) {
    // A phone/default hint or one weak observation must not outweigh the
    // client's next identifiable text. One weak signal is still provisional.
    nextState.language = observation.language;
    nextState.language_source = 'provisional';
    // Preserve a concordant candidate saved under the previous policy.
    recordCandidate(observation.language);
    return nextState;
  }
  if (observation.language === current) {
    if (nextState.language_provisional) recordCandidate(observation.language);
    else clearCandidate();
    return nextState;
  }
  recordCandidate(observation.language);
  return nextState;
}
