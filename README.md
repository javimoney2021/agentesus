# Agente SUS

Bot administrativo de Discord para publicaciones e inscripciones de eventos.

## Configuración relevante

- `DISCORD_TOKEN`: token del bot.
- `GUILD_ID`: servidor donde se sincronizan los comandos.
- `DATABASE_URL`: conexión PostgreSQL con TLS.
- `DB_HEARTBEAT_HOURS`: intervalo del heartbeat de PostgreSQL; por defecto, `12`.

El publicador revisa su cache cada minuto sin hacer un ping SQL. La limpieza
de plazos se ejecuta diariamente. Los checks fallidos se reintentan con espera
creciente hasta 30 minutos; no encienden un servicio apagado en Aiven.
Los logs ACTIVITY resumen operaciones cada 15 minutos y alertan sobre rafagas
(60 operaciones del mismo tipo/minuto, o 5 errores 429/reconexiones/minuto).
Estos umbrales son diagnosticos internos, no limites oficiales de Square.
Los contadores no incluyen todos los reintentos internos de las librerias.
Los posts fallidos se conservan con espera creciente hasta 60 minutos.
La proteccion de mensajes ya enviados es en memoria: aun debe persistirse para
resolver el caso de un reinicio entre el envio a Discord y la confirmacion SQL.
- `SUPPORT_CHANNEL_ID`: ID del canal `#soporte` autorizado para `/eliminar_datos`.
- `STAFF_ROLES`: nombres de roles administrativos separados por comas.
- `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`, `R2_PUBLIC_URL`: almacenamiento temporal de adjuntos.

El bot utiliza interacciones y modales. No requiere los intents privilegiados
`MESSAGE_CONTENT`, `GUILD_MEMBERS` ni `GUILD_PRESENCES`.

Al finalizar un evento, el bot genera en memoria una exportación `.xlsx` y la
adjunta a la respuesta efímera del miembro autorizado antes de limpiar la lista activa.
