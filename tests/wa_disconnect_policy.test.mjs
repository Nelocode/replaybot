import test from 'node:test';
import assert from 'node:assert/strict';
import { DisconnectReason } from '@whiskeysockets/baileys';
import {
  classifyWhatsAppDisconnect,
  diagnoseWhatsAppDisconnect,
} from '../wa_disconnect_policy.mjs';

test('logged-out credentials stop the dead worker and require a new QR', () => {
  assert.deepEqual(classifyWhatsAppDisconnect(DisconnectReason.loggedOut), {
    reauthRequired: true,
    shouldReconnect: false,
    terminateWorker: true,
    reason: 'logged_out',
  });
});

test('other invalid sessions also stop instead of appearing alive', () => {
  for (const status of [
    DisconnectReason.badSession,
    DisconnectReason.connectionReplaced,
    DisconnectReason.multideviceMismatch,
    DisconnectReason.forbidden,
  ]) {
    const decision = classifyWhatsAppDisconnect(status);
    assert.equal(decision.reauthRequired, true);
    assert.equal(decision.shouldReconnect, false);
    assert.equal(decision.terminateWorker, true);
    assert.equal(decision.reason, 'session_invalid');
  }
});

test('transient transport failures keep the reconnect loop enabled', () => {
  assert.deepEqual(classifyWhatsAppDisconnect(DisconnectReason.connectionClosed), {
    reauthRequired: false,
    shouldReconnect: true,
    terminateWorker: false,
    reason: 'transient',
  });
});

test('extended diagnostics preserve every known Baileys status exactly', () => {
  const cases = [
    [401, 'logged_out', 'authorization', 'relink', true],
    [403, 'forbidden', 'provider_restriction', 'review_provider', true],
    [408, 'connection_lost_or_timeout', 'transport', 'automatic_reconnect', false],
    [411, 'multidevice_mismatch', 'session_mismatch', 'relink_cleanly', true],
    [428, 'connection_closed', 'transport', 'automatic_reconnect', false],
    [429, 'provider_rate_limited', 'provider_restriction', 'retry_backoff', false],
    [440, 'connection_replaced', 'session_conflict', 'check_duplicate_session', true],
    [500, 'bad_session', 'authorization', 'relink_cleanly', true],
    [503, 'service_unavailable', 'provider_availability', 'retry_backoff', false],
    [515, 'restart_required', 'process', 'automatic_reconnect', false],
  ];

  for (const [statusCode, statusName, category, action, terminal] of cases) {
    assert.deepEqual(diagnoseWhatsAppDisconnect(statusCode), {
      statusCode,
      statusName,
      category,
      action,
      terminal,
      reauthRequired: terminal,
      shouldReconnect: !terminal,
    });
  }
});

test('extended diagnostics normalize numeric strings and fail closed to unknown', () => {
  assert.deepEqual(
    diagnoseWhatsAppDisconnect('440'),
    diagnoseWhatsAppDisconnect(DisconnectReason.connectionReplaced),
  );

  assert.deepEqual(diagnoseWhatsAppDisconnect(499), {
    statusCode: 499,
    statusName: 'unknown',
    category: 'unknown',
    action: 'automatic_reconnect',
    terminal: false,
    reauthRequired: false,
    shouldReconnect: true,
  });
  assert.deepEqual(diagnoseWhatsAppDisconnect('not-a-status'), {
    statusCode: null,
    statusName: 'unknown',
    category: 'unknown',
    action: 'automatic_reconnect',
    terminal: false,
    reauthRequired: false,
    shouldReconnect: true,
  });
});
