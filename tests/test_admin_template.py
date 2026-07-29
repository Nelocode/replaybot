import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import app as app_module


class AdminTemplateTestCase(unittest.TestCase):
    def test_every_inline_click_handler_exists(self):
        handlers = set(re.findall(r'onclick="([A-Za-z_$][\w$]*)\(', app_module.TEMPLATE))
        definitions = set(
            re.findall(
                r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                app_module.TEMPLATE,
            )
        )
        self.assertEqual(set(), handlers - definitions)

    @unittest.skipUnless(shutil.which("node"), "Node.js no está disponible")
    def test_embedded_javascript_parses(self):
        match = re.search(r"<script>(.*?)</script>", app_module.TEMPLATE, re.DOTALL)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "admin.js"
            script.write_text(match.group(1), encoding="utf-8")
            result = subprocess.run(
                [shutil.which("node"), "--check", str(script)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_account_switch_ui_uses_transactional_endpoints(self):
        for endpoint in (
            "/api/channels",
            "/api/switch_telegram",
            "/api/switch_telegram/code",
            "/api/switch_telegram/password",
            "/api/switch_telegram/cancel",
            "/api/switch_wa",
            "/api/switch_wa/claim",
            "/api/switch_wa/status",
            "/api/switch_wa/qr",
            "/api/switch_wa/commit",
            "/api/switch_wa/cancel",
        ):
            self.assertIn(endpoint, app_module.TEMPLATE)
        self.assertNotIn('fetch("/api/reset_wa"', app_module.TEMPLATE)

    def test_whatsapp_cancel_invalidates_poll_and_cannot_race_commit(self):
        template = app_module.TEMPLATE
        self.assertIn("let waSwitchGeneration = 0;", template)
        self.assertIn("waSwitchPollAbortController.abort()", template)
        self.assertIn("generation !== waSwitchGeneration", template)
        self.assertIn("setWaSwitchCancelDisabled(true);", template)
        self.assertIn("if (waCommitInFlight)", template)
        self.assertIn("El cambio ya había finalizado o no seguía activo", template)
        self.assertEqual(
            2,
            len(re.findall(r'class="[^"]*\bwa-switch-cancel\b[^"]*"', template)),
        )

    def test_whatsapp_state_refresh_and_recovery_are_actionable(self):
        template = app_module.TEMPLATE
        self.assertIn('fetch("/api/data", {cache: "no-store"})', template)
        self.assertIn('fetch("/api/channels", {cache: "no-store"})', template)
        self.assertIn("let channelStateRequest = null;", template)
        self.assertIn("setInterval(loadChannelState, 10000);", template)
        self.assertIn('document.addEventListener("visibilitychange"', template)
        self.assertIn("function guideChannelAdminRecovery()", template)
        self.assertIn("🔐 Recuperar acceso", template)
        self.assertIn("const recoveredAdmin = Boolean(d.already_authorized);", template)
        self.assertIn("Ya puedes volver a vincular WhatsApp", template)
        self.assertIn("latest.whatsapp.reauth_required", template)
        self.assertIn("necesitas un QR nuevo", template)

    def test_foreign_whatsapp_switch_never_polls_before_it_is_claimed(self):
        template = app_module.TEMPLATE
        self.assertIn('d.owned_by_this_browser === true', template)
        self.assertIn("async function claimWaSwitch()", template)
        self.assertIn('fetch("/api/switch_wa/claim"', template)
        self.assertIn("continuarla de forma segura en este navegador", template)
        self.assertIn("function revealWaQrCard", template)
        self.assertIn('scrollIntoView({behavior: "smooth", block: "start"})', template)

        owned = re.search(
            r'else if \(d\.error_code === "switch_in_progress" && '
            r'd\.owned_by_this_browser === true\) \{(?P<body>.*?)'
            r'\n    \} else if \(d\.error_code === "switch_in_progress"\)',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(owned)
        self.assertIn("revealWaQrCard", owned.group("body"))
        self.assertIn("beginWaSwitchPolling", owned.group("body"))

        foreign = re.search(
            r'else if \(d\.error_code === "switch_in_progress"\) \{(?P<body>.*?)'
            r'\n    \} else \{\n      btn\.disabled = false;',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(foreign)
        self.assertNotIn("revealWaQrCard", foreign.group("body"))
        self.assertNotIn("beginWaSwitchPolling", foreign.group("body"))
        self.assertIn("claimWaSwitch", foreign.group("body"))

        start = re.search(
            r'async function startWaSwitch\(knownState=null\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function claimWaSwitch',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(start)
        admin_guard = re.search(
            r'if \(!latest\.can_manage\) \{(?P<body>.*?)\n  \}',
            start.group("body"),
            re.DOTALL,
        )
        self.assertIsNotNone(admin_guard)
        self.assertIn("guideChannelAdminRecovery();", admin_guard.group("body"))
        self.assertIn("return;", admin_guard.group("body"))


if __name__ == "__main__":
    unittest.main()
