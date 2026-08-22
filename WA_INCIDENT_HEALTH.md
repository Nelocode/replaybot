# Salud e historial de incidentes de WhatsApp

## Objetivo

Esta capa observa el worker de WhatsApp, estima si aumenta el riesgo de una falla y conserva evidencia estructurada para el análisis posterior. No sustituye los controles de entrega, la supervisión del proveedor ni una prueba real de mensaje.

La estimación es preventiva, no una promesa de predicción. Algunos cierres, como un logout manual o una restricción repentina del proveedor, pueden ocurrir sin señales previas observables.

## Qué estima

El resumen expone dos resultados derivados:

- `condition`: `starting`, `healthy`, `warning`, `degraded`, `critical` o `unknown`.
- `failure_likelihood`: `low`, `elevated`, `high` o `unknown`.

Las señales permitidas incluyen inestabilidad de conexión, reconexiones frecuentes, timeout, auth no registrada o no escribible, `403`, `429`, fallas de entrega, límites locales, colas saturadas, circuit breaker, pausa existente, arranque fallido, conflicto de lease y problemas de integridad del ledger.

Los modos de falla posibles también son enumerados: pérdida de autenticación, suspensión de entrega, sesión duplicada, protección local, restricción del proveedor, rate limit, desajuste multidispositivo, inestabilidad de transporte o causa desconocida.

El heartbeat prueba que el proceso actualiza telemetría; por sí solo no prueba que WhatsApp reciba y responda mensajes.

## Causas exactas permitidas

El sistema persiste el código numérico estructurado recibido y un `status_name` de lista cerrada. No guarda el error crudo.

| Código | `status_name` | Interpretación operativa | Terminal |
|---:|---|---|:---:|
| 401 | `logged_out` | La cuenta cerró o perdió la sesión vinculada | Sí |
| 403 | `forbidden` | El proveedor rechazó la sesión | Sí |
| 408 | `connection_lost_or_timeout` | Pérdida de conexión o timeout | No |
| 411 | `multidevice_mismatch` | Desajuste multidispositivo | Sí |
| 428 | `connection_closed` | Transporte cerrado | No |
| 429 | `provider_rate_limited` | Límite del proveedor | No |
| 440 | `connection_replaced` | Otra conexión reemplazó esta sesión | Sí |
| 500 | `bad_session` | Sesión dañada o inválida | Sí |
| 503 | `service_unavailable` | Servicio temporalmente no disponible | No |
| 515 | `restart_required` | Baileys solicita reconstruir la conexión | No |
| otro o ausente | `unknown` | No existe una clasificación segura | Según la política vigente |

Baileys asigna el mismo código `408` a `connectionLost` y `timedOut`. Por eso el ledger conserva deliberadamente `connection_lost_or_timeout`: afirmar una de las dos causas como exacta sería inventar evidencia.

También existen causas operativas sin código de proveedor: `auth_unregistered`, `auth_write_failure`, `connection_timeout`, `delivery_failure`, `delivery_timeout`, `local_rate_limit`, `provider_forbidden`, `queue_full`, `queue_timeout`, `slow_connect`, `startup_failure`, `worker_lease_conflict` y `worker_shutdown`.

Un código identifica el cierre observado, no necesariamente la causa comercial, humana o de política interna que llevó al proveedor a emitirlo. Esa causa raíz puede requerir evidencia adicional.

## Archivos persistidos

Por defecto se usan estos archivos dentro del volumen de datos:

- `/app/data/wa_incident_health.json`: snapshot derivado y reemplazable para el panel.
- `/app/data/wa_incidents.jsonl`: ledger forense append-only.
- `/app/data/wa_incidents.jsonl.writer`: lease del único escritor autorizado.
- `/app/data/wa_incidents.jsonl.conflict`: marcador de conflicto reciente entre workers.

`WA_INCIDENT_HEALTH_FILE` y `WA_INCIDENT_LEDGER_FILE` permiten cambiar las dos rutas principales. Los archivos deben permanecer en almacenamiento privado, escribible por el usuario del contenedor y excluido de Git.

Antes de desplegar es obligatorio confirmar en Easypanel que `/app/data` es un volumen persistente. La existencia de esa ruta dentro de la imagen no demuestra persistencia. En staging debe comprobarse que un archivo controlado sobrevive a la recreación del contenedor antes de considerar confiable el post mortem.

No se deben borrar, mover ni truncar estos archivos durante un incidente. Una recreación sin volumen persistente perdería tanto el snapshot como el historial.

## Endpoint de resumen e historial

- `GET /api/wa_incident_health` devuelve el resumen allowlisted. Sin sesión administrativa oculta `active_incident`, `last_incident` y `last_failure`; con sesión administrativa incluye esos detalles.
- `GET /api/wa_incidents?limit=20` devuelve historial sólo a una sesión autorizada por `_can_manage_channels()`. Sin autorización responde `403`. El límite aceptado es de 1 a 100 registros.

Ambas respuestas usan `Cache-Control: no-store, private`, `X-Content-Type-Options: nosniff` y `Referrer-Policy: no-referrer`.

El historial sólo se entrega si la cadena completa verifica. Ante integridad `invalid` o `unavailable`, se devuelve una lista vacía en vez de exponer evidencia parcialmente confiable.

## Privacidad

El esquema permite únicamente enums, códigos numéricos, timestamps, contadores, UUID aleatorios, secuencia y hashes. Está prohibido persistir o exponer:

