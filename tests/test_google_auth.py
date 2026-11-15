"""Google sign-in, storefront sessions and the customer profile page (SQLite, no network)."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import webapp.app as storefront
from webapp import auth, core
from webapp.routes import auth as auth_routes

SCHEMA = """
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY, signup_date TEXT, signup_city TEXT, signup_state TEXT,
    signup_region TEXT, acquisition_channel TEXT, acquisition_campaign TEXT, age_bracket TEXT,
    gender TEXT, is_prime_member INTEGER, prime_start_date TEXT, prime_tier TEXT,
    email_opt_in INTEGER, sms_opt_in INTEGER, account_status TEXT, terms_accepted_at TEXT
);
CREATE TABLE products (product_id TEXT PRIMARY KEY, h1_category TEXT, h2_category TEXT, current_price REAL);
CREATE TABLE clickstream (
    event_id TEXT PRIMARY KEY, session_id TEXT, customer_id TEXT, order_id TEXT,
    event_timestamp TEXT, event_type TEXT, page_type TEXT, product_id TEXT,
    search_query TEXT, device_type TEXT, traffic_source TEXT, campaign_id TEXT
);
CREATE TABLE cart (customer_id TEXT, product_id TEXT, quantity INTEGER, added_at TEXT);
CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT, order_timestamp TEXT,
                     order_status TEXT, total_amount REAL);
CREATE TABLE transactions (transaction_id TEXT PRIMARY KEY, order_id TEXT, ship_city TEXT,
                           ship_state TEXT, ship_region TEXT);
