# agentcore-kb

A single AI agent on **Amazon Bedrock AgentCore**: a Harness running Claude
Haiku 4.5 whose one tool, `search_knowledge_base`, is an MCP server over the
RAG app's PostgreSQL pgvector database on EC2.

```
invoke.py ──▶ AgentCore Harness (Claude Haiku 4.5)
                 └─ tool ──▶ AgentCore Gateway ──▶ MCP server on AgentCore Runtime
                                                     └─▶ pgvector rag_chunks (EC2)
```

| Path | What it is |
|---|---|
| `mcp_server/server.py` | MCP server with `search_knowledge_base(query)`: stdio locally, streamable HTTP on AgentCore Runtime |
| `agent/deploy.py` | Creates the Harness, or updates it when `HARNESS_ARN` is set; prints the `harnessArn` |
| `agent/invoke.py` | Sends a question and streams the answer to the terminal |

Progress and open work are tracked in [../PROGRESS.md](../PROGRESS.md).

## How this differs from the original spec

| Spec | Here | Why |
|---|---|---|
| Table `documents`, `vector(384)`, all-MiniLM-L6-v2 | Table `rag_chunks`, `vector(256)`, Amazon Titan Text Embeddings v2 | The existing database stores Titan 256-dim vectors; a query must be embedded by the same model to be comparable |
| Claude 3 Sonnet, us-east-1 | Claude Haiku 4.5 (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`), eu-west-2 | Claude 3 Sonnet has reached end of life in us-east-1; eu-west-2 is the database's region |
| Local stdio MCP server as the harness tool | Same server, deployed to AgentCore Runtime behind a Gateway; stdio for local tests | A harness runs in AWS and can only call remote tools (an MCP URL or a Gateway) |
| `bedrock-agentcore` SDK | boto3 only | boto3 already has `CreateHarness`, `UpdateHarness` and `InvokeHarness` |
| `metadata` column | `payload` JSONB (text removed; source URL, chunk index, ingest date, TTL kept) | The existing table's layout |

## Prerequisites

- An AWS account with Bedrock access to Claude Haiku 4.5 and Titan Text
  Embeddings v2 in eu-west-2
- Python 3.10+
- The RAG app's PostgreSQL pgvector database (`rag_chunks`); this project
  never creates tables or indexes and never writes data

## Setup

```sh
cd agentcore-kb
python -m venv .venv && source .venv/bin/activate
pip install -r mcp_server/requirements.txt -r agent/requirements.txt
cp .env.example .env    # then fill in the values
```

## Run the MCP server locally

```sh
python mcp_server/server.py          # stdio, for an MCP client such as Claude Desktop
python mcp_server/server.py --http   # streamable HTTP on 0.0.0.0:8000/mcp
```

It connects as the read-only user `kb_reader` (SELECT on `rag_chunks` only).
The password comes from `PGPASSWORD`, or on AgentCore Runtime from the SSM
SecureString named by `PGPASSWORD_PARAMETER`, so it never appears in the
runtime's configuration. It checks the password, database and table at startup
and exits with a clear message if any is unavailable. Each search embeds the query with
Titan (AWS credentials from the usual boto3 chain), then returns the 5 closest
unexpired chunks as a numbered list: source URL, similarity, text and metadata.

## Deploy the agent

The Runtime, Gateway and IAM roles are not created by these scripts yet; see
[../PROGRESS.md](../PROGRESS.md) for that work. Once `HARNESS_ROLE_ARN` and
`GATEWAY_ARN` are in `.env`:

```sh
python agent/deploy.py               # prints harnessArn; copy it into .env as HARNESS_ARN
python agent/deploy.py               # with HARNESS_ARN set: updates the same harness
```

## Ask a question

```sh
python agent/invoke.py "How do I travel by train in Austria?"
```

The answer streams to stdout; tool calls and token usage go to stderr. The
session id is derived from your username, so follow-up questions continue the
same conversation.

## Ask from the web UI

The RAG app's Retrieval page runs this harness as a **team of three agents**
orchestrated by a LangGraph state machine (`app/agent.py`): a supervisor, a
knowledge-base agent and a generic external-APIs agent whose tools come from
the registry in `app/api_tools/` (canienter.com entry requirements, Open-Meteo
weather, transport.opendata.ch Swiss connections and departures). Each agent is this harness invoked with its own system prompt,
inline tools and runtime session (per-call `systemPrompt` and `tools`
overrides). The web app runs the tools itself, so the MCP server, Runtime and
interface endpoints are not needed for the web UI; `invoke.py` still uses the
gateway tool. The web app needs `AGENT_HARNESS_ARN` (on EC2,
`docker-compose.aws.yml` sets it), the EC2 role policy
`rag-systems-agentcore-kb-agent` in `infra/terraform/agentcore.tf`, and
outbound HTTPS to the APIs.
