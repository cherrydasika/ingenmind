# Plan: identity, permissions, sessions and memory

Status: **Phase 7 — deployed to EC2 (`618012d`), sign-in and sign-out**
checked; only the Langfuse session-ID check is left.
**Do not deploy to EC2 until Phase 7**: EC2 is fixed to AUTH_MODE=oidc
and has no provider settings yet, so nobody could sign in. Update this file at the end of every step: tick what is
done, note what was found, and say what comes next.

Working agreement: stop at the end of each phase (and whenever a decision
is the user's to make), summarise, and get the user's confirmation before
starting the next. Commit and push only when the user says so; deploying to
EC2 needs the user to run the publish workflow (see AGENTS.md).

The original requirement (the user's spec) is summarised in "Goal"; the
full text was pasted in the conversation of 2026-10-07 and is the reference
for anything not covered here.

## Goal

Turn the single-user app into a small multi-user platform:

- **Identity** — users sign in through an external identity provider; the
  app never stores or sees passwords. Log out ends the session so the next
  person signs in fresh.
- **Authorisation** — explicit permissions (query RAG, upload documents,
  manage knowledge, view sessions, view agent memory, run evaluations,
  manage agents, manage users, manage settings); roles (Admin, Developer,
  User, Viewer) are bundles of permissions; checks are on permissions,
  enforced server-side on every API route.
- **Sessions** — created automatically (no "New session" button), tied to a
  user, with history: questions, answers, retrieval and agent activity,
  evaluation. Users see their own sessions; seeing others' needs a
  permission.
- **Session memory** — per user + session; never visible to another user.
- **Agent memory** — what an agent learns across sessions (strategies,
  source quality), kept apart from any user's conversation.
- **User admin** — add, view, edit, deactivate (not hard delete) users and
  their permissions.
- **Appearance** — Light / Dark / System, remembered per user.
- **Navigation** — Admin only for those allowed; user menu (Profile, Log
  out) top right.

## Where things are today (2026-10-07)

- No users: every request is `local-user`, and the browser sends its own
  `user_id` and `session_id` in each request body (not trustworthy).
- Session = a random ID in the tab's `sessionStorage` (`web/static/js/main.js`),
  "New session" button in the top bar; `/api/session/reset`.
- The chat history lives only in the browser tab; the server keeps no
  conversation. Question text reaches Langfuse (traces tagged with session
  and user) and the agents' memory; flow metrics keep numbers only.
- Agent memory today is really session memory: AgentCore Memory (EC2) or
  the `agent_sessions` table (local runtime), one memory actor per browser
  session (`agent._actor_id`). The input guardrail also keeps the last three
  allowed questions per session in process memory (`agent._recent`).
- No light theme: CSS tokens in `web/static/css/app.css` are dark only; the
  flow builder canvas uses `colorMode="dark"`.
- The app is reached privately through SSM port forwarding
  (`localhost:28000` for EC2); there is no domain or HTTPS yet. "Invite-only
  authentication" is an open item before public access (AWS_MIGRATION.md).
- Product direction: runs anywhere Docker does; AWS is an option, never
  required. So the identity provider must be pluggable, not AWS-only.

## Phases

Each phase ends with a checkpoint: tests pass, the change is shown working,
this file and PROGRESS.md are updated, and the user confirms.

### Phase 1 — Identity foundation (backend)

- [x] Tables `app_users` (user_id, first_name, email, role, status,
  auth_provider, auth_subject, theme, created/updated by and at,
  last_login) and `app_user_permissions` — `app/identity.py`
- [x] Permission catalogue and role bundles (admin, developer, user, viewer)
  in `app/identity.py`; every check is on a permission
- [x] OpenID Connect sign-in (Authlib; authorization code + PKCE; ID token
  checked against the provider's keys) — `app/auth.py` (`/auth/login`,
  `/auth/callback`). **Not yet tried against a real provider**: that is
  Phase 7, with the provider the user picks
- [x] Signed, HTTP-only cookie `rag_session` (12 h, SameSite=Lax;
  `SESSION_COOKIE_SECURE=1` behind HTTPS); `POST /auth/logout` clears it and
  returns the provider's end-session URL when it has one
- [x] `AuthMiddleware` checks `auth.RULES` on every `/api` request; a route
  with no rule is refused (a test fails if any route lacks one). Handlers
  take user_id from the cookie and keep session IDs apart per user
  (`<user>:<browser session>`, `web_api._scoped`)
- [x] Invite-only: a sign-in links by provider subject, or once by email to
  a user an admin added; `ADMIN_EMAILS` addresses get in as admins;
  deactivated users are refused and signed out on their next request
- [x] Development sign-in (`AUTH_MODE=dev`, the Compose default): `/auth/dev`
  answers only for a localhost host name; the first visit creates the first
  admin; it links no provider identity. EC2 is fixed to `AUTH_MODE=oidc` and
  `RAG_DEPLOYMENT=aws` (`docker-compose.aws.yml`); the app refuses to start
  with dev there, or with oidc missing its settings
- [x] Tests: `app/test_auth.py` (19, in CI); the API tests in
  `test_flows.py` now sign in first. 228 Python tests pass

Checkpoint 1: sign in, see who you are, routes refuse without permission.

### Phase 2 — Sign-in and navigation UI

- [x] Signed-out screen with **Sign in** (`web/index.html` `#signin`); it
  explains sign-in errors (`?auth_error=not_invited|disabled|…`)
- [x] User menu top right: name ▾ → name, email, role; Profile; Log out.
  New Profile page (`web/static/js/profile.js`): details and permissions
- [x] "New session" button and the session/user IDs are gone from the top
  bar; the browser session lives with the tab (the server scopes it per user)
- [x] Sidebar shows only the pages the user may use (`pages` in
  `web/static/js/main.js` names each page's permission); a group label hides
  with its pages; opening a page by URL without permission shows "No
  access". Admin section: added with the admin page in Phase 5
- [x] Log out clears the tab (`sessionStorage`, the page) and goes to the
  provider's end-session URL when it has one; any 401 (expired or
  deactivated) shows the sign-in screen — also from the flow builder bundle
  (`web/flows-app/src/api.js`, rebuilt)

Checkpoint 2: two users on one browser, one after the other, see only their
own things.

### Phase 3 — Sessions and history

- [x] `app_sessions` and `app_session_turns` (`app/sessions.py`). The
  server picks the session: the cookie's `sid` when it is the user's,
  active and used in the last 30 minutes, else a new one (older active ones
  end); signing in starts fresh; logging out ends it
- [x] Each Retrieval-page question is a turn: question, the answer shown,
  and the public result (sources, answer check, guardrails, activities,
  flow, timings; no chunk text), plus the Langfuse trace ID (server-side
  only; `agent.answer` returns it and `web_api._agent_payload` removes it).
  Blocked questions are recorded too. The playground and evaluations are
  not recorded (they are tests)
- [x] Langfuse traces, agent memory and the guardrail's follow-up context
  use the server's session and the real user for the Retrieval page; other
  routes keep `<user>:<browser session>`
- [x] `/api/sessions` (`?user=me|all|<id>`), `/api/sessions/current`,
  `/api/sessions/{id}`; others' sessions need `view_sessions` (404 / 403
  otherwise). **Sessions** page (`web/static/js/sessions.js`): by day, My
  sessions / Everyone's, a session's details and conversation
- [x] The Retrieval page loads the current session's conversation from the
  server (survives reloads and tabs); its per-tab storage and **Clear chat**
  are gone (a clear button would be "New session" by another name)
- [x] Retention: sessions last used over 30 days ago are deleted with their
  turns (`sessions.prune`, at most hourly, when a turn is recorded)
- [x] Tests: `app/test_sessions.py` (8, in CI); 236 Python tests pass

Checkpoint 3: ask questions, sign out and in, find the earlier session.

### Phase 4 — Session memory and agent memory

- [x] Session memory keyed by user + session: the memory actor, runtime
  sessions and the guardrail's recent questions all derive from the
  server's session (Retrieval page) or `<user>:<browser session>`; a test
  shows two users with the same browser session never share an actor
- [x] Agent memory store `app_agent_memory` (`app/agent_memory.py`), learned
  at the end of every run (`agent._drive`, fail-soft) from outcomes only:
  research source validation per web site (pass/fail, mean score, failed
  checks); sites cited by answers that passed; evidence decisions; whether
  query rewrites recovered; tool calls per tool (errors anonymised: quoted
  text blanked). Web sites only, never paths; never questions, answers,
  tasks, queries or tool inputs (tested)
- [x] **Agent memory** page (`web/static/js/agent_memory.js`,
  `/api/agent-memory`) with `view_agent_memory`
- [x] Decision 5: agents do not read agent memory yet (store and view only)
- [x] Tests: `app/test_agent_memory.py` (6, in CI); 242 Python tests pass

Checkpoint 4: memory separation shown with two users; agent memory visible.

### Phase 5 — User administration

- [x] `/api/users` (list, add) and `/api/users/{id}` (edit: first name,
  role, permissions, status), `manage_users` only. A new role without
  explicit permissions brings its bundle; email is fixed (it matches the
  person's first sign-in); users are deactivated, never deleted
- [x] Lock-out protection (`identity.guard_change`): no deactivating
  yourself, no removing your own `manage_users`, and at least one active
  user who can manage users must remain
- [x] **Users** page under a new Admin sidebar section
  (`web/static/js/users.js`): add form (role fills the permission boxes,
  then adjustable), table (name, email, role, status, last sign-in,
  permissions), edit panel with Deactivate / Reactivate
- [x] Audit: created by / at, last changed by / at, last sign-in
- [x] Tests: `app/test_users.py` (5, in CI); 247 Python tests pass

Checkpoint 5: an admin adds a user, who can then sign in with only their
permissions.

### Phase 6 — Appearance

- [x] Light theme: `:root[data-theme="light"]`, and `"system"` follows
  `prefers-color-scheme` (`web/static/css/app.css`). Hard-coded tinted
  colours became variables (`--ok-text`, `--err-text`, `--warn-text`,
  `--info-text`, `--link`, `--on-soft`, `--code-*`, `--topbar`, …); only data
  colours (heatmap, swatches) stay fixed
- [x] Light / Dark / System on the Profile page, saved on the user
  (`PUT /api/me/theme`); applied at sign-in; the browser remembers the last
  one (`web/static/js/theme.js`) so the sign-in screen is drawn in it too
- [x] Flow builder canvas follows the theme (`colorMode` prop; its tinted
  colours use the same variables); rebuilt
- [x] Test: saving your own theme (any signed-in user, valid values only);
  248 Python tests pass

Checkpoint 6: both themes on every page.

### Phase 7 — Deploy and documents

- [x] `infra/terraform/cognito.tf`: user pool (email sign-in, admin-created
  accounts only, 12-character password policy, deletion protection), hosted
  domain `rag-systems-<account>`, confidential app client (code flow,
  openid/email/profile, callback `http://localhost:28000/auth/callback`).
  Outputs `cognito` and `cognito_client_secret` (sensitive). Plan from the
  branch: 3 to add, 0 to change, 0 to destroy
- [x] `OIDC_LOGOUT_URL` (`app/auth.py`): Cognito's discovery has no
  end-session endpoint, so without it "Log out" would leave Cognito signed
  in and the next person would get in as the previous one (tested)
- [x] `scripts/aws/fetch-secrets.sh` now requires the sign-in keys;
  `scripts/aws/set-auth-secrets.sh <admin email>` writes them into
  `/rag-systems/prod/langfuse-env` from the Terraform outputs (keeps
  SESSION_SECRET if set, prints no values, checks the 4 KiB limit)
- [x] README ("Users, sign-in and sessions"), infra/README.md,
  AWS_MIGRATION.md (invite-only authentication ticked)
- [x] Merged `identity` into `main` through PR #1 (CI passed): `2e45ca1`
- [x] `terraform apply` from main: 3 added, 0 changed, 0 destroyed — pool
  `<user-pool-id>`, client `<client-id>`, domain
  `rag-systems-<account-id>` (`terraform output cognito`)
- [x] `set-auth-secrets.sh admin@example.com`: parameter 1,700 bytes
- [x] Found while checking: Cognito's discovery now lists an end-session
  endpoint, but it ignores the standard `post_logout_redirect_uri` (the user
  is left on Cognito's sign-in page); `OIDC_LOGOUT_URL` (with `logout_uri`)
  now wins over discovery
- [x] First Cognito account created (`admin@example.com`, CONFIRMED):
  at the user's request, with a generated permanent password instead of an
  emailed temporary one. The password is kept outside the repo in
  `~/.config/rag-systems/cognito-login.txt` (mode 600)
- [x] EC2 started; the user published `618012d`; deployed. Checked on the
  instance: `/api/me` reports oidc / Amazon Cognito, signed-out `/api/flows`
  401, `/auth/dev` 404, `/auth/login` redirects to the Cognito hosted UI
  with the tunnel callback, openid/email/profile and PKCE (S256)
- [x] The user signed in at http://localhost:28000 through Cognito's hosted
  page. Sign-out ends the Cognito session too: `/auth/logout` returns
  Cognito's `/logout?client_id=…&logout_uri=http://localhost:28000/`, which
  302s back to the app, and the next "Sign in" asked for the password again
- [ ] Check that a Langfuse trace carries the server's session ID (left over
  from Phase 3): deferred, the user will check it in the Langfuse UI
  later; see docs/BACKLOG.md for what was tried

## Decisions

Taken (2026-10-07, by the user):

1. Identity provider: **any OpenID Connect provider** (authorization code +
   PKCE), configured by settings; not tied to AWS.
2. Local development sign-in: **yes, localhost only** — pick a user when
   `AUTH_MODE=dev`; refused on any other host; never enabled on EC2.
3. Conversation history: **stored on the server, kept 30 days**, then
   deleted automatically.
4. New session: **at sign-in, or after 30 minutes idle**.

6. EC2's identity provider (2026-10-07, at checkpoint 6): **Amazon
   Cognito**, set up with Terraform — a user pool where admins create
   accounts (no self sign-up), its hosted sign-in page and an app client.
   Cognito holds the passwords. First admin (`ADMIN_EMAILS`):
   admin@example.com.
5. Agent memory (2026-10-07, at checkpoint 3): **store and view only** —
   record learnings from run outcomes and show them with
   `view_agent_memory`; agents do not read them yet, so answers do not
   change. Using them in prompts is a later step that needs evaluation runs.

## Log

- 2026-10-07 — Plan written from the user's specification; current state
  surveyed. The user took decisions 1–4. Phase 1 started.
- 2026-10-07 — Phase 1 done (not committed): identity.py, auth.py, the
  middleware, Compose and `.env.example` settings, new dependencies
  (authlib, httpx, itsdangerous; rebuild the image), test_auth.py. Checked
  live on the local stack: signed out → 401, dev page offers the first
  admin, other host names → 404. The web pages are not adapted yet: until
  Phase 2 they get 401s (there is no sign-in screen). Next: checkpoint 1
  with the user, then Phase 2.
- 2026-10-07 — Checkpoint 1 confirmed by the user; EC2 stopped.
- 2026-10-07 — Phase 2 done (not committed). Checked in the browser against
  a throwaway database: sign-in screen → dev sign-in → first admin Ada →
  full sidebar and user menu → log out (tab storage cleared, /api/me empty)
  → viewer Vic: Home first, only Home/Ingestion/Sources/Pipeline
  docs/Profile, `#/flows` → No access; Vic deactivated mid-session → next
  request shows "Your sign-in has ended". 228 Python tests and the
  flows-app tests pass. Next: checkpoint 2, then Phase 3.
- 2026-10-07 — Checkpoint 2 confirmed; phases 1–2 committed to branch
  `identity` (`8f292fc`) and pushed. Phase 3 started.
- 2026-10-07 — Phase 3 done (not committed). Checked in the browser on a
  throwaway database with the local runtime and an Anthropic key: Ada asked
  "Will it rain in Leeds tomorrow?" and "And the day after?" (understood in
  context); a reload restored both turns and the graph; Sessions listed the
  session (2 questions, current) and its detail; after Ada logged out, Dev
  (developer, view_sessions) had an empty chat, no own sessions, and saw
  Ada's ended session under Everyone's. Not yet checked: the Langfuse trace
  carrying the server's session ID (code path only). Next: checkpoint 3 and
  decision 5, then Phase 4.
- 2026-10-07 — Checkpoint 3 confirmed; decision 5 taken (store and view);
  phase 3 committed to `identity`. Phase 4 started.
- 2026-10-07 — Phase 4 done (not committed). Checked in the browser on a
  throwaway database: Una (user) asked "Will it rain in Leeds, UK
  tomorrow?" — her sidebar had no Agent memory and the API refused her; Dev
  (developer) saw Agent memory: seeded research and knowledge-base outcomes
  plus `get_weather` 2 calls, 100% from Una's run, nothing she typed. Found
  and fixed during the check: a syntax error in agent_memory.js stopped the
  whole app loading (CI's module check would have caught it; AGENTS.md now
  lists that check for local runs). Next: checkpoint 4, then Phase 5.
- 2026-10-07 — Checkpoint 4 confirmed; phase 4 committed. Phase 5 started.
- 2026-10-07 — Phase 5 done (not committed). Checked in the browser on a
  throwaway database as Ada (admin): added Bob (user, 1 permission), made
  him a developer (the role ticked its 7 permissions), Ada's own Deactivate
  was refused ("You can't deactivate yourself"), Bob deactivated (greyed,
  Reactivate offered), audit line shown. Next: checkpoint 5, then Phase 6.
- 2026-10-07 — Checkpoint 5 confirmed; phase 5 committed. Phase 6 started.
- 2026-10-07 — Phase 6 done (not committed). Checked in the browser on a
  throwaway database: Profile → Light (saved); Retrieval with the workflow
  graph, the flow builder canvas, Users and the sign-in screen after log
  out all light; back to Dark (saved on the profile), flow builder dark.
  The development sign-in page (/auth/dev) stays dark: it is a fixed
  development-only page. Next: checkpoint 6, then Phase 7 (deploy).
- 2026-10-07 — Checkpoint 6 confirmed; decision 6 taken (Cognito; first
  admin admin@example.com); phase 6 committed. Phase 7 started.
- 2026-10-07 — Phase 7 prepared (not committed): Cognito Terraform, logout
  URL, secrets script, docs; 249 Python tests pass; plan 3 add / 0 change /
  0 destroy. Next: checkpoint 7a — the user's go-ahead to merge, apply,
  write the SSM secrets, create the Cognito account and deploy.
