# Security model

What IngenMind protects, from whom, and where each risk is handled. Every gap
names the roadmap issue that closes it (R-numbers, tracker
[#120](https://github.com/cherrydasika/ingenmind/issues/120)). Keep this file
up to date when a change moves a risk. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).

Last reviewed: 2026-10-21 (R02, #81).

## What we protect

| Asset | Where it lives | Why it matters |
|---|---|---|
| The knowledge base | PostgreSQL `rag_chunks`, `research_chunks` | Answers are only as good as what is in it; poisoning it misleads every user |
| People's questions and answers | `sessions` (30 days), Langfuse traces | May contain personal data |
| Flows, versions and the live flow | PostgreSQL `flows*` | Decide how every question is answered, which tools run and which checks apply |
| Users, roles and permissions | PostgreSQL `users` | Who may change the knowledge, flows and settings |
| Secrets | `.env` locally; SSM parameter `/rag-systems/prod/langfuse-env` on EC2 | Model provider keys, OIDC client secret, session secret, Langfuse keys, database passwords |
| Spend | Model providers, Tavily, AWS | Every question costs tokens; research and evaluations cost more |
| The host | EC2 instance, Docker | Reaches the database, Langfuse and the AWS instance role |

## Who we protect it from

- **A signed-in user** with fewer permissions than they want: may try other routes, other people's sessions, or to make the agents do something out of scope.
- **Someone who is not signed in**: today they can only reach the sign-in page (EC2 is private behind SSM; public access is R09, #23).
- **A malicious web page or document**: found by the research agent, picked as a source, or pasted as an ad-hoc document. It may carry hidden instructions (indirect prompt injection) or false facts (poisoning).
- **A compromised or misbehaving tool or provider**: an external API returns hostile content, or a model provider is down.
- **A stolen session cookie or API key** (API keys arrive with R46, #106).
- **A supply-chain compromise**: a Python or npm dependency, a base image, a GitHub Action.

## Trust boundaries

```mermaid
flowchart LR
    B([Browser]) -- session cookie --> W[Web app]
    IDP([Identity provider]) -- OIDC --> W
    W --> DB[(PostgreSQL + pgvector)]
    W --> LF[Langfuse]
    W -- prompts --> M([Model providers])
    W -- search --> T([Tavily])
    W -- fetch --> WEB([Public web])
    W -- calls --> API([External APIs])
    WK[Ingestion worker] --> DB
    WK -- fetch --> WEB
```

Everything to the right of the web app and worker is outside our control.
Text coming back from models, web pages, tools and documents is **data,
never instructions**, and is checked before it reaches a user or the
knowledge base.

## The OWASP Top 10 for LLM applications (2025)

| # | Risk | Handled today | Gap and the issue that closes it |
|---|---|---|---|
| LLM01 | **Prompt injection** | Input guardrail on every question, fail closed, whatever the flow (`app/guardrails.py`); research guardrail on every researched page; the answer evaluator judges answers only against the evidence | Instructions hidden in chunks and researched pages are not detected or fenced off as data: **R12 (#87)**. A fixed red-team set run on every publish: **R14 (#89)** |
| LLM02 | **Sensitive information disclosure** | Traces are public-safe (no prompts, drafts or tool inputs reach the browser); sessions are visible to their owner and `view_sessions` only; agent memory learns from outcomes, never from anyone's words | Personal data in questions reaches Langfuse, sessions and possibly web search: **R11 (#86)**. Retention and per-user export/delete: **R59 (#115)** |
| LLM03 | **Supply chain** | Dependencies bounded by major version; images built in CI from the reviewed commit; deploys only from `main` through an OIDC role limited to one SSM document | No vulnerability, image or secret scanning; Actions pinned by tag, not SHA; the web image runs as root: **R04 (#82)** |
| LLM04 | **Data and model poisoning** | Nothing enters the knowledge base without an admin's approval in setup; every chunk traces to its approval ("Why is this here?"); the source validator scores authority, freshness, consistency and relevance | Research during users' questions still ingests automatically (the user's choice for now); approving researched pages before they are added: **R32 (#100)**. Injection scanning at ingestion: **R12 (#87)** |
| LLM05 | **Improper output handling** | Model text is escaped before any markup is added (`markdown()` and `richText()` in `web/static/js/ui.js`); links only for `http(s)`; nothing a model writes is run as code or SQL (parameterised queries) | No Content Security Policy to contain a future escaping bug: **R05 (#83)** |
| LLM06 | **Excessive agency** | Every tool today is read-only (knowledge-base search, web search, fetch, weather, timetables, entry requirements); an agent only gets the tools plugged into it in the flow; flows are validated and versioned, and only promoting a version changes what users get | No per-tool argument checks, budgets or approvals for tools with side effects (needed before MCP and OpenAPI tools): **R13 (#88)**, then **R38 (#104)**, **R39 (#105)** |
| LLM07 | **System prompt leakage** | Prompts and drafts stay server-side; the output guardrail refuses answers that reveal instructions | Covered by the red-team set: **R14 (#89)** |
| LLM08 | **Vector and embedding weaknesses** | One knowledge base per deployment, so no cross-tenant retrieval; embeddings never leave the server | When there are several collections or workspaces, retrieval must respect who may see what: **R35 (#102)**, **R60 (#116)** |
| LLM09 | **Misinformation** | Every answer is judged for correctness, faithfulness, completeness and citations against its evidence; a failing answer is never shown; "not available" is preferred to a guess; readiness and evaluations before going live | Scheduled evaluations and a regression gate on promotion: **R56 (#114)**. User feedback: **R51 (#111)** |
| LLM10 | **Unbounded consumption** | Agent turn limits, two supervisor rounds, a research budget per question; flow size limits | No per-user rate limits or quotas: **R06 (#84)**. No hard token, time and tool-call budget per run: **R15 (#90)** |

## The web application

| Area | Handled today | Gap |
|---|---|---|
| **Sign-in** | OIDC (Cognito on EC2) with PKCE, state and nonce; invite-only; the app stores no passwords; dev sign-in answers only on localhost and is refused when `RAG_DEPLOYMENT=aws` | — |
| **Authorisation** | Every `/api` route names the permission it needs (`auth.RULES`), checked in middleware; users are deactivated, never deleted | — |
| **Dangerous actions** | Starting over needs a fresh sign-in (`/auth/confirm`, password asked again, valid once for 5 minutes) | — |
| **Session cookie** | Signed, HttpOnly, SameSite=Lax, Secure when `SESSION_COOKIE_SECURE`; at most 12 hours | Shorter idle timeout for admins; CSRF token or Origin check on state-changing routes; security headers and CSP: **R05 (#83)** |
| **Accountability** | Setup records each step with who and when | An append-only audit log of every admin action: **R07 (#85)** |
| **Server-side request forgery** | **Since R02:** the scraper (setup, ingestion, research) fetches only public addresses and checks every redirect; `FETCH_ALLOW_PRIVATE=true` allows private networks for an intranet, never loopback, link-local (cloud metadata) or reserved addresses. On EC2 the metadata service also needs IMDSv2 tokens | DNS rebinding between the check and the connection is not prevented (low risk: the metadata service needs a token, and the database is not HTTP) |

## The deployment

| Area | Handled today | Gap |
|---|---|---|
| **Network** | EC2 has no inbound access; people reach it through SSM port forwarding; the web app binds to 127.0.0.1 | Public access with HTTPS and a WAF: **R09 (#23)** |
| **Secrets** | On EC2 the release's `.env` comes from an SSM parameter; `DEPLOYMENT.local.md` and `.env` are git-ignored | Langfuse secrets match the local development `.env`: **R03 (#21)**. Secret scanning in CI: **R04 (#82)** |
| **Deploys** | Only merged `main`, through GitHub OIDC roles that trust only the `production` environment and may run one SSM document | — |
| **Backups** | Data on a separate EBS volume | Snapshots, a schedule and a restore test: **R08 (#22)** |
| **Monitoring** | Langfuse traces; per-flow metrics | Alerts on errors, cost and quality; CloudWatch alarms: **R55 (#113)** |

## Reviewing a change

Ask of every pull request that touches these areas:

1. Does text from a model, page, document or tool reach `innerHTML`, a shell, SQL or a URL without escaping or validation?
2. Does a new route name its permission in `auth.RULES`?
3. Does a new tool have side effects? Then it needs R13's approval path.
4. Does anything new fetch a URL? Use the scraper (`common.scraping`), which checks addresses.
5. Does it store personal data? Then it needs a retention period and must be covered by export and delete.
