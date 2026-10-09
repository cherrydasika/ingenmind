# AWS Infrastructure

This directory defines a **single EC2 foundation** in `terraform/`. Nothing
here is applied automatically on push. The manual workflow can produce a plan,
but any future apply needs separate approval after instance sizing, cost review,
and a backup plan.

## Bootstrap Status (updated 2026-10-01)

The foundation is applied and the stack runs on EC2; see
[Status in AWS_MIGRATION.md](../AWS_MIGRATION.md#status-2026-10-01) for what
is done and what is left.


- AWS account `<account-id>`, Region `eu-west-2`: the dedicated S3 state
  bucket `rag-systems-tfstate-<account-id>-eu-west-2` exists with versioning,
  SSE-S3 encryption, and all four S3 public-access blocks enabled. Its policy
  requires TLS and allows only the bootstrap user, account root, and the
  Terraform plan role; add a future apply role to the policy before using it.
- An existing GitHub Actions OIDC provider trusts `sts.amazonaws.com`.
  `arn:aws:iam::<account-id>:role/rag-systems-terraform-plan` is restricted
  to this repository's `main` branch, read-only infrastructure queries,
  state reads, and writes to its S3 lock object.
- Repository variables `TF_STATE_BUCKET`, `TF_PLAN_ROLE_ARN`, and
   `TF_INSTANCE_TYPE=t4g.xlarge` are configured. The uncommitted RDS proposal
   was removed in favor of running pgvector on this EC2 host.
  GitHub's private-repository Free plan does not allow branch protection or
  protected deployment environments here. The manual workflow is **plan-only**;
  it has no apply job and cannot provision EC2.
- The Terraform configuration was applied manually from a workstation; its
  state is `rag-systems/production.tfstate` in the state bucket. EC2
  `<instance-id>` (`t4g.xlarge`) runs the Compose stack, with
  pgvector and Langfuse data on the separate encrypted EBS data volume.
- **Not configured:** protected `production` environment and apply role. Do
  not set `TF_APPLY_ROLE_ARN` before verifying the environment approval
  protection.

## What Terraform Creates

| Resource | Purpose | Ongoing cost |
| --- | --- | --- |
| VPC, one public subnet, internet gateway, route table and security group | Outbound access for Systems Manager and future container pulls; **no inbound rules** | No NAT gateway or load balancer; EC2 public IPv4 is billed while assigned |
| One ARM64 Amazon Linux 2023 EC2 instance | Future Docker Compose host, reachable for administration via Systems Manager; no SSH key | Charged while running |
| Encrypted 20 GiB gp3 root volume | Operating system; deleted if the instance is terminated | EBS storage |
| Encrypted gp3 data volume (60 GiB by default) | Reserved for pgvector and self-hosted Langfuse state; protected with `prevent_destroy` | EBS storage even while EC2 is stopped |
| EC2 instance profile with `AmazonSSMManagedInstanceCore` | Keyless Systems Manager access | No separate role charge |
| Separate private, versioned SSE-S3 artifact bucket | Reviewed commit-addressed source archives; public access blocked | S3 storage and requests |
| GitHub OIDC publisher role and scoped EC2 delivery policy | Manual source uploads and read-only source/one SSM parameter retrieval | No separate role charge; SSM Standard SecureString has no parameter charge |

The data volume is attached but **not formatted or mounted** by Terraform.
`scripts/aws/prepare-host.sh` prepares it after an approved apply; do not run
the containers until that script verifies the mount. The EC2 role permits
only Titan V2 and Haiku EU-profile model invocation, Systems Manager, release
object reads, and retrieval/decryption of `/rag-systems/prod/langfuse-env`.
Terraform never creates or stores the secret value. Neither Docker, the app,
Langfuse, budget alerts, TLS, DNS, invitee authentication, nor a tested AWS
backup restore is deployed. No inbound ports are open; the app is **not
publicly reachable**. Do not publish it without authentication and HTTPS.

## Private Source And Secret Delivery

The artifact bucket is **not** the Terraform state bucket. After an explicitly
approved Terraform apply, set GitHub repository variables `ARTIFACT_BUCKET`
and `ARTIFACT_PUBLISH_ROLE_ARN` from the corresponding Terraform outputs.
`Publish reviewed source archive` runs by itself when "Terraform and app
checks" passes on a push to `main`, for the commit those checks tested; run
it manually on `main`, entering `PUBLISH`, for a commit the checks skipped.
It uses short-lived OIDC credentials to upload a tracked-file tarball and
SHA-256 checksum under `releases/<commit SHA>/`; it cannot apply Terraform or
read SSM secrets. Never put `.env`, `.env.aws`, runtime data, or AWS keys in
Git. Publishing a revision does not deploy it. A second publish of the same
revision fails rather than overwriting the archive (a partially uploaded
revision requires separate review before retrying).

Separately, on a trusted administrator machine, create a **new** Compose
compatible env file with mode `0600`, then create the SSM Standard SecureString
`/rag-systems/prod/langfuse-env` in `eu-west-2` using an authorized human
credential. For example, use `aws ssm put-parameter --name
/rag-systems/prod/langfuse-env --type SecureString --value
file:///path/to/private/compose.env --region eu-west-2` after ensuring the
file does not exceed the Standard 4 KiB limit. Do not paste values into a
shell command, Terraform, GitHub variables, or this runbook. Required
variables are `POSTGRES_PASSWORD`, `DATABASE_URL`, `SALT`, `ENCRYPTION_KEY`,
`CLICKHOUSE_PASSWORD`, `REDIS_AUTH`, `NEXTAUTH_SECRET`,
`MINIO_ROOT_PASSWORD`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`,
`LANGFUSE_INIT_PROJECT_SECRET_KEY`, `LANGFUSE_INIT_USER_EMAIL`, and
`LANGFUSE_INIT_USER_PASSWORD`, and for sign-in `SESSION_SECRET`,
`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_LOGOUT_URL` and
`ADMIN_EMAILS` (`scripts/aws/set-auth-secrets.sh <admin email>` writes these
from the Cognito resources in `infra/terraform/cognito.tf`; it prints no
values). Use unique generated values, not the Compose
fallbacks; the Langfuse `DATABASE_URL` must use the Langfuse Postgres service
and agree with `POSTGRES_PASSWORD`. Treat access to the EC2 instance role as
access to these secrets. Deletion/rotation requires a deliberate recovery plan.

## Private EC2 Deployment Procedure (First Release)

1. After reviewing a Terraform plan and approving a manual apply and separate
   source publish, connect via Systems Manager. Set `BUCKET` to the artifact
   bucket output and `SHA` to the exact reviewed 40-character commit SHA.
   Bootstrap the first release without a GitHub token: download
   `s3://$BUCKET/releases/$SHA/source.tar.gz` and its `.sha256` sibling with
   `aws s3 cp`, run `sha256sum -c source.tar.gz.sha256` in the download directory,
   then extract the verified archive into `/opt/rag-systems/releases/$SHA` as
   root. For example, in an SSM shell after setting those two variables:

   ```sh
   mkdir -m 700 /tmp/rag-source-bootstrap
   cd /tmp/rag-source-bootstrap
   aws s3 cp "s3://$BUCKET/releases/$SHA/source.tar.gz" . --region eu-west-2
   aws s3 cp "s3://$BUCKET/releases/$SHA/source.tar.gz.sha256" . --region eu-west-2
   sha256sum -c source.tar.gz.sha256
   sudo mkdir -p "/opt/rag-systems/releases/$SHA"
   sudo tar --no-same-owner -xzf source.tar.gz -C "/opt/rag-systems/releases/$SHA"
   ```

   Set `SHA` from the reviewed main-branch commit, not from the downloaded
   archive. For later releases, the already verified copy of
   `scripts/aws/fetch-release.sh` does this download and verification. Never
   run an unverified archive. From the selected release, run
   `sudo bash scripts/aws/install-docker.sh` and
   `sudo bash scripts/aws/fetch-secrets.sh /opt/rag-systems/releases/$SHA`.
   The latter retrieves only the named SSM parameter, checks required keys,
   and writes `.env` as root with mode `0600`. No GitHub PAT, long-lived AWS
   keys, or local `.env.aws` goes onto the instance.
2. Use Terraform's `data_volume_id` output to identify the EBS disk, inspect
   it with `lsblk -o NAME,SERIAL,FSTYPE,MOUNTPOINTS`, then run
   `sudo bash scripts/aws/prepare-host.sh vol-...`. The script matches the
   volume ID against the NVMe serial, refuses partitioned or unexpected
   filesystems, formats only a raw disk, and mounts by filesystem UUID under
   `/srv/rag-systems`. Confirm `mountpoint /srv/rag-systems` before Docker.
3. Create a reviewed `data/urls.json` from the tracked
   `data/urls.example.json` in the release (runtime URLs are not archived).
   The EC2-only [Compose override](../docker-compose.aws.yml) maps pgvector,
   Langfuse Postgres, ClickHouse, Redis and MinIO data onto that EBS mount.
   It binds web and Langfuse admin ports to loopback and clears static AWS
   keys, allowing SDKs to use the EC2 instance role. On EC2 only, from the
   release directory, run `sudo bash scripts/aws/stack.sh start` (see
   [Start/Stop Runbook](#startstop-runbook)).
   Keep the security group closed; access privately through Systems Manager.
4. Transfer the tested local pgvector data using a reviewed `pg_dump` and
   `pg_restore`, then check chunk counts and retrieval. Never overwrite the
   local volume during transfer.

### Deploying A Change

1. Merge to `main` (CI must pass).
2. When CI passes, **Publish reviewed source archive** uploads
   `releases/<commit SHA>/source.tar.gz` and its checksum by itself (for a
   commit CI skipped, run it on `main` by hand, typing `PUBLISH`).
3. On EC2, through Systems Manager:
   `sudo bash /opt/rag-systems/current/scripts/aws/deploy.sh <commit SHA>`

`deploy.sh` fetches and verifies the archive with the active release's
`fetch-release.sh`, writes `.env` from SSM, copies `data/urls.json` and
`data/evaluations` from the active release, stops it (refusing while an
ingestion job is queued or running), re-runs `install-docker.sh` if it
changed, and starts the new release with `stack.sh start`. If the start fails
it starts the previous release again. On success `/opt/rag-systems/current`
points at the new release; older release directories stay for rollback
(`deploy.sh <older SHA>`).

Every release runs as the Compose project `rag-systems`, so a new release
replaces the previous containers. Releases before `deploy.sh` used the
release directory as the project name; `deploy.sh` removes those containers
(`down`, no volumes; all data is on `/srv/rag-systems`). To adopt `deploy.sh`
from such a release, run the new release's copy once:
`sudo bash /opt/rag-systems/releases/<SHA>/scripts/aws/deploy.sh <SHA>`
after `fetch-release.sh`.

### Work Sessions (from your workstation)

`scripts/aws/session.sh` starts or stops everything that bills by the hour,
so nothing is left running:

| Command | What it does |
|---|---|
| `scripts/aws/session.sh status` | EC2 state and how many agentcore-kb interface endpoints exist |
| `scripts/aws/session.sh start` | Start EC2, wait for SSM, `stack.sh start` |
| `scripts/aws/session.sh start --agent` | The same, plus the agentcore-kb interface endpoints (about $0.055/hour) |
| `scripts/aws/session.sh stop` | Endpoints off (Terraform, endpoints only), `stack.sh stop`, stop EC2 |

It uses `AWS_PROFILE` (default `personal`) and the initialised
`infra/terraform`; run it from an up-to-date `main` checkout. The AgentCore
Runtime, Gateway and Harness bill per use and stay in place.

### Start/Stop Runbook

Run on EC2 (through Systems Manager); `current` is the active release:

| Command | What it does |
|---|---|
| `sudo bash /opt/rag-systems/current/scripts/aws/stack.sh status` | EBS usage and the state of every service |
| `sudo bash /opt/rag-systems/current/scripts/aws/stack.sh start` | Refuses unless `/srv/rag-systems` is mounted from the UUID in `/etc/fstab` with every data directory present, `.env` and `data/urls.json` exist, Buildx is ≥ 0.17 and the Compose config is valid; then `up -d` and waits for the web app and Langfuse to answer |
| `sudo bash /opt/rag-systems/current/scripts/aws/stack.sh stop` | Refuses while an ingestion job is queued or running (`--force` overrides); then stops all services with a 60 s grace period and syncs the disk |

A work session: start EC2 → `stack.sh start` → open the SSM tunnels → work →
`stack.sh stop` → stop EC2. Always stop the stack before stopping EC2. Every
EC2 service uses `restart: unless-stopped`, so a stopped stack stays stopped
across reboots and only comes back through `stack.sh start`. If the data
volume is ever missing, the bind mounts (`create_host_path: false`) make
containers fail instead of creating empty databases on the root volume.

On stop, the ingestion worker finishes and checkpoints the URL in progress;
an interrupted job resumes from that URL when the worker next starts.
`langfuse-web` (upstream Next.js server) may hold idle keep-alive connections
past the grace period and is then killed (exit 137); it keeps no local state.
`install-docker.sh` pins Compose v5.3.1 and Buildx v0.36.1 by SHA-256 and
sets Docker log rotation (`json-file`, 10 MB × 5) for containers created
after it runs.

### Backup (Not Yet Tested On AWS)

For a consistent backup of **both** pgvector and Langfuse state, run
`stack.sh stop`, stop EC2, and take an EBS snapshot of the
`data_volume_id` while the instance is stopped. Wait for snapshot completion
before treating it as a backup. From the workstation,
`scripts/aws/snapshot.sh create "<note>"` snapshots the volume tagged
`rag-systems-data` and waits until it completes (crash-consistent if the
stack is still running); `scripts/aws/snapshot.sh list` lists them. Take
one before resetting the knowledge system on EC2. Keep the instance stopped between work
sessions; EBS and snapshot charges continue. To test a
restore, create a **new** EBS volume from the snapshot in the instance's
Availability Zone, attach it to an isolated host, and verify pgvector chunk
counts and Langfuse traces. Do not restore over the production volume.

The existing local `pg_dump`/`pg_restore` and disposable container restart
tests do not verify this AWS snapshot and mount procedure. No AWS snapshot
or restore has been performed.

## GitHub Workflow

- `.github/workflows/terraform-check.yml`: PR and main-branch Terraform
   `fmt`/`validate`, pgvector job tests, and frontend syntax checks; no AWS
   credentials and no apply.
- `.github/workflows/terraform-deploy.yml`: manually dispatched **on main**
   with the exact text `PLAN`. A read-limited role generates a plan, but there
   is no apply job. Plans are artifacts retained for one day; treat them as
   sensitive because future resources may contain secrets. An apply job may be
   added only after an enforceable approval gate is available.
- `.github/workflows/publish-source.yml`: runs after CI passes on a push to
   `main` (or manually dispatched **on main**);
   OIDC grants only commit-addressed S3 object upload. It
   has no infrastructure apply or secret-reading permission.

### One-Time Prerequisites (Not Automated)

1. Create a dedicated S3 state bucket in `eu-west-2`, with versioning,
   server-side encryption, blocked public access, and access limited to your
   Terraform roles. The workflow stores state at
   `rag-systems/production.tfstate` and uses S3 `.tflock` locking. Do not
   commit state files or backend credentials.
2. Set up GitHub OIDC in AWS. Give the **plan role** read access to the
   described resources and the necessary state/lock-object access; restrict
   its trust policy to this repository's `main` branch. Give the **apply
   role** only the infrastructure permissions it needs and restrict its OIDC
   trust to this repository's `production` environment. Do not store AWS
   access keys in GitHub.
3. Add repository variables `TF_STATE_BUCKET`, `TF_PLAN_ROLE_ARN`, and
   `TF_INSTANCE_TYPE`. Choose `TF_INSTANCE_TYPE` deliberately after sizing
   the six-service Langfuse stack; only `t4g.*` is accepted. Add
   `TF_APPLY_ROLE_ARN` as a variable on the `production` environment.
4. For automated apply, upgrade to a GitHub plan that supports protection on
   this private repository (or choose a different enforceable approval gate),
   then protect `main` and configure `production` with required reviewers and
   deployment limited to `main` **before** creating an apply role or adding
   its ARN. On the current plan, use GitHub for reviewable plans only.
5. Review the plan job's output and the AWS cost estimate before a separately
   approved manual apply. Applying starts a billable EC2 instance;
   AWS Budgets is not a spending cap. Stop EC2 when idle, but EBS storage
   and snapshots continue to incur charges. A manual start/stop workflow
   is still to be added.

The provider lock file is tracked. Terraform state, local plans, and provider
downloads are ignored. To validate without AWS access, run:

```sh
terraform fmt -check -recursive infra/terraform
terraform -chdir=infra/terraform init -backend=false -input=false
terraform -chdir=infra/terraform validate
```

## Verification Before Public Access

GitHub CI runs `app/test_pg_store.py` against a disposable pgvector database.
The tests cover retrieval, URL replacement rollback, stale-chunk removal,
expiry pruning, and full-text/RRF edge cases without calling Bedrock. Locally,
a `pg_dump`/`pg_restore` drill restored all 75 sample rows into a temporary
database, and a separate disposable pgvector container retained its fixture
across stop/start. These checks do **not** verify an EC2 EBS mount, Langfuse
restore, or recovery of the full AWS stack; those require deployment and a
separate restore drill.

The API's ingestion and ad-hoc write endpoints do not have user authentication
yet. The EC2 security group has no inbound rules; do not expose the app until
invitee authentication and authorization are implemented and tested.