import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const SAFE_STATUSES = new Set(['trialing', 'active', 'grace', 'suspended', 'canceled']);
const EXPECTED_ISSUER = 'chicas-lindas-billing-control-plane';

function asBoolean(value, fallback = false) {
  if (value === undefined || value === null || value === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

function decodePart(value) {
  return Buffer.from(value, 'base64url');
}

function readPublicKeys(raw) {
  if (!raw) return new Map();
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new TypeError('BILLING_SIGNING_PUBLIC_KEYS_JSON must be an object');
  }
  const keys = new Map();
  for (const [keyId, encodedPem] of Object.entries(parsed)) {
    if (typeof encodedPem !== 'string') throw new TypeError('Invalid billing public key');
    keys.set(keyId, crypto.createPublicKey(Buffer.from(encodedPem, 'base64').toString('utf8')));
  }
  return keys;
}

export function verifyEntitlementToken(token, {
  keys,
  deploymentId,
  accountId,
  nowSeconds = Date.now() / 1000,
}) {
  const parts = String(token || '').split('.');
  if (parts.length !== 3) throw new TypeError('Invalid token shape');
  const header = JSON.parse(decodePart(parts[0]).toString('utf8'));
  const claims = JSON.parse(decodePart(parts[1]).toString('utf8'));
  if (header.alg !== 'EdDSA' || header.typ !== 'ENTITLEMENT') {
    throw new TypeError('Invalid token header');
  }
  const key = keys.get(String(header.kid || ''));
  if (!key) throw new TypeError('Unknown signing key');
  const valid = crypto.verify(
    null,
    Buffer.from(`${parts[0]}.${parts[1]}`, 'ascii'),
    key,
    decodePart(parts[2]),
  );
  if (!valid) throw new TypeError('Invalid token signature');
  if (claims.iss !== EXPECTED_ISSUER) throw new TypeError('Invalid issuer');
  if (claims.aud !== deploymentId || claims.account_id !== accountId) {
    throw new TypeError('Invalid entitlement audience');
  }
  if (!SAFE_STATUSES.has(claims.status) || typeof claims.service_allowed !== 'boolean') {
    throw new TypeError('Invalid entitlement decision');
  }
  if (!Number.isInteger(claims.iat) || !Number.isInteger(claims.exp)) {
    throw new TypeError('Invalid token time');
  }
  if (claims.iat > nowSeconds + 60 || claims.exp <= nowSeconds - 30 || claims.exp <= claims.iat) {
    throw new TypeError('Expired or future entitlement');
  }
  return claims;
}

export function createEntitlementGate({
  env = process.env,
  dataDir,
  fetchImpl = globalThis.fetch,
  now = () => Date.now(),
  logger = console,
} = {}) {
  const deploymentId = String(env.BILLING_DEPLOYMENT_ID || '').trim();
  const controlPlaneUrl = String(env.BILLING_CONTROL_PLANE_URL || '').replace(/\/$/, '');
  const deploymentToken = String(env.BILLING_DEPLOYMENT_TOKEN || '');
  const accountId = String(env.BILLING_ACCOUNT_ID || 'chicas-lindas');
  const enforcement = asBoolean(env.BILLING_ENFORCEMENT);
  const allowInsecureHttp = asBoolean(env.BILLING_ALLOW_INSECURE_HTTP);
  const refreshMs = Math.max(10_000, Math.min(Number(env.BILLING_REFRESH_SECONDS || 60) * 1000, 300_000));
  const timeoutMs = Math.max(500, Math.min(Number(env.BILLING_TIMEOUT_SECONDS || 3) * 1000, 10_000));
  let keys = new Map();
  try {
    keys = readPublicKeys(env.BILLING_SIGNING_PUBLIC_KEYS_JSON);
  } catch {
    logger.error?.('[BILLING] Public key configuration is invalid');
  }
  const cachePath = path.join(
    dataDir || path.resolve('data'),
    `billing_entitlement_${deploymentId || 'unconfigured'}.json`,
  );
  let claims = null;
  let lastAttempt = 0;
  let lastSuccess = 0;
  let inFlight = null;
  let errorCode = null;

  const configured = () => Boolean(
    controlPlaneUrl && deploymentId && deploymentToken.length >= 32 && keys.size,
  );

  function endpoint() {
    const url = new URL('/v1/entitlement', `${controlPlaneUrl}/`);
    const local = ['localhost', '127.0.0.1', '::1'].includes(url.hostname);
    if (url.protocol !== 'https:' && !(local || allowInsecureHttp)) {
      throw new TypeError('Billing control plane must use HTTPS');
    }
    return url;
  }

  function validate(token, currentMs = now()) {
    return verifyEntitlementToken(token, {
      keys,
      deploymentId,
      accountId,
      nowSeconds: currentMs / 1000,
    });
  }

  function loadCache() {
    try {
      const parsed = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
      claims = validate(parsed.token);
    } catch {
      claims = null;
    }
  }

  function writeCache(token) {
    fs.mkdirSync(path.dirname(cachePath), { recursive: true });
    const temporary = `${cachePath}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({ token }), { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, cachePath);
  }

  async function refresh({ force = false } = {}) {
    const current = now();
    if (!force && current - lastAttempt < refreshMs) return Boolean(claims);
    if (inFlight) return inFlight;
    lastAttempt = current;
    inFlight = (async () => {
      if (!configured()) {
        errorCode = 'not_configured';
        return false;
      }
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetchImpl(endpoint(), {
          headers: {
            Authorization: `Bearer ${deploymentToken}`,
            'X-Deployment-ID': deploymentId,
            Accept: 'application/json',
          },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`billing_http_${response.status}`);
        const body = await response.json();
        const verified = validate(body.token, current);
        writeCache(body.token);
        claims = verified;
        lastSuccess = current;
        errorCode = null;
        return true;
      } catch (error) {
        errorCode = error?.name === 'AbortError' ? 'timeout' : 'refresh_failed';
        try {
          const parsed = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
          claims = validate(parsed.token, current);
        } catch {
          claims = null;
        }
        return Boolean(claims);
      } finally {
        clearTimeout(timeout);
        inFlight = null;
      }
    })();
    return inFlight;
  }

  async function decision() {
    await refresh();
    const currentSeconds = now() / 1000;
    const observedAllowed = Boolean(
      claims && claims.exp > currentSeconds - 30 && claims.service_allowed === true,
    );
    return {
      configured: configured(),
      enforcement,
      observedAllowed,
      serviceAllowed: enforcement ? observedAllowed : true,
      status: claims?.status || 'unavailable',
      paidThrough: claims?.paid_through || null,
      graceUntil: claims?.grace_until || null,
      overrideUntil: claims?.override_until || null,
      tokenExpiresAt: claims?.exp || null,
      lastRefreshAt: lastSuccess ? Math.floor(lastSuccess / 1000) : null,
      errorCode,
    };
  }

  async function isAllowed() {
    return (await decision()).serviceAllowed === true;
  }

  function isAllowedCached() {
    const currentSeconds = now() / 1000;
    const observedAllowed = Boolean(
      claims && claims.exp > currentSeconds - 30 && claims.service_allowed === true,
    );
    return enforcement ? observedAllowed : true;
  }

  loadCache();
  return { isAllowed, isAllowedCached, decision, refresh, configured };
}
