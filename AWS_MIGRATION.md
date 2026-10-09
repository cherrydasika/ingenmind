# AWS Migration Working Notes

Edit this document as decisions change. It records decisions and progress; the
deployment itself is defined in `infra/` and `scripts/aws/`. See
[Status](#status-2026-10-01) for what is done and what is left.

## Agreed Direction

- AWS region: `eu-west-2`
- Monthly budget target: `GBP 100`; use alerts and a manual compute shutdown action. AWS Budgets is not a hard spending cap.
- Access: invitees only; choose authentication before inviting users.
- Domain: not selected; choose and configure it as a final step.
- Vector store: PostgreSQL with `pgvector` on the EC2 host; migrate the tested local corpus and persist database files on EBS across shutdowns.
- Ingestion: manually triggered PostgreSQL-backed worker, no scheduled ingestion; Airflow is retired from the active stack.
- Models: Amazon Bedrock; Claude plus selected lower-cost alternatives, selectable from a configured model list for evaluation.
- Observability/evaluations: self-host Langfuse on AWS.
- Data: public source material; still protect credentials, user access, and write-capable API endpoints.
- AWS access: use a named local AWS CLI profile or IAM role. Do not commit or share access keys or secrets.

## Editable Deployment Choices

- Initial hosting shape: single on-demand `t4g.xlarge` EC2 instance running the app, pgvector, and self-hosted Langfuse with Docker Compose. Both databases' persistent state lives on EBS.
- Pause action: `scripts/aws/stack.sh stop` through Systems Manager, then stop the EC2 instance (manual CLI). A GitHub Actions workflow for it is still `TBD`.
- Start action: start the EC2 instance, then `scripts/aws/stack.sh start` (refuses unless the EBS data volume is mounted). Deploys use `scripts/aws/deploy.sh`; see `infra/README.md`.
- Backup method and frequency: `TBD`. Include PostgreSQL/pgvector and Langfuse state; test a restore before relying on the backup.
- Invitee authentication: `TBD`.
- Domain and DNS provider: `TBD`.
- Infrastructure-as-code tool: Terraform with GitHub Actions; see `infra/README.md`.
- Bedrock model allowlist and region availability: `TBD`.
- Embedding model: `TBD`; changing it requires re-embedding the corpus.
- Evaluation artifact storage and worker execution model: `TBD`.

## Cost And Availability Notes

- Stopping EC2 stops its compute charges, but EBS storage, snapshots, S3, domain/DNS, and some networking resources can continue to incur charges.
- Bedrock requests can incur charges while EC2 is running; add application-level usage limits and a model-call disable control.
- Keep databases and admin interfaces private. Do not expose PostgreSQL, Langfuse dependencies, or ingestion controls directly to the public internet.
- Use Systems Manager for administration during development. Invitee access requires authentication and HTTPS; use a temporary secure access method until the domain is configured.

## Status (2026-10-01)

Running: EC2 `<instance-id>` (`t4g.xlarge`) serves release `288f0bc` as
Compose project `rag-systems`, privately through SSM port forwarding only.
Corpus: 5 test URLs from `data/urls.json`, 41 chunks.

### Done

- [x] AWS foundation: VPC with no inbound rules, EC2 with IMDSv2, encrypted
  root and 60 GiB data EBS volumes, SSM access, private artifact bucket, IAM
- [x] Source archives published per commit by GitHub Actions and verified on
  the host; secrets delivered from SSM Parameter Store
- [x] Compose stack on EC2: web app, worker, pgvector and self-hosted Langfuse,
  with all database state on the EBS volume
- [x] End-to-end test through SSM tunnels on an empty database: ingestion of
  5 URLs (41 chunks, 256-dim Titan embeddings), hybrid retrieval, Bedrock
  Claude Haiku answers with sources, refusal on an off-corpus question
- [x] Langfuse on EC2 records each question as a trace (search span plus
  Haiku generation with tokens and cost)
- [x] Failure and retry behavior: re-running fresh URLs skips them; two
  simultaneous triggers get one job and one HTTP 409; an unresolvable URL is
  recorded as a failed URL while the job finishes the rest
- [x] Docker Buildx pinned to v0.36.1 and Docker log rotation (10 MB × 5) in
  `install-docker.sh`; the legacy-builder workaround is no longer needed
- [x] Start/stop runbook (`stack.sh`): start checks the EBS mount, stop
  refuses during ingestion; every EC2 service uses `restart: unless-stopped`
- [x] The ingestion worker stops gracefully (exit 0) and resumes an
  interrupted job from its last checkpointed URL
- [x] One-command deploys (`deploy.sh`) with automatic fallback to the
  previous release; `/opt/rag-systems/current` names the active release

### Left Before Repeatable Operations

- [ ] Rotate the Langfuse secrets: the EC2 values (`NEXTAUTH_SECRET`, `SALT`,
  `ENCRYPTION_KEY`, initial admin password, project keys) currently match the
  local development `.env`, so a session from the local Langfuse can be valid
  on EC2.
  Changing `ENCRYPTION_KEY` affects data encrypted with the old key.
- [ ] Take an EBS snapshot of the data volume (no snapshots, lifecycle
  policies or AWS Backup plans exist yet)
- [ ] Restore that snapshot to a new volume and verify pgvector chunk counts and
  Langfuse traces from it
- [ ] Choose the backup frequency (manual before changes, or a lifecycle
  policy)

### Left Before Public Access

- [x] Invite-only authentication in the app: OpenID Connect sign-in
  (Amazon Cognito on EC2), users and permissions checked on every API route
  (identity plan, docs/plans/identity-sessions-memory.md)
- [ ] Domain and HTTPS certificate (no domain or ACM certificate yet)
- [ ] Controlled inbound path through a load balancer or reverse proxy
- [ ] Decide on the public IP: it is outbound-only (no inbound rules) but
  needed for SSM, Bedrock and scraping. Removing it requires VPC endpoints
  plus a NAT gateway for scraping.
- [ ] CloudWatch alarms, log shipping and retention (no alarms or app log
  groups yet; the CloudWatch agent is not installed). An account-wide AWS
  budget of USD 150/month exists; align it with the GBP 100 target and
  confirm its alert recipients.
- [ ] Re-run Terraform plan (with the `personal` AWS profile) and a security
  review
- [ ] Only then allow public access

### Known Issues

- Answer citations number chunks (`[Source 3]`) while the source list shows
  unique URLs, so the numbers can exceed the list length.
- Titan embedding calls and ingestion runs are not traced in Langfuse, so their
  cost is not visible there.
- An unresolvable URL takes about 35 s of HTTP retries before it is recorded
  as failed.
- `langfuse-web` (upstream) does not exit on SIGTERM within 60 s and is killed
  on stop (exit 137); it keeps no local state.
- The Evaluations tab has not been run on EC2 yet.
