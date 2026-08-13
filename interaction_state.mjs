import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const VALID_LANGUAGES = new Set(['es', 'en', 'fr']);
const VALID_KINDS = new Set(['call', 'content']);

function fingerprint(namespace, value) {
  return crypto.createHash('sha256').update(`${namespace}\0${String(value)}`, 'utf8').digest('hex');
}

export class PersistentInteractionState {
  constructor({
    filePath,
    defaultLanguage = 'es',
    maxRecentEvents = 256,
    now = () => Date.now(),
    logger = console,
  }) {
    if (!filePath) throw new TypeError('filePath is required');
    if (!VALID_LANGUAGES.has(defaultLanguage)) {
      throw new TypeError('defaultLanguage must be es, en, or fr');
    }
    if (!Number.isInteger(maxRecentEvents) || maxRecentEvents < 1) {
      throw new TypeError('maxRecentEvents must be a positive integer');
    }

    this.filePath = filePath;
    this.defaultLanguage = defaultLanguage;
    this.maxRecentEvents = maxRecentEvents;
    this.now = now;
    this.logger = logger;
    this.contacts = {};
    this.aliases = {};
    this.load();
  }

  load() {
    if (!fs.existsSync(this.filePath)) return;
    try {
      const parsed = JSON.parse(fs.readFileSync(this.filePath, 'utf8'));
      if (!parsed.contacts || typeof parsed.contacts !== 'object' || Array.isArray(parsed.contacts)) {
        throw new TypeError('contacts is not an object');
      }

      for (const [contactKey, raw] of Object.entries(parsed.contacts)) {
        if (!raw || typeof raw !== 'object' || ![0, 1, 2].includes(raw.phase)) continue;
        this.contacts[contactKey] = {
          phase: raw.phase,
          language: VALID_LANGUAGES.has(raw.language) ? raw.language : null,
          language_provisional: VALID_LANGUAGES.has(raw.language)
            && raw.language_provisional === true,
          recent_events: Array.isArray(raw.recent_events)
            ? raw.recent_events.filter(item => typeof item === 'string').slice(-this.maxRecentEvents)
            : [],
          updated_at: Number.isFinite(raw.updated_at) ? raw.updated_at : 0,
          ...(raw.reset_pending === true && raw.phase === 0 ? { reset_pending: true } : {}),
        };
      }
      if (parsed.aliases && typeof parsed.aliases === 'object' && !Array.isArray(parsed.aliases)) {
        for (const [aliasKey, contactKey] of Object.entries(parsed.aliases)) {
          if (typeof contactKey === 'string' && this.contacts[contactKey]) {
            this.aliases[aliasKey] = contactKey;
          }
        }
      }
    } catch {
      this.logger.error?.('[STATE] Interaction state could not be loaded; starting empty');
      this.contacts = {};
      this.aliases = {};
    }
  }

  save() {
    try {
      fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
      const temporaryPath = `${this.filePath}.tmp`;
      fs.writeFileSync(
        temporaryPath,
        JSON.stringify({ version: 2, contacts: this.contacts, aliases: this.aliases }),
        'utf8',
      );
      fs.renameSync(temporaryPath, this.filePath);
      return true;
    } catch {
      this.logger.error?.('[STATE] Interaction state could not be persisted');
      return false;
    }
  }

