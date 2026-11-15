"""Sign-in (Google), sign-out and the account / profile pages."""

from urllib.parse import quote_plus

from fastapi import APIRouter

from .. import auth, profile
from ..core import (
    _catalog_cached,
    Cookie,
    HTMLResponse,
    RedirectResponse,
    Request,
    _departments,
    _normalize_login_identifier,
    base_ctx,
    database,
    db,
    ensure_auth_tables,
    forget_customer_activity,
    get_session,
    log_event,
    logger,
    mirror_customer,
    now,
    reset_customer_activity,
    tpl,
    uid,
)

router = APIRouter()

LOGIN_ERRORS = {
    "failed": "Google sign-in did not complete. Please try again.",
    "unverified": "Your Google account's email address is not verified.",
    "inactive": "This Pantree account is not active.",
    "unavailable": "Sign-in is temporarily unavailable. Please try again in a minute.",
}


def _redirect_with(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote_plus(msg)}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", msg: str = "", sid: str = Cookie(None)):
    con = db()
    try:
        if get_session(con, sid):
            return RedirectResponse("/", status_code=303)
        showcase = _showcase_products(con)
    finally:
        con.close()
    return tpl.TemplateResponse(
        request,
        "login.html",
        {
            "title": "Sign in",
            "error": LOGIN_ERRORS.get(error, ""),
            "msg": msg,
            "showcase": showcase,
            "google_enabled": auth.google_configured(),
            "dev_login": auth.dev_login_enabled(),
        },
    )


def _showcase_products(con, per_department=2):
    """A few well-reviewed products with photos for the sign-in page (cached on Postgres)."""
    def compute():
        rows = con.execute(
            """SELECT p.product_name, p.brand_name, p.image_url, p.h1_category
               FROM products p JOIN inventory i USING(product_id)
               WHERE p.image_url IS NOT NULL AND p.image_url <> '' AND i.current_stock > 0
               ORDER BY p.review_count DESC, p.avg_rating DESC, p.product_id LIMIT 60"""
        ).fetchall()
        picked, per = [], {}
        for row in rows:
            department = row["h1_category"]
            if per.get(department, 0) < per_department:
                per[department] = per.get(department, 0) + 1
                picked.append(dict(row))
        return picked[:6]

    try:
        return _catalog_cached(con, "login_showcase", compute)
    except Exception:  # e.g. a fresh, unseeded database
        logger.warning("Sign-in showcase unavailable", exc_info=True)
        return []


# ----------------------------------------------------------------------------- identity -> customer
class InactiveAccountError(ValueError):
    """The customer behind this identity is not active."""


