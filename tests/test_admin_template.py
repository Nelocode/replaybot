import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import app as app_module


class AdminTemplateTestCase(unittest.TestCase):
    def test_panel_document_is_never_cached(self):
        with app_module.app.test_client() as client:
            response = client.get("/")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "no-store, no-cache, must-revalidate, max-age=0",
            response.headers["Cache-Control"],
        )
        self.assertEqual("no-cache", response.headers["Pragma"])
        self.assertEqual("0", response.headers["Expires"])

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

    def test_telegram_audio_labels_are_editable_without_restarting_workers(self):
        template = app_module.TEMPLATE
        self.assertIn('id="tg-audio-performer"', template)
        self.assertIn('id="tg-audio-title"', template)
        self.assertIn('placeholder="Caché Madrid"', template)
        self.assertIn('fetch("/api/telegram_audio_branding"', template)
        self.assertIn("JSON.stringify({title, performer})", template)
        self.assertIn("let tgAudioBrandingDirty = false", template)
        self.assertIn("if (tgAudioBrandingDirty)", template)
        self.assertIn("headers: channelHeaders()", template)

    def test_test_mode_can_reset_the_latest_conversation_per_channel(self):
        template = app_module.TEMPLATE
        self.assertIn('id="test-mode-card"', template)
        self.assertIn('fetch("/api/test_mode"', template)
        self.assertIn('fetch("/api/test_mode/reset"', template)
        self.assertIn("resetTestConversation('telegram')", template)
        self.assertIn("resetTestConversation('whatsapp')", template)
        self.assertIn("resetTestConversation('both')", template)
        self.assertIn('id="test-mode-language"', template)
        self.assertIn("body: JSON.stringify({channel, language, confirm: true})", template)
        self.assertIn("Úsalo sin tráfico real simultáneo", template)

    def test_whatsapp_state_refresh_and_recovery_are_actionable(self):
        template = app_module.TEMPLATE
        self.assertIn('fetch("/api/data", {cache: "no-store"})', template)
        self.assertIn('fetch("/api/channels", {cache: "no-store"})', template)
        self.assertIn("let channelStateRequest = null;", template)
        self.assertIn("setInterval(loadChannelState, 10000);", template)
        self.assertIn('document.addEventListener("visibilitychange"', template)
        self.assertIn("function guideChannelAdminRecovery()", template)
        self.assertIn("🔐 Administrar WA", template)
        self.assertIn("🔐 Recuperar acceso", template)
        self.assertIn("credenciales actuales de Telegram", template)
        self.assertIn("latest.whatsapp.reauth_required", template)
        self.assertIn("necesitas un QR nuevo", template)

    def test_linked_telegram_uses_saved_credentials_for_legacy_recovery(self):
        template = app_module.TEMPLATE

        linked_state = re.search(
            r'if \(tg\.linked\) \{(?P<linked>.*?)\n  \} else \{(?P<unlinked>.*?)'
            r'\n  \}\n\n  const wa',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(linked_state)
        self.assertIn(
            'tgInitial.style.display = data.can_manage ? "none" : "block";',
            linked_state.group("linked"),
        )
        self.assertIn(
            'tgLinked.style.display = data.can_manage ? "block" : "none";',
            linked_state.group("linked"),
        )
        self.assertIn('adminAccess.style.display = "none";', linked_state.group("linked"))
        self.assertIn('tgInitial.style.display = "block";', linked_state.group("unlinked"))
        self.assertIn('adminAccess.style.display = "none";', linked_state.group("unlinked"))

        link_telegram = re.search(
            r'async function linkTelegram\(\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function verifyTgCode',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(link_telegram)
        self.assertIn("already_authorized", link_telegram.group("body"))
        self.assertIn("recoveredAdmin", link_telegram.group("body"))
        self.assertNotIn("admin_verification_required", link_telegram.group("body"))

    def test_backend_recovery_challenge_renders_otp_without_second_request(self):
        template = app_module.TEMPLATE
        link_telegram = re.search(
            r'async function linkTelegram\(\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function verifyTgCode',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(link_telegram)
        body = link_telegram.group("body")
        challenge = re.search(
            r"if \(d\.recovery_via_telegram\) \{(?P<challenge>.*?)"
            r"\n    \} else if \(d\.needs_code\)",
            body,
            re.DOTALL,
        )
        self.assertIsNotNone(challenge)
        challenge_body = challenge.group("challenge")
        self.assertIn('adminPanel.style.display = "block";', challenge_body)
        self.assertIn("renderAdminAccessState(d);", challenge_body)
        self.assertIn('document.getElementById("admin-access-code").focus();', challenge_body)
        self.assertNotIn("requestAdminAccess(", challenge_body)
        self.assertLess(
            body.index("if (d.recovery_via_telegram)"),
            body.index("else if (d.ok)"),
        )

    def test_credential_mismatch_falls_back_to_linked_telegram_otp(self):
        template = app_module.TEMPLATE
        link_telegram = re.search(
            r'async function linkTelegram\(\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function verifyTgCode',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(link_telegram)
        body = link_telegram.group("body")
        self.assertIn(
            '["already_linked", "credential_mismatch"].includes(d.error_code)',
            body,
        )
        self.assertIn("openAdminAccessVerification();", body)
        self.assertIn("await loadAdminAccessStatus();", body)
        self.assertIn("await requestAdminAccess();", body)
        self.assertLess(
            body.index("openAdminAccessVerification();"),
            body.index("await requestAdminAccess();"),
        )

    def test_admin_access_flow_uses_csrf_and_refreshes_rotated_token(self):
        template = app_module.TEMPLATE
        for endpoint in (
            "/api/admin_access/request",
            "/api/admin_access/status",
            "/api/admin_access/verify",
            "/api/admin_access/cancel",
        ):
            self.assertIn(endpoint, template)

        request_flow = re.search(
            r'async function requestAdminAccess\(\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function verifyAdminAccess',
            template,
            re.DOTALL,
        )
        verify_flow = re.search(
            r'async function verifyAdminAccess\(\) \{(?P<body>.*?)'
            r'\n\}\n\nasync function cancelAdminAccess',
            template,
            re.DOTALL,
        )
        cancel_flow = re.search(
            r'async function cancelAdminAccess\(\) \{(?P<body>.*?)'
            r'\n\}\n\nfunction revealWaQrCard',
            template,
            re.DOTALL,
        )
        for flow in (request_flow, verify_flow, cancel_flow):
            self.assertIsNotNone(flow)
            self.assertIn("headers: channelHeaders()", flow.group("body"))

        self.assertIn(
            'fetch("/api/admin_access/status", {cache: "no-store"})', template
        )
        self.assertIn("if (!/^\\d{8}$/.test(code))", verify_flow.group("body"))
        self.assertIn("channelCsrf = data.csrf || null;", verify_flow.group("body"))
        self.assertIn("await loadChannelState(true);", verify_flow.group("body"))
        self.assertIn("if (latest && latest.csrf) channelCsrf = latest.csrf;", verify_flow.group("body"))
        self.assertIn("expired_code", template)
        self.assertIn("delivery_retrying", template)
        self.assertIn("retry_after", template)

    def test_every_panel_mutation_uses_the_central_csrf_headers(self):
        template = app_module.TEMPLATE
        self.assertNotIn('headers: {"Content-Type": "application/json"}', template)
        self.assertIn("function channelUploadHeaders()", template)
        self.assertIn(
            'fetch("/api/upload_chunk", {\n        method: "POST",\n        headers: channelUploadHeaders()',
            template,
        )
        self.assertNotIn(
            'fetch("/api/start_botfather", {method:"POST"})',
            template,
        )

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

    def test_whatsapp_qr_keeps_its_space_and_only_reloads_new_revisions(self):
        template = app_module.TEMPLATE
        self.assertIn(
            'id="wa-qr-img" alt="QR WhatsApp" '
            'style="visibility:hidden;display:inline-block;width:300px;height:300px;',
            template,
        )
        self.assertIn("let waQrLoadGeneration = 0;", template)
        self.assertIn("function showWaQrImage(revision=null)", template)
        self.assertIn("image.dataset.qrRevision === requestedRevision", template)
        self.assertIn("image.dataset.pendingQrRevision === requestedRevision", template)
        self.assertIn("const nextImage = new Image();", template)
        self.assertIn('image.style.visibility = "visible";', template)
        self.assertIn("showWaQrImage(data.qr_revision);", template)
        self.assertIn("showWaQrImage(d.qr_revision);", template)
        self.assertNotIn(
            'img.src = "/api/switch_wa/qr?ts=" + Date.now();',
            template,
        )


if __name__ == "__main__":
    unittest.main()
