const LANG_PATTERNS = {
  es: /\b(hola|gracias|por\s*favor|buenos\s*días|quiero|necesito|ayuda|habla|precio|precios|tarifa|tarifas|reserva|reservas|foto|fotos|vídeo|vídeos|video|videos|buenas|amigo|claro|vale|dale|listo|entiendo|puedes|hacer|dónde|cuándo|cómo|cuál|quién|eso|esto|algo|nada|todo|más|menos|está|estoy|estamos|están|tengo|tiene|tenemos|soy|eres|somos|son)\b/gi,
  en: /\b(hello|hi|thanks|thank\s*you|please|help|want|need|can\s*i|price|prices|rate|rates|book|booking|photo|photos|video|videos|yes|sure|fine|good|great|hey|would|could|should|where|when|how|what|who|that|this|there|here|is|are|am|have|has|do|does|did|will|may|might)\b/gi,
  fr: /\b(bonjour|merci|s'il\s*vous\s*plaît|aide|besoin|vouloir|prix|tarif|tarifs|réservation|réserver|photo|photos|vidéo|vidéos|oui|d'accord|bien|tres|peux|peut|où|quand|comment|quoi|qui|que|est|suis|sommes|êtes|sont|ai|as|a|avons|avez|ont|je|tu|il|elle|nous|vous|ils|elles|ce|cet|cette|ces|mon|ton|son|ma|ta|sa)\b/gi,
};

const LANG_MARKERS = {
  es: /\b(español|castellano|hablo español|hablo espanol)\b/i,
  en: /\b(english|speak english)\b/i,
  fr: /\b(français|francais|parle français|parle francais)\b/i,
};

const AMBIGUOUS = new Set(['ok', 'no', 'si', 'hey']);

export function detectSupportedLanguage(text) {
  if (typeof text !== 'string') return null;
  const scores = { es: 0, en: 0, fr: 0 };
  for (const [language, pattern] of Object.entries(LANG_PATTERNS)) {
    const matches = text.match(pattern) || [];
    for (const match of matches) {
      if (!AMBIGUOUS.has(match.toLowerCase())) scores[language] += 1;
    }
  }
  for (const [language, marker] of Object.entries(LANG_MARKERS)) {
    if (marker.test(text)) scores[language] += 20;
  }

  const topScore = Math.max(...Object.values(scores));
  if (topScore < 1) return null;
  const winners = Object.entries(scores)
    .filter(([, score]) => score === topScore)
    .map(([language]) => language);
  return winners.length === 1 ? winners[0] : null;
}