- teléfonos, JIDs o identificadores de cuenta;
- IDs de mensajes o llamadas;
- texto, audio, captions o payloads;
- QR, credenciales, claves, sesiones o tokens;
- objetos `Error` o `Boom`, mensajes de error, stack traces o rutas sensibles;
- notas de operador en texto libre.

Los callbacks diagnósticos sólo reciben `type` y, cuando aplica, `statusCode` y `attempt`. Un error del callback queda aislado y no cambia el comportamiento de entrega.

## Escritor único

Antes de anexar, el worker adquiere de forma exclusiva el archivo `.writer`. El lease contiene sólo la revisión aleatoria del worker y su heartbeat. Un lease ajeno con menos de 90 segundos se considera activo:

- el segundo worker no escribe el ledger;
- se crea el marcador `.conflict`;
- se publica `worker_lease_conflict` en la salud derivada.

Un lease vencido puede recuperarse. No se debe borrar manualmente `.writer` mientras exista un worker vivo: hacerlo puede habilitar escritores concurrentes y destruir la secuencia forense.

Este lock protege procesos que comparten el mismo volumen. No puede detectar preventivamente otro despliegue que use una copia independiente de las mismas credenciales y otro volumen.

## Integridad del ledger

Cada línea contiene `sequence`, `previous_hash` y `record_hash`. Antes de escribir, el worker audita la cadena completa; la escritura usa append, un solo write y `fsync` cuando el runtime lo ofrece. El snapshot conserva localmente el último `sequence` y `head`, y tanto el worker como el lector administrativo los usan para detectar también un rollback válido de la cola mientras el snapshot sobreviva. El lector vuelve a verificar esquema, claves exactas, secuencia y SHA-256.

Esto hace el archivo tamper-evident frente a alteraciones internas, reordenamiento o eliminación de registros intermedios. No lo vuelve inmutable ni constituye no repudio.

Limitación importante: no existe un anchor externo del último hash. El anchor local detecta que se trunque sólo el ledger, pero un actor con acceso suficiente al mismo almacenamiento podría modificar a la vez ledger y snapshot o reemplazarlos por otra cadena coherente. El `head` debe copiarse periódicamente a un sistema externo confiable si se necesita detectar ese escenario.

El ledger no tiene rotación automática. Se debe vigilar el uso de disco y diseñar una rotación sellada antes de alcanzar límites del volumen; nunca rotarlo durante un incidente ni romper la cadena sin conservar el segmento y su hash final.

## Automatización y acciones

La capa de salud e historial es observacional. No ejecuta por sí misma:

- reinicios;
- revinculación o generación de QR;
- borrado o reemplazo de `wa_auth`;
- cambios de pausa del operador;
- despliegues o rollback.

Los comportamientos preexistentes del worker siguen vigentes: la política de conexión puede programar una reconexión transitoria o terminar ante sesión inválida, y la seguridad de entrega puede activar circuito, backoff o pausa frente a señales como `403`. El monitor sólo registra y explica esos hechos.

Toda recuperación terminal requiere decisión humana autorizada. `critical`, `high` o un heartbeat fresco no autorizan acciones remotas.

## Procedimiento post mortem

1. Registrar hora, ciudad, condición del panel y si el worker aparece vivo. No reiniciar ni revincular todavía.
2. Consultar `GET /api/wa_incident_health` con sesión administrativa y conservar `condition`, `failure_likelihood`, `signals`, `ledger_integrity`, `ledger_sequence`, incidente activo y última falla.
3. Consultar `GET /api/wa_incidents?limit=100`. Exigir `integrity: verified`; si no verifica, preservar los archivos y tratar el contenido como evidencia no confiable.
4. Hacer una copia privada de `wa_incidents.jsonl`, `.writer`, `.conflict` y `wa_incident_health.json` antes de cualquier recuperación. Calcular el SHA-256 de las copias. No copiar `wa_auth`, QR ni secretos al informe.
5. Localizar el último `connection_closed`, su código, `status_name`, acción, timestamps, incident ID y eventos precursores. Separar hechos observados de inferencias.
6. Interpretar la causa:
   - `440`: buscar otro worker, contenedor o despliegue usando la sesión.
   - `411` o `500`: preservar auth y preparar una revinculación limpia y autorizada.
   - `403`: revisar estado y políticas del proveedor antes de vincular o reanudar.
   - `401`: confirmar logout y preparar revinculación autorizada.
   - `408`, `428`, `503` o `515`: revisar transporte, tiempos y secuencia de reconexión; no borrar auth por defecto.
7. Correlacionar `worker_started`, `reconnect_scheduled`, `health_transition`, señales de entrega y conflicto de lease. No usar contenido de conversaciones.
8. Sólo después del diagnóstico, ejecutar la acción aprobada. Preservar la sesión anterior y el ledger.
9. Cerrar el incidente únicamente tras una conexión nueva estable y una prueba controlada de mensaje entrante y respuesta. Un heartbeat o `connection: open` aislado no demuestra recuperación.

## Límites conocidos

- No existe una API de WhatsApp que anuncie con certeza un futuro `401`, `403`, `411`, `440` o `500`.
- Un teléfono puede cerrar sesión o reemplazar el dispositivo sin precursores visibles.
- El risk score usa reglas y ventanas locales; no es una probabilidad calibrada estadísticamente.
- El monitor ve sólo los procesos y el volumen de su despliegue.
- `unknown` es el resultado correcto cuando falta evidencia; nunca debe convertirse en una causa inventada.
- La comparación Madrid versus Barcelona requiere volumen persistente, mismo periodo de observación y denominadores equivalentes; una foto puntual no demuestra una tasa de incidentes.
