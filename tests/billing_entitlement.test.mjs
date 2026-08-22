import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { createEntitlementGate, verifyEntitlementToken } from '../billing_entitlement.mjs';

function token(privateKey, { now, audience = 'barcelona', allowed = true } = {}) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const header = encode({ alg: 'EdDSA', kid: 'test-key', typ: 'ENTITLEMENT' });
  const claims = {
    iss: 'chicas-lindas-billing-control-plane',
    aud: audience,
    account_id: 'chicas-lindas',
    plan_code: 'six-bots-eur-250-monthly',
    status: allowed ? 'active' : 'suspended',
    service_allowed: allowed,
    paid_through: null,
    grace_until: null,
    override_until: null,
    iat: now,
    exp: now + 6 * 60 * 60,
    jti: 'opaque-test-id',
  };
  const body = encode(claims);
  const signature = crypto.sign(null, Buffer.from(`${header}.${body}`), privateKey);
  return `${header}.${body}.${signature.toString('base64url')}`;
}

function material() {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519');
  const publicPem = publicKey.export({ type: 'spki', format: 'pem' });
  return {
    privateKey,
    keys: new Map([['test-key', publicKey]]),
    envKeys: JSON.stringify({ 'test-key': Buffer.from(publicPem).toString('base64') }),
  };
}

test('valida una decisión firmada y la conserva sin guardar datos privados', async () => {
  const { privateKey, envKeys } = material();
  const now = 2_000_000_000;
  const signed = token(privateKey, { now, allowed: true });
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'billing-entitlement-'));
  const gate = createEntitlementGate({
    env: {
      BILLING_CONTROL_PLANE_URL: 'https://billing.invalid',
      BILLING_DEPLOYMENT_ID: 'barcelona',
      BILLING_DEPLOYMENT_TOKEN: 'd'.repeat(48),
      BILLING_ACCOUNT_ID: 'chicas-lindas',
      BILLING_SIGNING_PUBLIC_KEYS_JSON: envKeys,
      BILLING_ENFORCEMENT: 'true',
    },
    dataDir: directory,
    now: () => now * 1000,
    fetchImpl: async () => ({ ok: true, json: async () => ({ token: signed }) }),
  });

  assert.equal(await gate.isAllowed(), true);
  const cache = fs.readFileSync(path.join(directory, 'billing_entitlement_barcelona.json'), 'utf8');
  assert.deepEqual(Object.keys(JSON.parse(cache)), ['token']);
});

test('falla cerrado con enforcement y permanece reversible en modo sombra', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'billing-entitlement-'));
  const enforced = createEntitlementGate({ env: { BILLING_ENFORCEMENT: 'true' }, dataDir: directory });
  const shadow = createEntitlementGate({ env: { BILLING_ENFORCEMENT: 'false' }, dataDir: directory });
  assert.equal(await enforced.isAllowed(), false);
  assert.equal(await shadow.isAllowed(), true);
  assert.equal((await shadow.decision()).observedAllowed, false);
});

test('rechaza audiencia incorrecta y firma manipulada', () => {
  const { privateKey, keys } = material();
  const now = 2_000_000_000;
  const wrongAudience = token(privateKey, { now, audience: 'madrid' });
  assert.throws(() => verifyEntitlementToken(wrongAudience, {
    keys,
    deploymentId: 'barcelona',
    accountId: 'chicas-lindas',
    nowSeconds: now,
  }), /audience/);
  const valid = token(privateKey, { now, audience: 'barcelona' });
  const parts = valid.split('.');
  const changedSignature = Buffer.from(parts[2], 'base64url');
  changedSignature[0] ^= 0x01;
  const changed = `${parts[0]}.${parts[1]}.${changedSignature.toString('base64url')}`;
  assert.throws(() => verifyEntitlementToken(changed, {
    keys,
    deploymentId: 'barcelona',
    accountId: 'chicas-lindas',
    nowSeconds: now,
  }));
});
