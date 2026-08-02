import ipaddress
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import discord
from aiohttp import web

from core import database
from core.config import (
    API_HOST,
    API_PORT,
    DATA_RETENTION_DAYS,
    FRONTEND_URL,
    GUILD_ID,
    VERIFIED_ROLE_ID,
)
from core.verification_security import (
    InvalidVerificationToken,
    VerificationConfigurationError,
    hash_ip_address,
    hash_limited_fingerprint,
    token_digest,
    validate_signed_verification_token,
)


logger = logging.getLogger(__name__)
API_NAME = "verification-sa-api"
API_VERSION = 1
MAX_REQUEST_SIZE = 64 * 1024
RATE_WINDOW = timedelta(minutes=15)
USER_SUBMISSION_LIMIT = 5
IP_SUBMISSION_LIMIT = 30
SUBMISSION_KEYS = frozenset({"token", "consent", "signals"})
SIGNAL_KEYS = frozenset({
    "signalVersion",
    "language",
    "timezone",
    "userAgent",
    "platform",
    "mobile",
    "deviceClass",
    "touchSupport",
})
DEVICE_CLASSES = frozenset({"phone", "tablet", "desktop"})
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2}$")


class InvalidSubmission(ValueError):
    pass


def _frontend_origin() -> str:
    parsed = urlsplit(FRONTEND_URL)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("FRONTEND_URL debe ser una direccion HTTPS valida.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _apply_security_headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    )


def _error_response(code: str, status: int) -> web.Response:
    return web.json_response(
        {"status": "error", "code": code},
        status=status,
    )


def _safe_string(signals: dict, key: str, max_length: int) -> str:
    value = signals.get(key)
    if not isinstance(value, str):
        raise InvalidSubmission(f"Campo {key} invalido.")
    value = value.strip()
    if not value or len(value) > max_length:
        raise InvalidSubmission(f"Campo {key} invalido.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidSubmission(f"Campo {key} invalido.")
    return value


def _parse_submission(payload: object) -> tuple[str, dict]:
    if not isinstance(payload, dict) or frozenset(payload) != SUBMISSION_KEYS:
        raise InvalidSubmission("Estructura de solicitud invalida.")
    if payload.get("consent") is not True:
        raise InvalidSubmission("Consentimiento requerido.")

    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise InvalidSubmission("Token invalido.")

    signals = payload.get("signals")
    if not isinstance(signals, dict) or frozenset(signals) != SIGNAL_KEYS:
        raise InvalidSubmission("Senales invalidas.")
    if type(signals.get("signalVersion")) is not int:
        raise InvalidSubmission("Version de senales invalida.")
    if signals["signalVersion"] != 1:
        raise InvalidSubmission("Version de senales no compatible.")
    if type(signals.get("mobile")) is not bool:
        raise InvalidSubmission("Campo mobile invalido.")
    if type(signals.get("touchSupport")) is not bool:
        raise InvalidSubmission("Campo touchSupport invalido.")

    device_class = signals.get("deviceClass")
    if device_class not in DEVICE_CLASSES:
        raise InvalidSubmission("Clase de dispositivo invalida.")

    sanitized = {
        "signal_version": signals["signalVersion"],
        "language": _safe_string(signals, "language", 32),
        "timezone": _safe_string(signals, "timezone", 64),
        "user_agent": _safe_string(signals, "userAgent", 512),
        "platform": _safe_string(signals, "platform", 64),
        "mobile": signals["mobile"],
        "device_class": device_class,
        "touch_support": signals["touchSupport"],
    }
    return token, sanitized


def _browser_family(user_agent: str) -> str:
    lowered = user_agent.lower()
    if "edg/" in lowered or "edgios/" in lowered or "edga/" in lowered:
        return "Edge"
    if "opr/" in lowered or "opera" in lowered:
        return "Opera"
    if "samsungbrowser/" in lowered:
        return "Samsung Internet"
    if "crios/" in lowered or "chrome/" in lowered:
        return "Chrome"
    if "fxios/" in lowered or "firefox/" in lowered:
        return "Firefox"
    if "safari/" in lowered:
        return "Safari"
    return "Other"


def _os_family(user_agent: str) -> str:
    lowered = user_agent.lower()
    if "iphone" in lowered or "ipad" in lowered or "ipod" in lowered:
        return "iOS"
    if "android" in lowered:
        return "Android"
    if "windows" in lowered:
        return "Windows"
    if "cros" in lowered:
        return "ChromeOS"
    if "mac os" in lowered or "macintosh" in lowered:
        return "macOS"
    if "linux" in lowered:
        return "Linux"
    return "Other"


def _client_ip(request: web.Request) -> str:
    supplied_ip = request.headers.get("CF-Connecting-IP") or request.remote
    if not supplied_ip:
        raise InvalidSubmission("Direccion de red no disponible.")
    try:
        parsed_ip = ipaddress.ip_address(supplied_ip.strip())
    except ValueError as exc:
        raise InvalidSubmission("Direccion de red invalida.") from exc
    if isinstance(parsed_ip, ipaddress.IPv6Address) and parsed_ip.ipv4_mapped:
        parsed_ip = parsed_ip.ipv4_mapped
    return parsed_ip.compressed


def _country_code(request: web.Request) -> str | None:
    country = request.headers.get("CF-IPCountry", "").strip().upper()
    return country if COUNTRY_CODE_PATTERN.fullmatch(country) else None


async def _get_member(bot, guild_id: int, user_id: int):
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


def create_verification_app(bot) -> web.Application:
    allowed_origin = _frontend_origin()
    configured_guild_id = int(GUILD_ID) if GUILD_ID and GUILD_ID.isdigit() else None

    @web.middleware
    async def request_security(
        request: web.Request,
        handler,
    ) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin != allowed_origin:
            response = _error_response("origin_not_allowed", 403)
            _apply_security_headers(response)
            response.headers["Vary"] = "Origin"
            return response

        try:
            response = await handler(request)
        except web.HTTPException as http_error:
            response = http_error

        _apply_security_headers(response)
        response.headers["Vary"] = "Origin"
        if origin == allowed_origin:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": API_NAME,
                "version": API_VERSION,
            }
        )

    async def submit(request: web.Request) -> web.Response:
        if database.bot_pool is None or not bot.is_ready():
            return _error_response("temporarily_unavailable", 503)
        if request.content_type != "application/json":
            return _error_response("invalid_request", 415)

        try:
            request_payload = await request.json(loads=json.loads)
            supplied_token, signals = _parse_submission(request_payload)
            verification_token = validate_signed_verification_token(
                supplied_token,
                expected_guild_id=configured_guild_id,
            )
        except (json.JSONDecodeError, InvalidSubmission):
            return _error_response("invalid_request", 400)
        except InvalidVerificationToken:
            return _error_response("invalid_or_expired_link", 400)
        except VerificationConfigurationError:
            logger.exception("Configuracion criptografica de verificacion invalida.")
            return _error_response("temporarily_unavailable", 503)

        try:
            member = await _get_member(
                bot,
                verification_token.guild_id,
                verification_token.user_id,
            )
        except discord.HTTPException:
            logger.exception(
                "No se pudo comprobar el miembro de verificacion %s.",
                verification_token.user_id,
            )
            return _error_response("temporarily_unavailable", 503)

        if member is None or member.bot:
            return _error_response("membership_required", 403)

        supplied_digest = token_digest(supplied_token)
        if member.get_role(VERIFIED_ROLE_ID) is not None:
            await database.revoke_verification_token(
                verification_token.token_id,
                supplied_digest,
            )
            return web.json_response({"status": "completed"})

        try:
            client_ip = _client_ip(request)
            ip_hash = hash_ip_address(client_ip)
            browser_family = _browser_family(signals["user_agent"])
            os_family = _os_family(signals["user_agent"])
            fingerprint_basis = {
                "version": signals["signal_version"],
                "language": signals["language"].lower(),
                "timezone": signals["timezone"],
                "browser_family": browser_family,
                "os_family": os_family,
                "platform": signals["platform"],
                "mobile": signals["mobile"],
                "device_class": signals["device_class"],
                "touch_support": signals["touch_support"],
            }
            fingerprint_hash = hash_limited_fingerprint(fingerprint_basis)
        except (InvalidSubmission, VerificationConfigurationError):
            return _error_response("temporarily_unavailable", 503)

        current_time = datetime.now(timezone.utc)
        try:
            user_count, ip_count = await database.get_verification_submission_counts(
                verification_token.guild_id,
                verification_token.user_id,
                ip_hash,
                current_time - RATE_WINDOW,
            )
            if user_count >= USER_SUBMISSION_LIMIT or ip_count >= IP_SUBMISSION_LIMIT:
                return _error_response("too_many_requests", 429)

            attempt = await database.record_pending_verification_attempt(
                token_id=verification_token.token_id,
                token_digest=supplied_digest,
                guild_id=verification_token.guild_id,
                user_id=verification_token.user_id,
                discord_tag=str(member)[:128],
                ip_hash=ip_hash,
                fingerprint_hash=fingerprint_hash,
                country_code=_country_code(request),
                region=None,
                timezone_name=signals["timezone"],
                language=signals["language"],
                browser_family=browser_family,
                os_family=os_family,
                device_type=signals["device_class"],
                signals={
                    "signal_version": signals["signal_version"],
                    "platform": signals["platform"],
                    "mobile": signals["mobile"],
                    "touch_support": signals["touch_support"],
                },
                retention_until=current_time + timedelta(days=DATA_RETENTION_DAYS),
            )
        except Exception:
            logger.exception(
                "No se pudo registrar la solicitud de verificacion del usuario %s.",
                verification_token.user_id,
            )
            return _error_response("temporarily_unavailable", 503)

        if attempt is None:
            return _error_response("invalid_or_expired_link", 400)
        return web.json_response({"status": "accepted"}, status=202)

    async def options(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application(
        middlewares=[request_security],
        client_max_size=MAX_REQUEST_SIZE,
    )
    app.router.add_get("/health", health)
    app.router.add_post("/api/verification/submit", submit)
    app.router.add_route("OPTIONS", "/{path:.*}", options)
    return app


class VerificationAPIServer:
    def __init__(self, bot):
        self.bot = bot
        self._runner = None
        self._site = None

    @property
    def is_running(self) -> bool:
        return self._site is not None

    async def start(self) -> None:
        if self.is_running:
            return

        runner = web.AppRunner(
            create_verification_app(self.bot),
            access_log=None,
        )
        await runner.setup()
        try:
            site = web.TCPSite(runner, API_HOST, API_PORT)
            await site.start()
        except Exception:
            await runner.cleanup()
            raise

        self._runner = runner
        self._site = site
        print(f"✅ API de Verificacion SA activa en {API_HOST}:{API_PORT}/health")

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        self._site = None
