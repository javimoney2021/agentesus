# Agente SUS

Bot administrativo de Discord para publicaciones e inscripciones de eventos.

## Configuración relevante

- `DISCORD_TOKEN`: token del bot.
- `GUILD_ID`: servidor donde se sincronizan los comandos.
- `DATABASE_URL`: conexión PostgreSQL con TLS.
- `SUPPORT_CHANNEL_ID`: ID del canal `#soporte` autorizado para `/eliminar_datos`.
- `STAFF_ROLES`: nombres de roles administrativos separados por comas.
- `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`, `R2_PUBLIC_URL`: almacenamiento temporal de adjuntos.

El bot utiliza interacciones y modales. No requiere los intents privilegiados
`MESSAGE_CONTENT`, `GUILD_MEMBERS` ni `GUILD_PRESENCES`.

Al finalizar un evento, el bot genera en memoria una exportación `.xlsx` y la
adjunta a la respuesta efímera del miembro autorizado antes de limpiar la lista activa.