def customer_for_identity(con, provider, subject, email, first_name, last_name, picture=None):
    """Return (customer_id, created) for a verified identity, creating the customer if needed.

    1. The identity is already linked -> that customer.
    2. A customer already has this email -> link the identity to them (sign-in).
    3. Otherwise -> create a new customer from the profile (sign-up).
    """
    ensure_auth_tables(con)
    email = (email or "").strip().lower() or None
    created = False
    linked = con.execute(
        "SELECT customer_id FROM auth_identities WHERE provider=? AND subject=?",
        (provider, subject),
    ).fetchone()
    customer_id = linked[0] if linked else None
    if customer_id is None and email:
        row = con.execute(
            "SELECT customer_id FROM customer_pii WHERE lower(email)=?", (email,)
        ).fetchone()
        customer_id = row[0] if row else None
    if customer_id is None:
        customer_id = uid("cust")
        dim_row = profile.create_customer(
            con, customer_id=customer_id, email=email, first_name=(first_name or "")[:60] or None,
            last_name=(last_name or "")[:60] or None, created=now(), campaign=f"{provider}_signup",
        )
        mirror_customer(dim_row)
        created = True
    else:
        account = con.execute(
            "SELECT account_status FROM customers WHERE customer_id=?", (customer_id,)
        ).fetchone()
        if account and account[0] not in (None, "active"):
            raise InactiveAccountError(customer_id)
        if first_name:  # fill a missing name from the Google profile, never overwrite
            con.execute(
                "UPDATE customer_pii SET first_name=? WHERE customer_id=? AND (first_name IS NULL OR first_name='')",
                (first_name[:60], customer_id),
            )
    stamp = now()
    if linked:
        con.execute(
            "UPDATE auth_identities SET email=?, last_login_at=?, picture_url=? WHERE provider=? AND subject=?",
            (email, stamp, picture, provider, subject),
        )
    else:
        con.execute(
            """INSERT INTO auth_identities
               (provider, subject, customer_id, email, created_at, last_login_at, picture_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (provider, subject, customer_id, email, stamp, stamp, picture),
        )
    return customer_id, created


def start_session(request: Request, con, customer_id: str, created: bool) -> RedirectResponse:
    """Create the storefront session and its cookie; new customers go to their profile."""
    import datetime as dt

    sid = auth.new_session_id()
    device = auth.device_from_user_agent(request.headers.get("user-agent"))
    started = now()
    expires = (dt.datetime.fromisoformat(started) + dt.timedelta(days=auth.SESSION_DAYS)).isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO sessions (session_id, customer_id, device_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (sid, customer_id, device, started, expires),
    )
    log_event(con, customer_id, sid, device, "app_open", "home")
    con.commit()
    response = RedirectResponse("/account?welcome=1" if created else "/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, sid, httponly=True, secure=auth.secure_cookies(), samesite="lax",
        max_age=60 * 60 * 24 * auth.SESSION_DAYS, path="/",
    )
    return response


def _finish_sign_in(request, provider, subject, email, first_name, last_name, picture=None):
    con = db()
    try:
        customer_id, created = customer_for_identity(
            con, provider, subject, email, first_name, last_name, picture
        )
        return start_session(request, con, customer_id, created)
    except InactiveAccountError:
        con.rollback()
        return RedirectResponse("/login?error=inactive", status_code=303)
    except (ValueError, *database.IntegrityError, *database.OperationalError) as exc:
        con.rollback()
        logger.error("Could not finish sign-in (error_type=%s)", type(exc).__name__)
        return RedirectResponse("/login?error=unavailable", status_code=303)
    finally:
        con.close()


# ----------------------------------------------------------------------------- Google
@router.get("/auth/google/login")
async def google_login(request: Request):
    client = auth.oauth()
    if client is None:
        return RedirectResponse("/login?error=unavailable", status_code=303)
    return await client.google.authorize_redirect(
        request, f"{auth.public_url()}/auth/google/callback", prompt="select_account"
    )


@router.get("/auth/google/callback")
async def google_callback(request: Request):
    client = auth.oauth()
    if client is None:
        return RedirectResponse("/login?error=unavailable", status_code=303)
    try:
        token = await client.google.authorize_access_token(request)
    except Exception as exc:  # cancelled, expired state, network error
        logger.warning("Google sign-in failed (error_type=%s)", type(exc).__name__)
        return RedirectResponse("/login?error=failed", status_code=303)
    info = token.get("userinfo") or {}
    if not info.get("sub") or not info.get("email"):
        return RedirectResponse("/login?error=failed", status_code=303)
    if not info.get("email_verified", False):
        return RedirectResponse("/login?error=unverified", status_code=303)
    return _finish_sign_in(
        request, "google", str(info["sub"]), info["email"],
        info.get("given_name") or info.get("name"), info.get("family_name"), info.get("picture"),
    )


# ----------------------------------------------------------------------------- local development only
@router.post("/auth/dev-login")
async def dev_login(request: Request):
    """Sign in without Google on a developer machine (APP_ENV=development, no OAuth client)."""
    if not auth.dev_login_enabled():
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        kind, email = _normalize_login_identifier(form.get("email") or "")
        if kind != "email":
            raise ValueError
    except ValueError:
        return RedirectResponse("/login?msg=Enter+a+valid+email+address.", status_code=303)
    name = (form.get("name") or "").strip() or email.split("@")[0]
    return _finish_sign_in(request, "dev", email, email, name, None)


@router.post("/logout")
async def logout(request: Request, sid: str = Cookie(None)):
    if sid:
        con = db()
        try:
            con.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
            con.commit()
        finally:
            con.close()
    request.session.clear()
    response = RedirectResponse("/login?msg=You+have+signed+out.", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ----------------------------------------------------------------------------- account and profile
def _customer(con, customer_id):
    return con.execute(
        """SELECT c.*, p.first_name, p.last_name, p.email, p.phone, p.home_address_line1,
                  p.home_address_line2, p.home_postal_code, p.home_country
           FROM customers c LEFT JOIN customer_pii p USING(customer_id) WHERE c.customer_id=?""",
        (customer_id,),
    ).fetchone()


def _account_context(con, s, request, **extra):
    orders = con.execute(
        """SELECT o.*, t.ship_address_line1, t.ship_address_line2,
        t.ship_city, t.ship_state, t.ship_region, t.ship_postal_code, t.ship_country
        FROM orders o LEFT JOIN transactions t ON t.transaction_id = (
            SELECT MIN(t2.transaction_id) FROM transactions t2 WHERE t2.order_id = o.order_id)
        WHERE o.customer_id=? ORDER BY o.order_timestamp DESC LIMIT 10""",
        (s["customer_id"],),
    ).fetchall()
    customer = _customer(con, s["customer_id"])
    ctx = base_ctx(con, s, request, cats=_departments(con), title="Your account")
    ctx.update({
        "customer": customer,
        "orders": orders,
        "cities": sorted(profile.CITY_CHOICES),
        "genders": profile.GENDER_CHOICES,
        "age_brackets": profile.AGE_BRACKETS,
        "heard_from_choices": profile.HEARD_FROM_CHOICES,
        "profile_complete": profile.profile_complete(customer),
        "form": None,
        "errors": [],
        "msg": "",
        "welcome": False,
    })
    ctx.update(extra)
    return ctx


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, sid: str = Cookie(None), msg: str = "", welcome: int = 0):
    con = db()
    try:
        s = get_session(con, sid)
        if not s:
            return RedirectResponse("/login", status_code=303)
        ctx = _account_context(con, s, request, msg=msg, welcome=bool(welcome))
    finally:
        con.close()
    return tpl.TemplateResponse(request, "account.html", ctx)


@router.post("/account/profile", response_class=HTMLResponse)
async def save_profile(request: Request, sid: str = Cookie(None)):
    """Save the profile form to customers (dimension) and customer_pii (personal details)."""
    form = dict(await request.form())
    con = db()
    try:
        s = get_session(con, sid)
        if not s:
            return RedirectResponse("/login", status_code=303)
        customer_id = s["customer_id"]
        dim, pii, errors = profile.validate_profile(form, _normalize_login_identifier)
        if not errors and profile.phone_in_use(con, customer_id, pii["phone"]):
            errors.append("This mobile number is already used by another account.")
        if errors:
            ctx = _account_context(con, s, request, form=form, errors=errors)
            return tpl.TemplateResponse(request, "account.html", ctx, status_code=422)
        try:
            dim_row = profile.save_profile(con, customer_id, dim, pii, now())
            log_event(con, customer_id, sid, s["device_type"], "profile_update", "account")
            con.commit()
        except database.IntegrityError:
            con.rollback()
            ctx = _account_context(con, s, request, form=form,
                                   errors=["This mobile number is already used by another account."])
            return tpl.TemplateResponse(request, "account.html", ctx, status_code=422)
    finally:
        con.close()
    mirror_customer(dim_row)
    return _redirect_with("/account", "Your profile was saved.")


@router.post("/account/reset-recommendations")
def reset_recommendations(sid: str = Cookie(None)):
    """Delete this customer's activity (keeps the account and login) and start fresh."""
    con = db()
    try:
        s = get_session(con, sid)
        if not s:
            return RedirectResponse("/login", status_code=303)
        customer_id = s["customer_id"]
        reset_customer_activity(con, customer_id, keep_session_id=sid, now=now(), new_id=uid)
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("Account reset failed")
        return _redirect_with("/account", "Could not reset your recommendations. Please try again.")
    finally:
        con.close()
    forget_customer_activity(customer_id)
    logger.info("Customer reset their recommendations")
    return _redirect_with("/account", "Your recommendations were reset.")
