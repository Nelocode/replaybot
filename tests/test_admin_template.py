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
        self.assertIn("clave administrativa", template)
        self.assertIn("latest.whatsapp.reauth_required", template)
        self.assertIn("necesitas un QR nuevo", template)

    def test_operator_key_bootstraps_both_linked_and_unlinked_telegram(self):
        template = app_module.TEMPLATE

        linked_state = re.search(
            r'if \(tg\.linked\) \{(?P<linked>.*?)\n  \} else \{(?P<unlinked>.*?)'
            r'\n  \}\n\n  const wa',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(linked_state)
        self.assertIn(
            'tgInitial.style.display = "none";',
            linked_state.group("linked"),
        )
        self.assertIn(
            'tgLinked.style.display = data.can_manage ? "block" : "none";',
            linked_state.group("linked"),
        )
        self.assertIn(
            'adminAccess.style.display = data.can_manage ? "none" : "block";',
            template,
        )
        self.assertIn(
            'tgCredentialsHelp.style.display = "none";',
            linked_state.group("linked"),
        )
        self.assertIn(
            'tgCredentialsHelp.style.display = data.can_manage ? "block" : "none";',
            linked_state.group("unlinked"),
        )
        self.assertIn(
            'tgInitial.style.display = data.can_manage ? "block" : "none";',
            linked_state.group("unlinked"),
        )
        self.assertNotIn(
            'adminAccess.style.display = "none";',
            linked_state.group("unlinked"),
        )
        operator_gate = template.index(
            'adminAccess.style.display = data.can_manage ? "none" : "block";'
        )
        telegram_branch = template.index("if (tg.linked) {", operator_gate)
        self.assertLess(operator_gate, telegram_branch)

        recovery_guide = re.search(
            r'function guideChannelAdminRecovery\(\) \{(?P<body>.*?)'
            r'\n\}\n\nfunction setAdminAccessStatus',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(recovery_guide)
        self.assertIn('document.getElementById("admin-operator-key").focus()', recovery_guide.group("body"))
        self.assertNotIn("tg-api-hash", recovery_guide.group("body"))
        self.assertNotIn("tg-phone", recovery_guide.group("body"))

    def test_operator_admin_key_flow_is_nontechnical_and_rotates_csrf(self):
        template = app_module.TEMPLATE
        self.assertIn('id="admin-operator-key" type="password"', template)
        self.assertIn('id="admin-operator-key" type="password" autocomplete="off"', template)
        operator_input = re.search(r'<input id="admin-operator-key"(?P<attrs>[^>]*)>', template)
        self.assertIsNotNone(operator_input)
        self.assertNotIn('name=', operator_input.group("attrs"))
        self.assertIn("Clave administrativa", template)
        self.assertIn("no necesitas los datos ni el acceso a su Telegram", template)
        self.assertIn('onclick="recoverOperatorAdminAccess()"', template)

        recover_flow = re.search(
            r'async function recoverOperatorAdminAccess\(\) \{(?P<body>.*?)'
            r'\n\}\n\nfunction revealWaQrCard',
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(recover_flow)
        body = recover_flow.group("body")
        self.assertIn('fetch("/api/admin_access/operator"', body)
        self.assertIn("headers: channelHeaders()", body)
        self.assertIn("body: JSON.stringify({key})", body)
        self.assertIn("if (data.csrf) channelCsrf = data.csrf;", body)
        self.assertIn("await loadChannelState(true);", body)
        self.assertIn("operator_recovery_unconfigured", body)
        self.assertIn("invalid_operator_key", body)
        self.assertIn("rate_limited", body)
        self.assertIn("data.retry_after", body)

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
