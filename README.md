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
