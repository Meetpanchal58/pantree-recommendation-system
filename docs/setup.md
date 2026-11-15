# Setup

Install, get the data, set up Google sign-in, start the shop and host it (Google Cloud Run + Neon).

## How to run

### What you need

- Windows, macOS, or Linux; the commands below are for Windows PowerShell.
- Python 3.11 or newer and an internet connection to install Python packages.
- For Google sign-in: a Google Cloud project with an OAuth client (see
  [Google sign-in](#google-sign-in)). Without one, local development offers a developer-only
  sign-in form instead.

### 1. Install Pantree

Open PowerShell in the folder containing this README, then run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The recommendation packages include machine-learning libraries and can take a while to install.
You only need to install them once for this environment.

### 2. Get the shop's data

From the same project folder, run:

```powershell
.\.venv\Scripts\python.exe prepare_data.py
```

This downloads the shop's history (catalogue, customers, browsing, orders, stock and more, from
1 January 2025 to yesterday) from the [ecommerce-simulator](https://github.com/Meetpanchal58/ecommerce-simulator)
package, which refreshes it every night, and converts it for Pantree. It takes about a minute.
Use `--size small` for a lighter shop on a laptop (`small`, `medium`, `full` (default) or
`large`). Run the same command again later to get the latest days. See [data.md](data.md).

### 3. Create the storefront database

```powershell
.\.venv\Scripts\python.exe webapp\seed_db.py
```

This loads products, customers and starting stock into the live shop database. Locally that is
the SQLite file `webapp\pantree_app.db`, which **seeding replaces**. With `DATABASE_URL` set to a
Postgres database (as on the hosted site), it seeds only an empty database and keeps existing
activity; `--reset` starts over.

### 4. Settings

Copy the example settings file:

```powershell
Copy-Item .env.example .env
notepad .env
```

For a first local run you can leave everything as it is: with `APP_ENV=development` and no Google
client, the sign-in page shows a **Developer sign-in** form (name + email) that creates or opens
an account without Google. It is never shown when `APP_ENV=production` or when a Google client
is configured. To test the real Google button locally, follow [Google sign-in](#google-sign-in)
and add `http://127.0.0.1:8000/auth/google/callback` as a redirect URI.

### 5. Start the shop

```powershell
.\.venv\Scripts\python.exe -m uvicorn webapp.app:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/login`. If you use another port, set `PUBLIC_APP_URL` to that origin
(for example `http://127.0.0.1:8013`). On a small machine set `PANTREE_HEAVY_RECOMMENDATIONS=0`
to skip the collaborative-filtering and sentence-transformer models.

### 6. Try the shop

1. Click **Continue with Google**. If your Google email already belongs to a Pantree customer,
   you are signed in to that account; otherwise a new account is created from your Google name
   and email and you land on **Your account** to add the rest of your details.
2. In **Your account → Personal details**, add your mobile number, city, home address, gender,
   age range, how you heard about Pantree and your marketing choices, then **Save profile**.
   The changes are written straight to the live `customers` and `customer_pii` tables, and the
   address prefills checkout.
3. Browse departments, search, open a product, add products to your cart or wishlist, and try a
   checkout.
4. Visit **Your orders** to mark the demo order delivered or cancel it. After marking it
   delivered, you can also try a return or replacement.
5. Use **Sign out** from the account menu when you are finished.

Checkout is a demonstration: it does not charge a real card. It creates a paid demo order in
**Placed** status. In **Your orders**, mark it delivered to try a return or replacement, or cancel
it to see the payment marked refunded and the items returned to stock. Sold-out items can be
restocked from **Your account → Demo tools**.

### Getting newer data

`python prepare_data.py` always downloads the latest published days. It refreshes the files the
recommender and the notebook read; it does not touch the live shop database.

## Google sign-in

The storefront signs shoppers in with Google (OAuth 2.0 / OpenID Connect, via
[Authlib](https://docs.authlib.org/)); there are no passwords or one-time codes.

### How it works

- `GET /auth/google/login` sends the browser to Google's consent screen (scopes
  `openid email profile`). The OAuth `state` is kept in a signed, 10-minute cookie
  (`pantree_oauth`, signed with `SESSION_SECRET`).
- `GET /auth/google/callback` exchanges the code, checks the ID token and requires a **verified**
  email. Then:
  - the Google account is already linked in `auth_identities` → that customer;
  - otherwise a customer whose `customer_pii.email` matches is linked (existing shoppers keep
    their history);
  - otherwise a new customer is created (`customers` + `customer_pii`) and sent to the profile
    page with a welcome message.
- Pantree then creates its own session: a random 256-bit ID in an HttpOnly, SameSite=Lax cookie
  (`sid`, Secure on HTTPS) and a row in `sessions` that expires after 7 days. Sign-out deletes
  the row. Every protected page and API checks the session; data operations use that session's
  customer ID, never one supplied by the browser.
- Unsafe requests (POST, ...) with an `Origin`/`Referer` must match `PUBLIC_APP_URL`; production
  also rejects unsafe requests without one.
- Customers whose `account_status` is not `active` cannot sign in.

### Create the OAuth client

1. In [Google Cloud console](https://console.cloud.google.com/) pick your project, then
   **APIs & Services → OAuth consent screen**: user type **External**, app name "Pantree",
   your support email, scopes `openid`, `email`, `profile`. While the app is in *Testing*, add the
   Google accounts that may sign in as test users, or **Publish** the app so anyone can.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application**.
3. Authorised JavaScript origins: your site's origin (for example
   `https://pantree-storefront-123456789.asia-southeast1.run.app`, and `http://127.0.0.1:8000`
   for local testing).
4. Authorised redirect URIs: `<origin>/auth/google/callback` for each origin above.
5. Copy the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (in `.env`
   locally; in Secret Manager for Cloud Run, below). Never commit them.

## Hosting: Google Cloud Run + Neon Postgres

The storefront runs as a container on Cloud Run (scales to zero, so it fits the free tier). The
shop's data is baked into the image at build time; everything people do on the site (accounts,
profiles, carts, orders, clicks, impressions) is stored in a Postgres database such as Neon, so it
survives restarts and redeploys.

### One-time setup

1. **Database.** Create a free project at neon.tech (pick a region near your Cloud Run region,
   e.g. Singapore for `asia-southeast1`), click **Connect**, keep **Connection pooling** on and
   copy the connection string.
2. **OAuth client.** Create it as in [Create the OAuth client](#create-the-oauth-client). You can
   add the Cloud Run origin after the first deploy; the script prints it.
3. **Secrets.** Open Cloud Shell in your project and store the three values in Secret Manager:

   ```bash
   printf '%s' 'postgresql://...'        | gcloud secrets create database-url --data-file=-
   printf '%s' 'xxxx.apps.googleusercontent.com' | gcloud secrets create google-client-id --data-file=-
   printf '%s' 'GOCSPX-...'              | gcloud secrets create google-client-secret --data-file=-
   ```

   `session-secret` is generated by the deploy script if it does not exist.

### Deploy

From the repository folder in Cloud Shell:

```bash
bash scripts/deploy_cloudrun.sh
```

The script:

1. enables Cloud Run, Cloud Build, Artifact Registry and Secret Manager;
2. creates a service account that can read only the four secrets, and an Artifact Registry
   repository;
3. builds the image with Cloud Build (`Dockerfile`: installs `requirements.txt` with CPU-only PyTorch, downloads
   the small shop with six months of browsing, and drops the raw source tables);
4. runs `webapp/seed_db.py` once as a Cloud Run **job** against the database: it fills an empty
   database and, on later deploys of the same shop, keeps all live activity and only adds new
   products, customers and stock (`SKIP_SEED=1` skips it);
5. deploys the service (`min-instances 0`, `max-instances 2`, 1 vCPU, 1 GiB,
   `APP_ENV=production`, `PANTREE_HEAVY_RECOMMENDATIONS=0`, `PANTREE_LIVE_MIRROR=0`) with
   `PUBLIC_APP_URL` set to the service's `https://SERVICE-PROJECTNUMBER.REGION.run.app` address;
6. prints the URL and the redirect URI to add to the OAuth client.

Overrides: `PROJECT`, `REGION` (default `asia-southeast1`), `SERVICE` (default
`pantree-storefront`), `REPO`, and `PUBLIC_APP_URL` for a custom domain. Re-run the script to
deploy a new version; a rebuilt image also picks up the latest nightly data.

Without `DATABASE_URL` the app uses SQLite (local development and most tests).
`webapp/database.py` lets the same SQL run on both: it converts `?` placeholders, makes LIKE
case-insensitive as in SQLite, and pools connections (Neon's pooler does not support prepared
statements, so they are turned off). The Neon free plan has 1 GB of storage; a demo site uses a
small fraction of it.

### Automatic deploys

A Cloud Build trigger deploys every push to `main`: it runs `cloudbuild.yaml`, which builds the
image, pushes it to Artifact Registry and rolls it out to the existing Cloud Run service.
Environment variables, secrets, scaling and the service account stay as
`scripts/deploy_cloudrun.sh` set them; only the image changes. Run the script by hand when those
settings change, or to re-seed the database with newly published products.

One-time setup, after `scripts/deploy_cloudrun.sh` has run once (all in the Google Cloud console):

1. **Cloud Build → Settings → Service account permissions:** for the default service account
   shown there, set **Cloud Run Admin** and **Service Account User** to **Enabled**.
2. **Cloud Build → Triggers → Connect repository:** choose **GitHub (Cloud Build GitHub App)**,
   authenticate, install the app on this repository only, select it and click **Connect**.
3. **Create trigger:** name `deploy-main`, region `global`, event **Push to a branch**, branch
   `^main$`, configuration **Cloud Build configuration file (yaml or json)**, location
   **Repository**, file `/cloudbuild.yaml`. Leave the service account as the default from step 1.
   Click **Create**.
4. Click **Run** next to the trigger once to test it. Builds appear under **Cloud Build → History**
   (about 15 minutes), and the push to `main` deploys by itself from then on.

Cloud Build's free tier includes 2,500 build-minutes a month on the default machine.

### Reset my recommendations

The account page has a **Reset recommendations** button. It deletes the customer's
clickstream, recommendation impressions and clicks, cart, wishlist, orders and returns, and
their other login sessions; it keeps the account, the profile, the Google link and the current
login. Units of open (not yet delivered) orders go back into stock with an `ACCOUNT_RESET`
inventory movement. `customers.recommendations_reset_at` is set, so serving ignores anything stored
about the customer before the reset and personalises again from new activity.

### Before opening the shop to the public

Publish the OAuth consent screen (or add test users), check that the redirect URI matches the
deployed origin exactly, and test a real sign-in, a profile save and the main shopping actions on
the public site. Neon can restore the database to an earlier point within its history window (see
Neon's plan limits).

Back to the [README](../README.md).
