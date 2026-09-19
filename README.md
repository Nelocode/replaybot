# Bot AutoReply Madrid

## Adaptación de idioma

El bot evalúa localmente evidencia Unicode para español, inglés y francés. El
idioma sugerido por el prefijo de WhatsApp —o español en Telegram cuando aún no
hay texto— es sólo provisional. El primer texto identificable lo corrige de
inmediato, incluso si es breve, como `Hi` o `How much?`. Una señal débil mantiene
el nuevo idioma provisional hasta confirmarlo. Si el idioma anterior ya estaba
confirmado, era heredado o lo había indicado el operador, cambiarlo sigue
requiriendo evidencia fuerte, una petición expresa o dos mensajes débiles
consecutivos. El vocabulario cubre consultas como `Are you free tonight?` y
`Looking for a girl tonight`. Los textos ambiguos no fuerzan un cambio. El
detector es conservador y no pretende acertar en todos los textos breves o mezclados.

El estado persistente guarda únicamente el idioma, su origen y, cuando aplica,
un candidato con su contador. No guarda el texto, los tokens ni las reglas que
coincidieron.
