# Upgrading

IngenMind uses [semantic versions](https://semver.org): **v1.2.3** is
major.minor.patch.

- A **patch** release only has fixes. Upgrade whenever you like.
- A **minor** release adds features and keeps your settings, data and API
  working.
- A **major** release may change settings or the API. Its release notes say
  what to do, under "Upgrade notes".

Pre-releases (`v1.1.0-rc.1`) are for trying a release out. The installer
never picks one unless you name it.

## Upgrade

Run the installer again. Add `INGENMIND_VERSION=v1.2.3` to pick a
release; without it you get the latest.

```bash
curl -fsSL https://raw.githubusercontent.com/cherrydasika/ingenmind/main/install.sh | sh
```

It:

1. keeps your settings (`~/.ingenmind/.env`) and moves `INGENMIND_VERSION`
   to the new release;
2. **backs up the database** first, to `~/.ingenmind/backups/rag-<time>.sql.gz`;
3. downloads the release's `compose.yml`, checks it against the release's
   `SHA256SUMS`, and pulls the image. From v1.0.0 the image is signed. If
   [cosign](https://docs.sigstore.dev/cosign/system_config/installation/)
   3 or newer is installed, the installer verifies the signature and refuses an image
   that doesn't match;
4. restarts. On start, the web app **migrates the database** before it
   serves anything (`app/migrations.py`). Every module brings its tables up
   to date, and the version is recorded in `ingenmind_schema_versions`.
   Migrations only ever add tables, columns and indexes, so an upgrade
   never deletes data.

Watch the first start with `docker compose -f ~/.ingenmind/compose.yml logs -f webapp`.
It prints `database migrated to 1.2.3`.

## Going back

The app refuses to start against a database that a **newer** release has
migrated, because the older code may not understand it. To go back:

```bash
cd ~/.ingenmind
docker compose down
docker compose up -d pgvector
gunzip -c backups/rag-<time>.sql.gz | docker compose exec -T pgvector sh -c \
  'dropdb -U rag rag && createdb -U rag rag && psql -q -U rag rag'
INGENMIND_VERSION=v1.1.0 sh install.sh      # the version you had
```

Setting `ALLOW_DOWNGRADE=true` in `.env` starts an older release against a
newer database anyway. Use it only once you have checked that release's
notes say it is safe.

## Verify an image yourself

With cosign 3 or newer (older versions look for signatures in another place
and report "no signatures found"):

```bash
cosign verify ghcr.io/cherrydasika/ingenmind:1.0.0 \
  --certificate-identity-regexp '^https://github.com/cherrydasika/ingenmind/.github/workflows/release.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Each image also carries an SBOM and build provenance. To see them, run
`docker buildx imagetools inspect ghcr.io/cherrydasika/ingenmind:1.0.0 --format '{{ json .SBOM }}'`.

## Making a release (maintainers)

1. Check that `main` is green and the PRs are labelled. The labels group
   the generated notes (`.github/release.yml`).
2. Add the release to `CHANGELOG.md`: highlights, plus "Upgrade notes" for
   anything you must do by hand.
3. Tag it: `git tag v1.2.3 && git push origin v1.2.3`. The release
   workflow then:
    - builds the amd64 and arm64 image;
    - signs it;
    - publishes the GitHub Release with `compose.yml`, `install.sh`,
      `SHA256SUMS` and the notes;
    - publishes this site.
