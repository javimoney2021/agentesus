# Agente SUS

Bot administrativo de Discord para publicaciones inmediatas, edición de mensajes e inscripciones de eventos.

## Configuración relevante

- `DISCORD_TOKEN`: token del bot.
- `GUILD_ID`: servidor donde se sincronizan los comandos.
- `DATABASE_URL`: conexión PostgreSQL con TLS.
- `DB_HEARTBEAT_HOURS`: intervalo del heartbeat de PostgreSQL; por defecto, `12`.
- `SUPPORT_CHANNEL_ID`: ID del canal `#soporte` autorizado para `/eliminar_datos`.
- `STAFF_ROLES`: nombres de roles administrativos separados por comas.
- `POST_MAX_ATTACHMENTS`: máximo de adjuntos por `/post`; por defecto, `10`.
- `POST_MAX_FILE_MB`: máximo por archivo adjunto; por defecto, `25`.
- `POST_MAX_TOTAL_MB`: máximo de la suma de adjuntos; por defecto, `50`.

El bot utiliza interacciones y modales. No requiere los intents privilegiados
`MESSAGE_CONTENT`, `GUILD_MEMBERS` ni `GUILD_PRESENCES`.

## Base de datos y migraciones

El esquema vive en `migrations/`. `bot.py` ya no crea tablas al arrancar: solo
abre el pool de conexiones.

```bash
python scripts/migrate.py
```

El runner aplica solo los archivos pendientes, los registra en
`schema_migrations` y es idempotente. El `Procfile` ejecuta esa fase `release`
antes de iniciar el worker. La migración `0002` elimina la tabla obsoleta
`scheduled_posts` y la `0003` elimina `registros`.

Los índices de lectura se declaran en `0001_initial.sql`:
`event_registrations(guild_id, user_id)` y
`event_users(guild_id, updated_at)`.

El heartbeat reconstruye el pool después de dos comprobaciones fallidas y los
comandos responden con un aviso mientras la base no esté disponible.

## Publicaciones

`/post` abre un selector de canal y permite publicar de inmediato mediante un
modal con texto, adjuntos y un nombre de hilo opcional. Los adjuntos se
descargan en streaming a un directorio temporal con límites de tamaño; el bot
no los guarda en almacenamiento externo.

`/post_edit` permite reemplazar el contenido de una publicación ya enviada por
el bot. El sistema de publicaciones agendadas fue retirado.

Los thumbnails de eventos conservan sus URLs públicas actuales.

## Eventos

Al finalizar un evento, el bot genera la exportación `.xlsx` en un hilo de
ejecución aparte, publica la lista final en menos mensajes y retira los cargos
con concurrencia limitada. La exportación solo se adjunta a la respuesta
efímera del miembro autorizado antes de limpiar la lista activa.

## Eliminación de datos

`/eliminar_datos` permite que un usuario borre su perfil de eventos y sus
inscripciones activas desde el canal `#soporte`. La baja inicia un periodo de
enfriamiento y no elimina entradas de blacklist ni otros controles
administrativos justificados.

## Limpieza manual

`scripts/cleanup_scheduled_posts.py` sigue disponible para eliminar
`scheduled_posts` manualmente si hiciera falta. Normalmente no es necesario:
la migración `0002` ya lo hace en cada despliegue que la tenga pendiente.
