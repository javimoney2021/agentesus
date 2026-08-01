from urllib.parse import urlsplit

from aiohttp import web

from core.config import API_HOST, API_PORT, FRONTEND_URL


API_NAME = "verification-sa-api"
API_VERSION = 1
MAX_REQUEST_SIZE = 64 * 1024


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


def create_verification_app() -> web.Application:
    allowed_origin = _frontend_origin()

    @web.middleware
    async def request_security(
        request: web.Request,
        handler,
    ) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin != allowed_origin:
            response = web.json_response(
                {"error": "origin_not_allowed"},
                status=403,
            )
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

    async def options(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application(
        middlewares=[request_security],
        client_max_size=MAX_REQUEST_SIZE,
    )
    app.router.add_get("/health", health)
    app.router.add_route("OPTIONS", "/{path:.*}", options)
    return app


class VerificationAPIServer:
    def __init__(self):
        self._runner = None
        self._site = None

    @property
    def is_running(self) -> bool:
        return self._site is not None

    async def start(self) -> None:
        if self.is_running:
            return

        runner = web.AppRunner(
            create_verification_app(),
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
