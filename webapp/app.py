"""FastAPI application assembly and middleware."""

import faulthandler
import sys
import time

_BOOT = time.monotonic()


def _boot_log(step: str) -> None:
    """Start-up progress in the host's logs (nothing else appears until the port opens)."""
    print(f"[startup {time.monotonic() - _BOOT:5.1f}s] {step}", file=sys.stderr, flush=True)


_boot_log("importing the app")
# If start-up stalls, print where every thread is stuck every 60 s until the app is ready.
faulthandler.dump_traceback_later(60, repeat=True, file=sys.stderr)

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

_boot_log("loading web framework, database layer, sign-in and recommender")
from webapp import auth as signin
from webapp import core, database
from webapp.core import (
    DB,
    _warm_recommendation_worker,
    db,
    get_session,
    logger,
    os,
    warm_homepage_caches,
)
from webapp.routes import auth, catalog, commerce, home


@asynccontextmanager
async def lifespan(_: FastAPI):
    homepage_feeds_ready = asyncio.Event()
    app.state.homepage_feed_responses = 0
    app.state.homepage_feeds_ready = homepage_feeds_ready

    async def warm_secondary_recommendations():
        # Warm in the background so uvicorn binds its port immediately; a cold
        # Cloud Run instance otherwise waits for the caches before serving.
        try:
            await asyncio.to_thread(warm_homepage_caches)
        except Exception:
            logger.exception("Homepage recommendation cache warmup failed")
        try:
            await asyncio.wait_for(homepage_feeds_ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        else:
            await asyncio.sleep(2)
        await asyncio.to_thread(_warm_recommendation_worker)

    warmup_task = asyncio.create_task(warm_secondary_recommendations())
    try:
        yield
    finally:
        warmup_task.cancel()
        await asyncio.gather(warmup_task, return_exceptions=True)


app = FastAPI(title="Pantree Storefront", lifespan=lifespan)
_boot_log("app created; the server will now open its port")
faulthandler.cancel_dump_traceback_later()
# Carries only the OAuth "state" between /auth/google/login and the callback (10 minutes).
app.add_middleware(
    SessionMiddleware,
    secret_key=signin.session_secret(),
    session_cookie="pantree_oauth",
    max_age=600,
    same_site="lax",
    https_only=signin.secure_cookies(),
)
static_dir = core.BASE / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/healthz")
def healthcheck():
    if not database.database_url() and not DB.is_file():
        return JSONResponse({"status": "not_ready"}, status_code=503)
    try:
        con = database.connect(DB, timeout=2)
        try:
            tables = database.table_names(con)
        finally:
            con.close()
    except Exception:
        logger.exception("Health check could not reach the database")
        return JSONResponse({"status": "not_ready"}, status_code=503)
    required_tables = {"customers", "cart", "orders", "transactions"}
    if not required_tables.issubset(tables):
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.middleware("http")
async def protect_storefront_sessions(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        public_url = os.getenv("PUBLIC_APP_URL")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        supplied_origin = origin or (
            f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}"
            if referer
            else ""
        )
        configured_origin = (
            f"{urlsplit(public_url).scheme}://{urlsplit(public_url).netloc}"
            if public_url
            else ""
        )
        allowed_origin = configured_origin or expected_origin
        if supplied_origin and supplied_origin != allowed_origin:
            return JSONResponse(
                {"ok": False, "message": "Invalid request origin."}, status_code=403
            )
        if not supplied_origin and os.getenv("APP_ENV", "").lower() in {
            "prod",
            "production",
        }:
            return JSONResponse(
                {"ok": False, "message": "Request origin is required."}, status_code=403
            )

    path = request.url.path
    if path.startswith(("/auth", "/login", "/static/", "/healthz")):
        return await call_next(request)

    sid = request.cookies.get(signin.SESSION_COOKIE)
    if sid:
        con = db()
        try:
            session = get_session(con, sid)
        finally:
            con.close()
        if session is None:  # unknown, expired or signed out elsewhere
            response = (
                RedirectResponse("/login", status_code=303)
                if "text/html" in request.headers.get("accept", "")
                else JSONResponse(
                    {"ok": False, "message": "Please sign in again."}, status_code=401
                )
            )
            response.delete_cookie(signin.SESSION_COOKIE, path="/")
            return response
        request.state.pantree_customer_id = session["customer_id"]
    return await call_next(request)


for route_module in (auth, home, catalog, commerce):
    app.include_router(route_module.router)
