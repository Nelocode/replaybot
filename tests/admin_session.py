import time
from unittest.mock import patch

import app as app_module


TEST_OPERATOR_KEY = "test-panel-operator-key-" + ("K" * 32)


def install_operator_key(test_case, key=TEST_OPERATOR_KEY):
    """Configure the operator key for a test without depending on process env."""

    patcher = patch.object(
        app_module,
        "_configured_operator_recovery_key",
        return_value=key,
    )
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return key


def grant_operator_admin(client, key=TEST_OPERATOR_KEY, *, csrf=None):
    """Create the same key-bound, expiring admin session as the live route."""

    with client.session_transaction() as browser_session:
        browser_session["channel_admin"] = True
        browser_session["operator_recovery_key_version"] = (
            app_module._operator_recovery_key_version(key)
        )
        browser_session["operator_recovery_verified_at"] = time.time()
        if csrf is not None:
            browser_session["channel_csrf"] = csrf
        browser_session.permanent = True
