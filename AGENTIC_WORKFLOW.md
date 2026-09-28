# Agentic approval workflow

The repository now has a backend domain layer for the approved architecture without
replacing the existing payroll, ChangeSet, audit, or Gemma client code.

## Local API

Set the database connection for the included PostgreSQL container:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://hr_agent:hr_agent_dev_only@localhost:5432/hr_agent"
docker compose up -d postgres
uvicorn api:app --app-dir src
```

The `POSTGRES_PASSWORD` in `docker-compose.yml` is development-only. Use a secret
manager in a deployed environment.

## Endpoints

- `POST /cases` creates `RECEIVED` and always records `case_id`, `changeset_id`, and `status`.
- `POST /cases/{case_id}/messages` persists the HR conversation, uses Gemma structured output to either ask a clarification or submit a plan, and records tokens automatically.
- `POST /cases/{case_id}/plans` validates the structured plan and moves to `REVIEW`.
- `POST /cases/{case_id}/plan-decision` is the HR web approval gate.
- `POST /cases/{case_id}/runs` only works after approval.
- `POST /cases/runs/{run_id}/finish` only moves a case to `DONE` when all approved plan steps are complete.
- `POST /cases/{case_id}/token-usage` and `GET /cases/{case_id}/token-report` provide partner-facing usage measurements.

For this demo, `X-User-Id` is an authentication placeholder. Replace it with the
web application's real identity/session middleware before deployment.

## Cloudflare boundary

Cloudflare Pages hosts the HR UI. A Cloudflare Worker calls this API, and a
Cloudflare Workflow waits for the approval event before requesting the ReAct run.
PostgreSQL remains the authoritative audit/state store. The Python domain layer is
not duplicated in TypeScript; the Worker is only an adapter around these endpoints.

## Gemma structured output

`GemmaAPICompletionClient.complete_structured()` sends
`generationConfig.responseMimeType = "application/json"` and
`generationConfig.responseJsonSchema`. It returns parsed JSON plus provider
`usageMetadata` token counts. Pydantic validates the result again before the plan
or a tool action is accepted.
