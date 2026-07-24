# GitLab deployment

CI/CD follows the corporate runner and registry layout used by
`ai-agent-loskutov`.

## Delivery routes

| Source | Registry image | Runner | Environment |
|---|---|---|---|
| `develop` | `${CI_REGISTRY_IMAGE}/dev:$CI_COMMIT_SHORT_SHA` | `group-deploy-shell` | `development` |
| tag `v*` | `${CI_REGISTRY_IMAGE}/prod:$CI_COMMIT_TAG` | `server-a-shell` | `production` |

Production deployment is manual. The project does not deploy `main`, because
that branch currently contains the separate n8n history.

The build job runs on `build-did`, publishes immutable and `latest` tags, and
uses the corporate S3 BuildKit cache.

## Required GitLab variables

| Variable | Type | Scope | Purpose |
|---|---|---|---|
| `ENV_DEV` | File | development | Runtime environment based on `.env.host.example` |
| `ENV_PROD` | File | production | Production runtime environment |
| `GOOGLE_SERVICE_ACCOUNT_DEV` | File | development | Google service-account JSON |
| `GOOGLE_SERVICE_ACCOUNT_PROD` | File | production | Production Google service-account JSON |
| `DEV_ENVIRONMENT_URL` | Variable | development | URL shown in GitLab Environments |
| `PROD_ENVIRONMENT_URL` | Variable | production | Production URL shown in GitLab Environments |
| `YC_S3_ACCESS_KEY` | Variable | all | Corporate S3 build-cache access key |
| `YC_S3_SECRET_KEY` | Variable | all | Corporate S3 build-cache secret |
| `YC_S3_ENDPOINT_URL` | Variable | all | Corporate S3 endpoint |
| `YC_S3_BUCKET_NAME` | Variable | all | Corporate S3 bucket |

Secret variables must be masked where GitLab supports masking. Production
variables and `v*` tags must be protected. File-variable contents must not be
printed in job logs.

`ENV_DEV` and `ENV_PROD` contain all application secrets, database credentials,
host binding and service configuration. `DOCKER_IMAGE` is assigned by the
pipeline and does not need to be stored in either File variable.

## Runner requirements

- `dev`: container jobs for Python, Docker CLI and Structurizr quality gates;
- `build-did`: Docker socket, buildx, registry access and S3 access;
- `group-deploy-shell`: Docker Engine and Compose plugin on the development host;
- `server-a-shell`: Docker Engine and Compose plugin on the production host.

Deploy runners retain `deploy/.env` and
`deploy/secrets/google-service-account.json` on their host with mode `0600`.
The paths are ignored by Git and excluded from the Docker build context.

## Deployment behavior

One immutable application image runs the migration, API, worker and beat
services. PostgreSQL and Redis use persistent Compose volumes. Deployment waits
for healthy services and then verifies `/health/ready` from inside the API
container.

For manual inspection on a deploy host:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.host.yml -p restaurant-bot-dev ps
docker compose --env-file deploy/.env -f deploy/docker-compose.host.yml -p restaurant-bot-dev logs --tail=200
```
