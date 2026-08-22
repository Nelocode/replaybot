# Bot AutoReply Madrid

## Adaptación de idioma

El bot evalúa localmente evidencia Unicode para español, inglés y francés. El
idioma sugerido por el prefijo de WhatsApp —o español en Telegram cuando aún no
hay texto— es sólo provisional. Una indicación expresa o evidencia fuerte puede
corregirlo inmediatamente; una señal débil necesita dos mensajes textuales
consecutivos que coincidan. El detector es conservador y no pretende acertar en
todos los textos breves o mezclados.

El estado persistente guarda únicamente el idioma, su origen y, cuando aplica,
un candidato con su contador. No guarda el texto, los tokens ni las reglas que
coincidieron.

## Recuperación privada de WhatsApp

La recuperación automática es opt-in. Configura en el entorno del panel:

- `WA_RELINK_ENABLED=1`
- `WA_RELINK_PUBLIC_BASE_URL=https://panel.ejemplo.com`
- `WA_RELINK_TELEGRAM_CHAT_ID=<destino privado explícito>`
- `WA_RELINK_TELEGRAM_BOT_TOKEN=<token dedicado>`
- `WA_RELINK_SERVICE_NAME=Bot Madrid`

Si falta el token dedicado, el panel usa `AUTOREPLY_BOT_TOKEN` como fallback
documentado. Se recomienda el dedicado; éste se elimina del entorno de todos
los workers hijos. El destino siempre debe configurarse explícitamente.

Ante `logged_out` o `session_invalid`, el supervisor crea un único incidente y
envía una URL HTTPS privada. El token viaja en el fragmento, se consume una vez
y sólo se guarda su hash. Abrir la URL no genera nada: el QR aparece únicamente
después de pulsar **Generar QR** y el cliente todavía debe escanearlo en
WhatsApp. El enlace dura 15 minutos y el candidato QR, 180 segundos. No se crea
otro candidato mientras el actual siga vigente o tenga estado incierto. La
cuenta anterior se conserva hasta verificar la candidata y el aviso final sólo
se envía cuando el worker principal reporta una conexión `open` real.