CREATE TABLE supertokens_customers (supertokens_user_id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE pending_signups (device_id TEXT PRIMARY KEY, payload TEXT, created_at REAL);
INSERT INTO products VALUES ('prod_1', 'Apparel', 'Topwear', 999);
INSERT INTO customers (customer_id, signup_date, account_status, gender)
    VALUES ('cust_existing', '2025-01-01', 'active', 'F'),
           ('cust_closed', '2025-01-01', 'closed', 'M');
"""

GOOGLE_USER = {
    "sub": "google-123", "email": "Asha@Example.com", "email_verified": True,
    "given_name": "Asha", "family_name": "Rao", "picture": "https://example.com/a.png",
}


def google_client(userinfo):
    google = SimpleNamespace(authorize_access_token=AsyncMock(return_value={"userinfo": userinfo}))
    return SimpleNamespace(google=google)


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "live.db"
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)
        con.execute("CREATE TABLE customer_pii (customer_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,"
                    " email TEXT UNIQUE, phone TEXT UNIQUE, home_address_line1 TEXT, home_address_line2 TEXT,"
                    " home_postal_code TEXT, home_country TEXT, created_at TEXT, updated_at TEXT)")
        con.execute("INSERT INTO customer_pii (customer_id, first_name, email) VALUES "
                    "('cust_existing', NULL, 'existing@example.com'), ('cust_closed', 'C', 'closed@example.com')")
        con.commit()
        con.close()
        self.patches = [
            patch.object(core, "DB", self.path),
            patch.object(storefront, "DB", self.path),
            patch.dict(os.environ, {"DATABASE_URL": "", "APP_ENV": "development",
                                    "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret"}),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(storefront.app)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def query(self, sql, *params):
        con = core.db()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def sign_in(self, userinfo=GOOGLE_USER):
        with patch.object(auth, "oauth", return_value=google_client(userinfo)):
            return self.client.get("/auth/google/callback", follow_redirects=False)

    # --------------------------------------------------------------- sign-in / sign-up
    def test_login_page_offers_google_only(self):
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/auth/google/login"', page.text)
        self.assertNotIn("/auth/dev-login", page.text)
        self.assertNotIn("otp", page.text.lower())

    def test_terms_and_privacy_are_public_and_linked_from_sign_in(self):
        for path in ("/terms", "/privacy"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
        login = self.client.get("/login")
        self.assertIn('href="/terms"', login.text)
        self.assertIn('href="/privacy"', login.text)

    def test_new_google_account_creates_customer_and_goes_to_profile(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/account?welcome=1")
        self.assertIn("sid=", response.headers["set-cookie"])
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        (identity,) = self.query("SELECT * FROM auth_identities")
        self.assertEqual((identity["provider"], identity["subject"]), ("google", "google-123"))
        (pii,) = self.query("SELECT * FROM customer_pii WHERE customer_id=?", identity["customer_id"])
        self.assertEqual((pii["email"], pii["first_name"], pii["last_name"]), ("asha@example.com", "Asha", "Rao"))
        (dim,) = self.query("SELECT * FROM customers WHERE customer_id=?", identity["customer_id"])
        self.assertEqual((dim["account_status"], dim["acquisition_campaign"]), ("active", "google_signup"))
        self.assertIsNotNone(dim["terms_accepted_at"])

    def test_second_sign_in_reuses_the_customer(self):
        self.sign_in()
        response = self.sign_in()
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(len(self.query("SELECT * FROM customer_pii WHERE email='asha@example.com'")), 1)
        self.assertEqual(len(self.query("SELECT * FROM auth_identities")), 1)

    def test_existing_email_signs_in_to_that_customer(self):
        response = self.sign_in({**GOOGLE_USER, "email": "EXISTING@example.com"})
        self.assertEqual(response.headers["location"], "/")
        (identity,) = self.query("SELECT customer_id FROM auth_identities")
        self.assertEqual(identity[0], "cust_existing")
        (pii,) = self.query("SELECT first_name FROM customer_pii WHERE customer_id='cust_existing'")
        self.assertEqual(pii[0], "Asha")  # missing name filled from Google
        self.assertEqual(self.query("SELECT COUNT(*) FROM customers")[0][0], 2)

    def test_unverified_email_and_inactive_account_are_rejected(self):
        response = self.sign_in({**GOOGLE_USER, "email_verified": False})
        self.assertEqual(response.headers["location"], "/login?error=unverified")
        response = self.sign_in({**GOOGLE_USER, "email": "closed@example.com"})
        self.assertEqual(response.headers["location"], "/login?error=inactive")
        self.assertEqual(self.query("SELECT COUNT(*) FROM sessions")[0][0], 0)

    def test_cancelled_google_consent_returns_to_login(self):
        client = SimpleNamespace(google=SimpleNamespace(
            authorize_access_token=AsyncMock(side_effect=RuntimeError("access_denied"))))
        with patch.object(auth, "oauth", return_value=client):
            response = self.client.get("/auth/google/callback", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login?error=failed")

    def test_retired_sign_in_tables_are_dropped(self):
        tables = {row[0] for row in self.query("SELECT name FROM sqlite_master WHERE type='table'")}
        core._SCHEMA_CHECKED.clear()
        con = core.db()
        core.ensure_auth_tables(con)
        con.close()
        tables = {row[0] for row in self.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("auth_identities", tables)
        self.assertFalse({"supertokens_customers", "pending_signups"} & tables)

    # --------------------------------------------------------------- sessions
    def test_session_cookie_opens_the_account_and_logout_ends_it(self):
        self.sign_in()
        self.assertEqual(self.client.get("/account").status_code, 200)
        response = self.client.post("/logout", follow_redirects=False)
        self.assertTrue(response.headers["location"].startswith("/login"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM sessions")[0][0], 0)

    def test_expired_or_unknown_session_redirects_to_login(self):
        self.sign_in()
        con = core.db()
        con.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00'")
        con.commit()
        con.close()
        response = self.client.get("/account", headers={"accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login")
        self.client.cookies.set("sid", "made-up")
        response = self.client.post("/cart/add", data={"product_id": "prod_1"}, follow_redirects=False)
        self.assertEqual(response.status_code, 401)

    def test_cross_origin_mutation_is_rejected(self):
        self.sign_in()
        response = self.client.post("/account/profile", data={"first_name": "X"},
                                    headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    # --------------------------------------------------------------- profile
    def test_profile_saves_to_customer_dimension_and_pii(self):
        self.sign_in()
        response = self.client.post("/account/profile", follow_redirects=False, data={
            "first_name": "Asha", "last_name": "Rao", "phone": "98765 43210", "city": "Mumbai",
            "gender": "F", "age_bracket": "25-34", "heard_from": "Referral", "email_opt_in": "on",
            "home_address_line1": "12 Marine Drive", "home_postal_code": "400001", "home_country": "India",
        })
        self.assertEqual(response.status_code, 303)
        (row,) = self.query("SELECT c.*, p.* FROM customers c JOIN customer_pii p USING(customer_id) "
                            "WHERE p.email='asha@example.com'")
        self.assertEqual((row["signup_city"], row["signup_state"], row["signup_region"]),
                         ("Mumbai", "Maharashtra", "West"))
        self.assertEqual((row["gender"], row["age_bracket"], row["acquisition_channel"]), ("F", "25-34", "Referral"))
        self.assertEqual((row["email_opt_in"], row["sms_opt_in"]), (1, 0))
        self.assertEqual((row["phone"], row["home_address_line1"], row["home_postal_code"]),
                         ("+919876543210", "12 Marine Drive", "400001"))
        self.assertIn("profile_update", [r[0] for r in self.query("SELECT event_type FROM clickstream")])
        page = self.client.get("/account")
        self.assertIn('value="12 Marine Drive"', page.text)

    def test_invalid_or_duplicate_phone_is_rejected(self):
        con = core.db()
        con.execute("UPDATE customer_pii SET phone='+919876543210' WHERE customer_id='cust_existing'")
        con.commit()
        con.close()
        self.sign_in()
        bad = self.client.post("/account/profile", data={"first_name": "Asha", "phone": "123"})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("valid mobile number", bad.text)
        taken = self.client.post("/account/profile", data={"first_name": "Asha", "phone": "9876543210"})
        self.assertEqual(taken.status_code, 422)
        self.assertIn("already used", taken.text)
        self.assertIsNone(self.query("SELECT phone FROM customer_pii WHERE email='asha@example.com'")[0][0])

    def test_profile_requires_sign_in(self):
        response = self.client.post("/account/profile", data={"first_name": "X"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login")

    # --------------------------------------------------------------- health
    def test_healthcheck_requires_the_storefront_schema(self):
        self.assertEqual(json.loads(storefront.healthcheck().body), {"status": "ok"})
        self.path.unlink()
        self.assertEqual(storefront.healthcheck().status_code, 503)


class DevLoginAndConfigTests(unittest.TestCase):
    def test_dev_login_only_without_google_in_development(self):
        with patch.dict(os.environ, {"APP_ENV": "development", "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}):
            self.assertTrue(auth.dev_login_enabled())
        with patch.dict(os.environ, {"APP_ENV": "development", "GOOGLE_CLIENT_ID": "x", "GOOGLE_CLIENT_SECRET": "y"}):
            self.assertFalse(auth.dev_login_enabled())
        with patch.dict(os.environ, {"APP_ENV": "production", "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}):
            self.assertFalse(auth.dev_login_enabled())

    def test_production_requires_google_client_session_secret_and_https(self):
        base = {"APP_ENV": "production", "PUBLIC_APP_URL": "https://shop.example",
                "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret", "SESSION_SECRET": "s" * 40}
        with patch.dict(os.environ, base):
            auth.check_configuration()
        for name, value in (("GOOGLE_CLIENT_ID", ""), ("SESSION_SECRET", ""),
                            ("PUBLIC_APP_URL", "http://shop.example"), ("SESSION_SECRET", "short")):
            with patch.dict(os.environ, {**base, name: value}), self.assertRaises(RuntimeError):
                auth.check_configuration()

    def test_device_type_from_user_agent(self):
        self.assertEqual(auth.device_from_user_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"), "Mobile Web")
        self.assertEqual(auth.device_from_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), "Desktop Web")

    def test_customer_for_identity_is_exported_for_reuse(self):
        self.assertTrue(callable(auth_routes.customer_for_identity))


if __name__ == "__main__":
    unittest.main()
