#!/usr/bin/env bash
# Build and deploy the Pantree storefront to Google Cloud Run (free-tier friendly settings).
#
# Run in Google Cloud Shell (or anywhere with gcloud) from the repository folder:
#   bash scripts/deploy_cloudrun.sh
#
# Secrets in Secret Manager (create them once; see docs/setup.md):
#   database-url          Neon (or any Postgres) connection string        required
#   google-client-id      OAuth client ID ("Web application")             required
#   google-client-secret  OAuth client secret                             required
#   session-secret        signs the OAuth state cookie                    created here if missing
#
# Optional overrides: PROJECT, REGION, SERVICE, REPO, PUBLIC_APP_URL (custom domain), SKIP_SEED=1.
# DEPLOY_ONLY=1 skips the one-time setup (APIs, service account, secret access, image repository).
# Day-to-day deploys from main run through cloudbuild.yaml (a Cloud Build trigger); see docs/setup.md.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || { echo "Set PROJECT=<your-gcp-project-id> or run: gcloud config set project <id>"; exit 1; }
REGION="${REGION:-asia-southeast1}"   # Singapore, next to Neon's Singapore region; change to match your database
SERVICE="${SERVICE:-pantree-storefront}"
JOB="${SERVICE}-seed"
REPO="${REPO:-pantree}"
SA_NAME="pantree-storefront"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/storefront:${TAG}"
gcloud config set project "$PROJECT" >/dev/null

DEPLOY_ONLY="${DEPLOY_ONLY:-0}"
if [ "$DEPLOY_ONLY" != "1" ]; then
  echo "Enabling the required Google Cloud APIs ..."
  gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com >/dev/null
fi

has_secret() { gcloud secrets describe "$1" >/dev/null 2>&1; }
for s in database-url google-client-id google-client-secret; do
  has_secret "$s" || { echo "Missing secret '$s'. Create it first, e.g.:"; \
    echo "  printf '%s' 'VALUE' | gcloud secrets create $s --data-file=-"; exit 1; }
done
if [ "$DEPLOY_ONLY" = "1" ]; then
  has_secret session-secret || { echo "Missing secret 'session-secret'. Run the script once without DEPLOY_ONLY."; exit 1; }
elif ! has_secret session-secret; then
  echo "Creating secret 'session-secret' ..."
  head -c 48 /dev/urandom | base64 | tr -d '\n=/+' | gcloud secrets create session-secret --data-file=- >/dev/null
fi

if [ "$DEPLOY_ONLY" != "1" ]; then
  # Service account the app runs as: may read its four secrets, nothing else.
  gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$SA_NAME" --display-name "Pantree storefront (Cloud Run)"
  for s in database-url google-client-id google-client-secret session-secret; do
    gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" \
      --role roles/secretmanager.secretAccessor >/dev/null
  done

  gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
      --description "Pantree container images" >/dev/null
fi

if gcloud artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "Image $IMAGE already built; skipping the build."
else
  echo "Building $IMAGE with Cloud Build (downloads the shop's data; a few minutes) ..."
  gcloud builds submit --tag "$IMAGE" --region "$REGION" .
fi

# The public origin is known before the first deploy: https://SERVICE-PROJECTNUMBER.REGION.run.app
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')"
PUBLIC_APP_URL="${PUBLIC_APP_URL:-https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app}"

# Seed the live database (fills an empty database; on later deploys only adds new products,
# customers and stock and keeps all activity). Runs as a one-off Cloud Run job.
if [ "${SKIP_SEED:-0}" != "1" ]; then
  echo "Seeding the live database with a Cloud Run job ..."
  gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
    --service-account "$SA" --command python --args webapp/seed_db.py \
    --set-env-vars "PANTREE_LIVE_MIRROR=0" \
    --set-secrets "DATABASE_URL=database-url:latest" \
    --memory 2Gi --cpu 1 --task-timeout 1800 --max-retries 0 >/dev/null
  gcloud run jobs execute "$JOB" --region "$REGION" --wait
fi

SECRETS="DATABASE_URL=database-url:latest,GOOGLE_CLIENT_ID=google-client-id:latest"
SECRETS+=",GOOGLE_CLIENT_SECRET=google-client-secret:latest,SESSION_SECRET=session-secret:latest"
ENV_VARS="APP_ENV=production,PUBLIC_APP_URL=${PUBLIC_APP_URL},PANTREE_LIVE_MIRROR=0,PANTREE_HEAVY_RECOMMENDATIONS=0"

# min-instances 0: scales to zero (free tier). Sessions live in Postgres, so several instances are fine.
echo "Deploying $SERVICE ..."
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 0 --max-instances 2 --concurrency 40 \
  --cpu 1 --memory 1Gi --cpu-throttling --cpu-boost --timeout 120 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS"

echo
echo "Live at: $PUBLIC_APP_URL/login"
echo "(Use this address: sign-in cookies and the OAuth redirect are tied to it.)"
echo
echo "In Google Cloud console -> APIs & Services -> Credentials -> your OAuth client, make sure:"
echo "  Authorised JavaScript origin:  $PUBLIC_APP_URL"
echo "  Authorised redirect URI:       $PUBLIC_APP_URL/auth/google/callback"
