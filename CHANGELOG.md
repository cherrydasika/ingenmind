# Changelog

Each release's full notes, generated from its merged PRs, are on
[GitHub Releases](https://github.com/cherrydasika/ingenmind/releases).
This file has the highlights and anything you must do by hand when
upgrading ([Upgrading](docs/upgrading.md)).

## 1.0.0

The first stable release. From here on, versions follow semantic
versioning: minor releases keep your settings, data and API working.

**Highlights**

- **Setup with agents.** A guided setup plans the knowledge base with you,
  then finds and labels sources and builds a question set to evaluate it.
  It shows readiness before going live.
- **Multi-agent answers with checks.** Each question goes through
  guardrails, retrieval with hybrid search and a reranker, and a team of
  agents. Every answer is checked and cited before anyone sees it, and the
  agents research when the knowledge base falls short.
- **Flow builder.** Live, draft and versioned agent workflows, with
  evaluations, nightly runs and a promotion gate.
- **Channels and integrations.**
    - Channels: a web widget, Slack and Teams.
    - Public API with API keys, and agent-to-agent (A2A).
    - MCP (both ways) and OpenAPI tools.
    - Webhooks, plus file, S3 and MinIO connectors.
- **Operations.**
    - Operations dashboard with cost per model and service levels with error
      budgets.
    - Alerts, the answer cache, budgets and usage limits.
    - Analytics, with an export for DuckDB.
- **Data you control.**
    - Every action is recorded in an audit log.
    - People can download or delete their own data, and retention is set per
      kind of data.
    - It is self-hosted and runs on Anthropic, OpenAI, Bedrock or Ollama.
- **Releases.**
    - Signed multi-arch images (amd64, arm64) with an SBOM and provenance.
    - A one-command installer that backs up before upgrading.
    - The database is migrated on start, and a downgrade is refused.
    - A docs site.
- **Checked before release.** A security review and a pentest of the
  deployed site (ZAP active and API scans, signed in, and manual checks).
  A restore test of a backup, and a measured capacity: one 4-vCPU server
  serves about 5 questions a second.

**Upgrade notes** (from a 0.1.0 pre-release)

- Run the installer again (it backs up the database first). The first
  start migrates the database.
- New settings are optional, with working defaults: `WEB_THREADS` and
  `SLO_*` (see `.env.example`).
