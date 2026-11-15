#!/usr/bin/env bash
# One-time setup so GitHub Actions can deploy to Cloud Run without a stored key
# (Workload Identity Federation). Run in Google Cloud Shell from the repository folder,
# after scripts/deploy_cloudrun.sh has run once:
#   bash scripts/setup_github_deploy.sh
#
# Creates:
#   - service account github-deployer, with only the roles a deploy needs;
#   - workload identity pool "github" and provider "github-actions" that trust GitHub's
#     OIDC tokens only for this repository's main branch.
# Prints the four values to save as GitHub repository variables.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || { echo "Set PROJECT=<your-gcp-project-id> or run: gcloud config set project <id>"; exit 1; }
GITHUB_REPO="${GITHUB_REPO:-$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')}"
POOL="github"
PROVIDER="github-actions"
DEPLOYER_NAME="github-deployer"
DEPLOYER="${DEPLOYER_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="pantree-storefront@${PROJECT}.iam.gserviceaccount.com"
gcloud config set project "$PROJECT" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')"
# Cloud Build runs builds as one of these, depending on when the project was created.
BUILD_SAS="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com ${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

echo "Project: $PROJECT ($PROJECT_NUMBER)   GitHub repository: $GITHUB_REPO"
gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1 || {
  echo "Run 'bash scripts/deploy_cloudrun.sh' once first (it creates $RUNTIME_SA)."; exit 1; }

echo "Enabling IAM credential APIs ..."
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com >/dev/null

echo "Creating the deploy service account ..."
gcloud iam service-accounts describe "$DEPLOYER" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$DEPLOYER_NAME" --display-name "GitHub Actions deployer (Pantree)"

# viewer: read project/secret metadata and stream Cloud Build logs.
for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/storage.admin roles/serviceusage.serviceUsageConsumer roles/viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$DEPLOYER" \
    --role "$role" --condition None >/dev/null
done
# May run the app and Cloud Build as their own service accounts (and no others).
for sa in "$RUNTIME_SA" $BUILD_SAS; do
  gcloud iam service-accounts describe "$sa" >/dev/null 2>&1 || continue
  gcloud iam service-accounts add-iam-policy-binding "$sa" --member "serviceAccount:$DEPLOYER" \
    --role roles/iam.serviceAccountUser >/dev/null
done

echo "Creating the workload identity pool and GitHub provider ..."
gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --display-name "GitHub ${GITHUB_REPO}" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" >/dev/null

PROVIDER_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo
echo "Done. In GitHub: ${GITHUB_REPO} -> Settings -> Secrets and variables -> Actions -> Variables tab,"
echo "add these four repository variables (they are not secret):"
echo
echo "  GCP_PROJECT_ID                  $PROJECT"
echo "  GCP_PROJECT_NUMBER              $PROJECT_NUMBER"
echo "  GCP_WORKLOAD_IDENTITY_PROVIDER  $PROVIDER_NAME"
echo "  GCP_DEPLOY_SERVICE_ACCOUNT      $DEPLOYER"