  register({
    contactId,
    contactAliases = [],
    eventId,
    kind,
    detectedLanguage = null,
    provisionalLanguage = null,
  }) {
    if (contactId === undefined || contactId === null || contactId === '') {
      throw new TypeError('contactId is required');
    }
    if (eventId === undefined || eventId === null || eventId === '') {
      throw new TypeError('eventId is required');
    }
    if (!VALID_KINDS.has(kind)) throw new TypeError('kind must be call or content');
    if (!VALID_LANGUAGES.has(detectedLanguage)) detectedLanguage = null;
    if (!VALID_LANGUAGES.has(provisionalLanguage)) provisionalLanguage = null;

    const identityValues = [contactId, ...(Array.isArray(contactAliases) ? contactAliases : [])]
      .filter(value => value !== undefined && value !== null && value !== '');
    const identityKeys = [...new Set(identityValues.map(value => fingerprint('contact', value)))];
    const primaryKey = identityKeys[0];
    const eventKey = fingerprint('event', eventId);
    // Keep both sides while identities converge.  An older alias may already
    // point at a LID history while the direct PN key holds a pending reset;
    // choosing only `alias || key` would silently hide that reset state.
    const resolvedKeys = [...new Set(identityKeys.flatMap(key => {
      const aliasTarget = this.aliases[key];
      return typeof aliasTarget === 'string' ? [aliasTarget, key] : [key];
    }))];
    const existingKeys = resolvedKeys.filter(key => this.contacts[key]);
    const pendingResetKey = existingKeys.find(
      key => this.contacts[key]?.reset_pending === true,
    );
    const canonicalKey = pendingResetKey || existingKeys[0] || primaryKey;
    const state = this.contacts[canonicalKey] || {
      phase: 0,
      language: null,
      language_provisional: false,
      recent_events: [],
      updated_at: 0,
    };
    const mergeKeys = existingKeys.filter(key => key !== canonicalKey);
    const mergeStates = mergeKeys.map(key => this.contacts[key]);

    // A number-specific reset may be prepared before WhatsApp has revealed
    // that the PN belongs to an existing LID-only history.  In that case the
    // bounded marker wins this first merge/inbound exactly once.  Otherwise,
    // preserve the normal highest-phase merge semantics.
    const pendingReset = pendingResetKey ? this.contacts[pendingResetKey] : null;
    if (pendingReset) {
      state.phase = 0;
      state.language = pendingReset.language;
      state.language_provisional = pendingReset.language_provisional === true;
      state.updated_at = pendingReset.updated_at;
      state.recent_events = [];
      state.reset_pending = true;
    } else {
      for (const candidate of mergeStates) {
        state.phase = Math.max(state.phase, candidate.phase);
        if (
          candidate.language
          && (
            !state.language
            || (state.language_provisional === true && candidate.language_provisional !== true)
          )
        ) {
          state.language = candidate.language;
          state.language_provisional = candidate.language_provisional === true;
        }
        state.updated_at = Math.max(state.updated_at, candidate.updated_at);
        state.recent_events = [...new Set([
          ...state.recent_events,
          ...candidate.recent_events,
        ])].slice(-this.maxRecentEvents);
      }
    }
    for (const oldKey of mergeKeys) delete this.contacts[oldKey];
    for (const [aliasKey, targetKey] of Object.entries(this.aliases)) {
      if (existingKeys.includes(targetKey)) this.aliases[aliasKey] = canonicalKey;
    }
    for (const identityKey of identityKeys) this.aliases[identityKey] = canonicalKey;
    this.contacts[canonicalKey] = state;

    // Consume before classifying the inbound event.  The cleared event window
    // guarantees that this interaction cannot be mistaken for an old event,
    // and the marker cannot trigger a perpetual sequence of Step 1 replies.
    if (state.reset_pending === true) delete state.reset_pending;

    if (state.recent_events.includes(eventKey)) {
      const persisted = this.save();
      return {
        duplicate: true,
        phase: state.phase,
        responseKey: null,
        language: state.language || this.defaultLanguage,
        contactKey: canonicalKey,
        persisted,
      };
    }

    if (detectedLanguage && (!state.language || state.language_provisional === true)) {
      state.language = detectedLanguage;
      state.language_provisional = false;
    } else if (!state.language && provisionalLanguage) {
      state.language = provisionalLanguage;
      state.language_provisional = true;
    }
    const language = state.language || this.defaultLanguage;

    let responseKey;
    if (kind === 'call') {
      responseKey = 'call';
      state.phase = state.phase === 0 ? 1 : 2;
    } else if (state.phase === 0) {
      responseKey = 'step1';
      state.phase = 1;
    } else {
      responseKey = 'step2';
      state.phase = 2;
    }

    state.recent_events.push(eventKey);
    if (state.recent_events.length > this.maxRecentEvents) {
      state.recent_events.splice(0, state.recent_events.length - this.maxRecentEvents);
    }
    state.updated_at = this.now();
    const persisted = this.save();

    return {
      duplicate: false,
      phase: state.phase,
      responseKey,
      language,
      contactKey: canonicalKey,
      persisted,
    };
  }
}
