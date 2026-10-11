# Plan: install with one command

Status: **Phases 2–3 done and the release workflow and README written
(2026-10-11), not committed; waiting for the user to commit, push and
publish a test release (`v0.1.0-rc.1`).** Update this file at the end of
every step.

GitHub: issue #60. Working agreement: phases with a stop at each; a branch
(`release/install`) and a pull request; the user merges (AGENTS.md).

## Goal

A new user runs one command, answers one question (their model key), and
gets the whole stack running on their own machine at
`http://localhost:18000`, with the demo documents loaded. No clone, no
image build, no account anywhere but the model provider's.

## How it is today

- **The image holds only the dependencies.** `Dockerfile.web` installs
  `requirements-web.txt`; the code (`app/`, `dags/`, `web/`, `data/`) is
  bind-mounted from the checkout by `docker-compose.yml`. So the image alone
  cannot run the app.
- **Compose builds the image** (`build: *app-build`, `image:
  rag_systems-web`); nothing is pulled from a registry.
- **There is no `.dockerignore`.** Copying the code into the image would
  also copy `.env`, `.env.aws`, `DEPLOYMENT.local.md`, `data/urls.json` and
  `data/evaluations/`: secrets and the maintainer's data must stay out.
- **Settings:** `.env.example` sets `POSTGRES_PASSWORD`, `COMPOSE_PROFILES=demo`,
  `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `EMBEDDING_PROVIDER=local`,
  `AUTH_MODE=dev`. Langfuse is a separate overlay (`docker-compose.langfuse.yml`).
- **EC2** deploys source archives and builds on the host
  (`scripts/aws/deploy.sh`), with the code mounted; it is not changed here.

## Design

1. **The image carries the code.** `Dockerfile.web` copies `app/`, `dags/`,
   `web/`, `data/demo/` and `data/urls.example.json`, and sets the default
   command. A `.dockerignore` keeps out `.env*`, `DEPLOYMENT.local.md`,
   `data/urls.json`, `data/evaluations/`, `.git`, `infra/`, `agentcore-kb/`,
   `node_modules`. The dev compose file keeps its bind mounts, which simply
   cover the copied code: development and EC2 work as before.
2. **Release workflow** (`.github/workflows/release.yml`), on a tag `v*`:
   build for `linux/amd64` and `linux/arm64` with Buildx, push
   `ghcr.io/cherrydasika/ingenmind:<version>` (and `:latest` for full
   releases), then create a GitHub Release with the release compose file,
   `install.sh` and `SHA256SUMS`. A pre-release tag (`v0.1.0-rc.1`) tests
   the whole path without touching `latest`.
3. **Release compose file** (`deploy/compose.yml`): pgvector, the worker,
   the web app and the demo seed, all from the tagged image; named volumes;
   the port bound to `127.0.0.1`; no bind mounts, no `build:`. Langfuse is
   not in it.
4. **`install.sh`** (POSIX `sh`, short enough to read):
   - checks Docker and Docker Compose v2 are installed and running;
   - installs into `~/.ingenmind` (or `INGENMIND_DIR`): downloads the
     release compose file for the chosen version and checks its checksum;
   - asks for the model key (Anthropic by default; `INGENMIND_PROVIDER`
     and the key can come from the environment, for unattended installs);
   - generates `POSTGRES_PASSWORD` and `SESSION_SECRET`, writes `.env`
     (mode 600);
   - pulls, starts, waits until the app answers, prints the address;
   - running it again upgrades in place and keeps `.env` and the data.
   `uninstall.sh` (or `install.sh --uninstall`) stops the stack, and only
   with `--purge` deletes the data volumes and `~/.ingenmind`.
5. **Docs:** the README's quickstart becomes the install command (pinned
   and latest), what it installs, how to stop, upgrade and remove; cloning
   moves to "Development".

## Phases

- [x] **1. Plan**: this file. The user's answers below.
- [x] **2. The image and the release compose file**: `Dockerfile.web`,
  `.dockerignore`, `deploy/compose.yml`; build locally and run the release
  compose file from an empty directory (no checkout) with the demo; the
  dev stack and the test suite unchanged. Stop.
  - Done: the image carries `app/`, `dags/`, `web/`, the demo data and
    `urls.example.json` (918 MB, mostly dependencies); no `.env`,
    `DEPLOYMENT.local.md` or `urls.json` in it. `deploy/compose.yml`
    (project `ingenmind`): pgvector, worker, web app and the demo seed
    (profile `demo`, so it can be turned off). From an empty folder with the
    local image: the demo loaded (6 chunks), a first user signed in, and the
    agents answered "Is there step-free access at Kestrel Bay station?"
    from the demo document with a citation; the page, scripts, Plotly and
    the flow builder bundle load. The dev image rebuilt with the new
    Dockerfile: 410 tests pass, the dev stack runs as before.
- [x] **3. `install.sh` and uninstall**: test a fresh install, a re-run
  (upgrade) and an uninstall on this Mac, against the locally built image.
  Stop.
  - Done (`install.sh`, POSIX sh): Docker and Compose checks; the latest
    release from the GitHub API, or `INGENMIND_VERSION`; the compose file
    downloaded and checked against `SHA256SUMS`; the Anthropic key asked
    on the terminal (hidden) unless set; secrets generated; `.env` mode 600;
    `INGENMIND_DEMO=0` leaves out the demo; a re-run keeps `.env` and the
    data and only changes the version; `--uninstall` keeps the data,
    `--purge` deletes it. Tested on this Mac with a local compose file and
    image (`INGENMIND_COMPOSE_FILE`, `INGENMIND_IMAGE`,
    `INGENMIND_IMAGE_TAG`): fresh install and an answer, re-run (settings
    unchanged, the user still there), uninstall (2 volumes kept), purge
    (none left), with and without the demo.
- [ ] **4. Release workflow**: push a pre-release tag (`v0.1.0-rc.1`),
  check the multi-arch image on GHCR and the GitHub Release assets; the user
  makes the package public; install from the published release. Stop.
  - Written, not run: `.github/workflows/release.yml` (QEMU and Buildx,
    amd64 and arm64, `ghcr.io/cherrydasika/ingenmind:<version>` and
    `latest` for full releases, then `gh release create` with
    `compose.yml`, `install.sh`, `SHA256SUMS`; a tag with `-` is a
    pre-release).
- [x] **3b. Demo first, your own domain from the app** (the user, 2026-10-11:
  install the demo by default, then offer the switch as a prompt and in the
  settings).
  - The installer also asks for a Tavily key (Enter skips it; needed only
    for setup's web research). `knowledge_system.demo_wanted()`: the seed
    loads the demo only on a new install or while the demo is live, never
    after a reset or with setup under way (it used to reload the demo on
    every start, even mid-setup). The knowledge-system API says whether web
    search is configured (`web_search`).
  - Admin → Knowledge system: **Use your own domain** (while the demo is
    live): what setup does, the Tavily instructions when the key is
    missing (button disabled), and two clicks (no typing) that reset and
    open setup. After sign-in, admins see a notice linking there, with
    **Not now** (remembered in that browser).
  - Tests: `demo_wanted` (new install, demo live, after a reset, set up);
    the seed skips once the demo is left. Full suite: 412 pass. Browser,
    with the local image: the notice after sign-in, the card without a
    Tavily key (button disabled), with one: two clicks land on setup's first
    question; a restart afterwards does not bring the demo back (0 chunks,
    state NEW).
  - Found and fixed on the way: `v0.1.0-rc.1`'s installer failed with a
    fixed version (Compose preferred the shell's `INGENMIND_VERSION`);
    `v0.1.0-rc.2` built; `rc.3` will carry this step.
- [x] **3c. The demo's own scope** (2026-10-11). Installing `v0.1.0-rc.3` as
  a new user: the README's example "Can I bring my dog on Lakeshore Rail
  trains?" was blocked every time by the output guardrail ("Lakeshore Rail
  is not a UK train operator"): the demo ran the built-in flow, scoped to UK
  rail. Now the seed also publishes `lakeshore_demo` (the built-in flow
  through `evaluation.apply_blueprint` with the demo's brief, routing and
  scope) and makes it live, only while the built-in flow is live. Test: the
  flow is live with the demo's scope, and is not installed over a chosen
  flow. 413 pass. The local image, installed: the demo's evaluation set
  (`lakeshore_demo`, 7 questions with both README examples) met 7 of 7, pass
  rate 100%, none blocked by the output guardrail; the off-topic poem still
  blocked.
- [ ] **5. Docs, PR, the first release (`v0.1.0`).**
  - README written: "Get started" is the install command (with a fixed
    version, unattended settings, stop, upgrade, remove), cloning is "From
    source"; "Releases" under Development; the project layout.

## The user's answers (2026-10-11)

`~/.ingenmind`. For the proof of concept, the recommendations for the rest:
the installer asks only for the Anthropic key (OpenAI was not tested with a
real model; evaluating it is for later), Langfuse is left out, port 18000,
`v0.1.0` after `v0.1.0-rc.1`.

## The questions as asked

1. **Where it installs**: `~/.ingenmind` (recommended: one folder holds
   the compose file and settings; data in Docker volumes), or the current
   directory.
2. **What the installer asks**: only the Anthropic key, with OpenAI and
   Ollama through environment variables (recommended: one question), or a
   provider menu.
3. **Langfuse**: left out of the install (recommended: smaller and faster,
   and it is optional; `--with-tracing` can come later), or included.
4. **The port**: `18000` as now (recommended), or ask when it is taken.
5. **First version number**: `v0.1.0` (recommended), after a
   `v0.1.0-rc.1` test release.
