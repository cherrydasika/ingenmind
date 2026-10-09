# Notes for coding agents

Read these first when picking the work up, whichever model or tool you are:

1. [PROGRESS.md](PROGRESS.md) — what is done and open, and the current
   state of the EC2 deployment.
2. The active plan, if any, in [docs/plans/](docs/plans/) (PROGRESS.md
   says which). Finished: research data-source discovery (issue #8);
   identity work, nearly done:
   [docs/plans/identity-sessions-memory.md](docs/plans/identity-sessions-memory.md). Its status line says which
   phase is in progress and what the user must decide; its checkboxes say
   what is done.
3. [docs/BACKLOG.md](docs/BACKLOG.md) — an index of the open GitHub
   issues, which are the backlog; the user asks for items by number or name.
4. [README.md](README.md) — how the app works; "Development" has the test
   commands.

## Working agreement with the user

- Work in phases and stop at each checkpoint for the user's confirmation.
- Update the active plan and PROGRESS.md at the end of every step, so the
  next agent can continue from them.
- Commit and push only when the user asks.
- Never push to `main`: work on a branch and open a pull request
  (`gh pr create`); CI runs on every pull request. The user merges.

## Tests

```bash
# The tests assume CI's defaults (Bedrock providers, 256-dimension vectors),
# whatever .env sets for running the app locally.
docker compose run --rm --no-deps -v "$PWD/data:/data:ro" \
  -e EMBEDDING_PROVIDER=bedrock -e LLM_PROVIDER=bedrock \
  -e PYTHONPATH=/app:/app/dags -e WEB_DIR=/web webapp \
  python -m unittest test_agent_runtime test_demo test_llm test_embeddings test_pg_store \
    test_evidence test_research test_answer_eval test_guardrails test_flows test_api_tools test_auth test_sessions test_agent_memory test_users test_setup
npm test --prefix web/flows-app   # after editing web/flows-app/src: npm run build --prefix web/flows-app
# The pages are ES modules: check them as modules (a plain `node --check` misses
# errors), as CI does. One syntax error stops the whole app loading.
for file in web/static/js/*.js; do node --input-type=module --check < "$file" || echo "$file"; done
```

The opt-in live check of data-source research (Tavily, the chat model,
real pages) runs on EC2 in the web app container:
`RUN_LIVE=1 PYTHONPATH=/app:/app/dags python -m unittest -v test_research_live`.
The same for Domain Blueprint research (`test_blueprint_live`, UK trains and
flights); it also runs locally when `.env` has `TAVILY_API_KEY`.

The Python tests need the local `pgvector` container running. Leave it
running afterwards. For running the app locally, `.env` sets
`LLM_PROVIDER=anthropic` (with `ANTHROPIC_API_KEY`) and
`EMBEDDING_PROVIDER=local`, as `.env.example` does.

## Deploying to EC2

The identifiers of the maintainer's own deployment (instance, account,
Cognito, harness) are not in the repository: they are in
`DEPLOYMENT.local.md` (git-ignored) in the maintainer's checkout. Read it
for the real values behind `<instance-id>`, `<account-id>` and the like.
Publishing from this repository needs its GitHub Actions variables and the
AWS publish role's trust of this repository (see PROGRESS.md).

1. The user merges a pull request into `main`; CI ("Terraform and app
   checks") runs on the merge. When it passes, the publish workflow
   uploads that commit to S3 by itself
   (check: `gh run list --workflow publish-source.yml`).
2. A commit CI skipped (docs only) is published by hand, and only the
   user does that (agents are not allowed to):
   `gh workflow run publish-source.yml --ref main -f confirmation=PUBLISH`
3. Deploy that commit through Systems Manager (profile `personal`,
   region `eu-west-2`, instance `<instance-id>`):
   `sudo bash /opt/rag-systems/current/scripts/aws/deploy.sh <full SHA>`
4. `scripts/aws/session.sh start | stop | status` turns EC2 and its stack
   on and off; stop it when done, it bills by the hour.
