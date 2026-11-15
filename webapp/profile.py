"""Customer profile: create a customer from a Google sign-up and edit details afterwards.

Non-identifying fields go to the ``customers`` dimension (the same columns the simulated
warehouse has, so real and simulated shoppers can be analysed together); identifying fields go
to ``customer_pii``, which is never copied to the analytics mirror.
"""
from __future__ import annotations

try:
    from . import database
    from .geo import GEO
except ImportError:
    import database
    from geo import GEO

GENDER_CHOICES = {"F": "Female", "M": "Male", "Other": "Other", "Prefer not to say": "Prefer not to say"}
AGE_BRACKETS = ["18-24", "25-34", "35-44", "45-54", "55+"]
HEARD_FROM_CHOICES = {
    "Organic": "Search engine (Google, Bing…)",
    "Social Ads": "Social media",
    "Referral": "A friend or family member",
    "Paid Search": "An online ad",
    "Email/CRM": "Email or newsletter",
    "Direct": "I typed the website address",
    "Other": "Other",
}
CITY_CHOICES = {
    city: (state, region)
    for region, states in GEO.items()
    for state, cities in states.items()
    for city in cities
}

# Profile fields a customer can edit, by table.
DIM_FIELDS = ("signup_city", "signup_state", "signup_region", "gender", "age_bracket",
              "acquisition_channel", "email_opt_in", "sms_opt_in")
PII_FIELDS = ("first_name", "last_name", "phone", "home_address_line1", "home_address_line2",
              "home_postal_code", "home_country")
REQUIRED_FOR_COMPLETE = ("first_name", "phone", "signup_city", "home_address_line1", "home_postal_code")


def _text(form, name, limit):
    value = (form.get(name) or "").strip()
    return value[:limit] if value else ""


def validate_profile(form, normalize_phone) -> tuple[dict, dict, list[str]]:
    """Clean the profile form. Returns (dimension updates, PII updates, errors)."""
    errors = []
    first_name = _text(form, "first_name", 200)
    last_name = _text(form, "last_name", 200)
    if not first_name or len(first_name) > 60:
        errors.append("Enter your first name (up to 60 characters).")
    if len(last_name) > 60:
        errors.append("Last name is too long.")

    phone = None
    if (form.get("phone") or "").strip():
        try:
            kind, phone = normalize_phone(form["phone"])
            if kind != "phone":
                raise ValueError
        except ValueError:
            errors.append("Enter a valid mobile number (10 digits, or with a country code).")
            phone = None

    city = form.get("city") or ""
    if city and city not in CITY_CHOICES:
        errors.append("Choose your city from the list.")
        city = ""
    state, region = CITY_CHOICES.get(city, (None, None))

    gender = form.get("gender") or "Prefer not to say"
    if gender not in GENDER_CHOICES:
        errors.append("Choose a valid gender option.")
    age_bracket = form.get("age_bracket") or None
    if age_bracket and age_bracket not in AGE_BRACKETS:
        errors.append("Choose a valid age range.")
    heard_from = form.get("heard_from") or None
    if heard_from and heard_from not in HEARD_FROM_CHOICES:
        errors.append("Choose a valid option for how you heard about us.")

    postal_code = _text(form, "home_postal_code", 20)
    if postal_code and not postal_code.replace(" ", "").isalnum():
        errors.append("Enter a valid postal code.")

    dim = {
        "signup_city": city or None,
        "signup_state": state,
        "signup_region": region,
        "gender": gender,
        "age_bracket": age_bracket,
        "acquisition_channel": heard_from or "Unknown",
        "email_opt_in": int(bool(form.get("email_opt_in"))),
        "sms_opt_in": int(bool(form.get("sms_opt_in"))),
    }
    pii = {
        "first_name": first_name,
        "last_name": last_name or None,
        "phone": phone,
        "home_address_line1": _text(form, "home_address_line1", 200) or None,
        "home_address_line2": _text(form, "home_address_line2", 200) or None,
        "home_postal_code": postal_code or None,
        "home_country": _text(form, "home_country", 60) or "India",
    }
    return dim, pii, errors


def create_customer(con, *, customer_id, email, first_name, last_name, created, campaign):
    """Insert the dimension row and the PII row for a new customer. Returns the dimension row."""
    dim_row = {
        "customer_id": customer_id,
        "signup_date": created[:10],
        "signup_city": None,
        "signup_state": None,
        "signup_region": None,
        "acquisition_channel": "Unknown",
        "acquisition_campaign": campaign,
        "age_bracket": None,
        "gender": "Prefer not to say",
        "is_prime_member": 0,
        "prime_start_date": None,
        "prime_tier": "Non-Prime",
        "email_opt_in": 0,
        "sms_opt_in": 0,
        "account_status": "active",
        "terms_accepted_at": created,
    }
    existing = set(database.table_columns(con, "customers"))
    columns = [c for c in dim_row if c in existing]
    con.execute(
        f"INSERT INTO customers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [dim_row[c] for c in columns],
    )
    con.execute(
        """INSERT INTO customer_pii (customer_id, first_name, last_name, email, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (customer_id, first_name, last_name, email, created, created),
    )
    return {c: dim_row[c] for c in columns}


def save_profile(con, customer_id, dim: dict, pii: dict, updated: str) -> dict:
    """Write the edited profile to customers and customer_pii. Returns the full dimension row."""
    existing = set(database.table_columns(con, "customers"))
    dim_columns = [c for c in DIM_FIELDS if c in existing]
    con.execute(
        f"UPDATE customers SET {', '.join(f'{c}=?' for c in dim_columns)} WHERE customer_id=?",
        [dim[c] for c in dim_columns] + [customer_id],
    )
    con.execute(
        "INSERT INTO customer_pii (customer_id, created_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (customer_id, updated),
    )
    con.execute(
        f"UPDATE customer_pii SET {', '.join(f'{c}=?' for c in PII_FIELDS)}, updated_at=? WHERE customer_id=?",
        [pii[c] for c in PII_FIELDS] + [updated, customer_id],
    )
    row = con.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    return dict(row) if row else {}


def phone_in_use(con, customer_id, phone) -> bool:
    if not phone:
        return False
    row = con.execute(
        "SELECT 1 FROM customer_pii WHERE phone=? AND customer_id<>?", (phone, customer_id)
    ).fetchone()
    return row is not None


def profile_complete(customer) -> bool:
    return bool(customer) and all(customer[name] for name in REQUIRED_FOR_COMPLETE)
