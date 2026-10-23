# v1.0 launch checklist

R63 ([#119](https://github.com/cherrydasika/ingenmind/issues/119)): one
place to confirm IngenMind is ready to ship. v1.0.0 is tagged when every
line is done or consciously deferred.

Status as of 2026-10-23, on `main` after v1.0.0-rc.5.

## Roadmap

| Item | Status |
|---|---|
| R01–R63 closed or deferred | 51 of 63 done. Deferred: R60 (multi-tenant workspaces). Skipped earlier: R01, R03, R08, R09, R17, R25. Built in part, waiting on outside accounts: R48 (OAuth connectors), R50 (a real Slack app and Teams bot), R55 (alert delivery) |

## Security

| Item | Status |
|---|---|
| Review against the security model | **Done** (2026-10-23). [Security model](security.md) updated for everything since R02. One fix: requests to admin-configured addresses (webhooks, alert channels, MCP, OpenAPI tools, A2A) now refuse the metadata service and other special addresses, redirects included (`app/outbound.py`) |
| Automated scans | **Done.** On every PR: CodeQL, pip-audit, npm audit, Trivy and gitleaks. A ZAP baseline scan of the app had no failures |
| Pentest of the EC2 site | **Waiting**: needs an approved EC2 session. An active, signed-in scan (ZAP full scan with an admin and a user account) through the SSM tunnel, plus a manual pass over sign-in, permissions and the public API |
| The image runs as root | **Accepted for v1.0** while the app is only reached through 127.0.0.1 or SSM. A non-root user comes before public access (R09) |

## Operations

| Item | Status |
|---|---|
| Restore test | **Local: done** (2026-10-23). The installer's database dump restored into a new database with every table's rows (74 tables), and the app migrated it and served sign-in, sessions, the audit log and Operations. **EC2: waiting** on R08: apply the snapshot policy and run `scripts/aws/restore-test.sh` |
| Alerts verified | **Rules: done**: every rule fires a test alert and records each channel's result. **Delivery: waiting** on a real Slack incoming webhook, an SMTP account and CloudWatch on EC2 (R55) |
| On-call notes | **Done**: [On call](on-call.md) |
| Capacity | **Local: done**: 2 workers × 45 threads served about 12 questions a second with no errors. **EC2: waiting**: the same run on a t4g.xlarge (`loadtest/README.md`) |

## Product

| Item | Status |
|---|---|
| Installs with one command | **Done** on v1.0.0-rc.5. The image signature verified, the install came up and migrated, and running it again took a backup first |
| Demo works from a clean install | **Waiting** on a model key with credit. The rc.5 install started with the demo data, but the demo question wasn't answered: the local Anthropic key is out of credit |
| README and screenshots | **Text: done.** **Screenshots: waiting**: the four in `docs/images` show the old sidebar. Retake them from a clean demo install once a question can be answered |
| Docs site | **Done**: built strictly on every PR. Goes live with the v1.0.0 tag |

## To tag v1.0.0

1. Do the waiting lines above, or mark them deferred here.
2. Add the date to the 1.0.0 entry in `CHANGELOG.md`.
3. `git tag v1.0.0 && git push origin v1.0.0`. The release workflow then builds, signs and publishes, and the docs site goes live.
4. Install it with the one command from the docs on a clean machine.
