# On call

What to look at and what to do when IngenMind misbehaves. The commands
are for an install made with `install.sh` (`~/.ingenmind`). On the EC2
deployment, run `sudo bash scripts/aws/stack.sh status` from the release
directory, inside an SSM session (`scripts/aws/session.sh`).

```bash
alias im='docker compose -f ~/.ingenmind/compose.yml'
im ps                      # every service up?
im logs --tail 200 webapp  # the web app (questions, migrations, background loops)
im logs --tail 200 worker  # the ingestion worker
```

## Where to look first

| Signal | Where |
|---|---|
| Is it up? | The status dots in the sidebar (`/api/status`), and `im ps` |
| Service levels | **Monitoring → Operations**, top: availability, latency, pass rate, each with its error budget and burn rate |
| What alerted | **Monitoring → Alerts → History**: each alert and whether Slack or email took it |
| One bad answer | **Monitoring → Runs** or **Sessions**: the run's steps, evidence and checks. Its trace is in Langfuse, if set up |
| Who changed what | **Admin → Audit log** |
| Ingestion | **Data → Knowledge base → Ingestion**: the worker's queue and failed jobs |

## Alerts and what to do

| Alert | Usually means | Do |
|---|---|---|
| **Error rate** above 5% (1 h) | The model provider is down or out of credit, or the database is unreachable | Check `im logs webapp` for the provider's error. Out of credit: top up, or move the agents to another model in **Build → Agent Builder** (**Monitoring → Overview → Models** shows what each job uses). Database: `im ps`, then `im restart pgvector` |
| **Slowest 5%** above 60 s (1 h) | The provider is slow, or the app is out of threads (more questions at once than `WEB_THREADS` covers) | Operations → Per model shows which. Threads: see `loadtest/README.md` and raise `WEB_THREADS` within the database's connections |
| **Answer pass rate** below 80% (1 day) | A source changed or went stale, or a flow or model change made answers worse | **Monitoring → Analytics → Learning from live use** lists the sources behind poor answers, with a recommendation to approve. Then run the evaluation set on the live flow |
| **Evaluation regression** | Last night's evaluation passed fewer questions than the run before | Compare the two runs on **Evaluations**. If a recent promotion caused it, make the previous version live again in **Build → Agent Builder** |
| **Guardrail blocks** above 30% (1 h) | An abuse attempt, or a guardrail model misbehaving (it fails closed) | **Monitoring → Runs**, filtered to blocked: are they real attacks? A guardrail model error shows in the run |
| **Cost today** above $20 | Heavy use, a loop, or an expensive model on a busy job | Operations → Top users and Per agent. Tighten a person's limits on **Admin → Users**, or `GLOBAL_TOKENS_PER_DAY` |
| **Ingestion failures** | A source refused us (robots, 403, 404) or a file didn't parse | **Data → Knowledge base → Ingestion** has the error. Fix or remove the source |
| **Stale sources** | Pages gone, failing three times, or overdue for a re-crawl | **Data → Knowledge base → Ingestion → Freshness** |

## Common fixes

- **Restart a service:** `im restart webapp`. A run cut short by the
  restart is marked interrupted, and Ask-page runs resume on their own.
- **The app won't start: "Refusing to start: this database was migrated
  by …"** An older release is running against a newer database. Install
  the newer release again, or restore the backup (next item).
- **Restore a backup:** the installer keeps one before each upgrade in
  `~/.ingenmind/backups/`. The steps are in [Upgrading](upgrading.md#going-back).
  On EC2, the restore test script brings a snapshot back:
  `scripts/aws/restore-test.sh`.
- **A leaked API key:** revoke it on **Profile → API keys** (or **Admin →
  Users** for someone else's). It stops working at once. Then check **Admin → Audit log**
  for what it did.
- **Someone asks for their data, or for it to be deleted:** **Admin → Users →
  Download data / Delete account and data**. The deletion reaches Langfuse
  and the assistant's memory as well.
- **Sign everyone out:** change `SESSION_SECRET` in `.env` and restart
  the web app. Every session cookie stops working.
- **The disk is full:** Docker's build cache and old images are the usual
  cause: `docker system df`, then `docker builder prune`. Never prune
  volumes: they hold the data.

## After an incident

Write down what happened, how it was noticed, and what changed. If an
alert should have fired sooner or didn't fire at all, adjust its
threshold on **Monitoring → Alerts**.
