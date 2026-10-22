# Neon fresh-database setup — Doraemon Lakehouse v0.7.22

This setup is intentionally **not a data migration**. It creates a new, empty Neon PostgreSQL database, applies the v0.7.21 schema and all SQL migrations, and seeds only the app's starter RSS sources and starter Topics. It does not read from or copy rows out of Supabase. This v0.7.21-based project includes no Cloudflare/R2/Iceberg runtime or deployment configuration.

## 1. Create the Neon project

1. Create a new project in the Neon console and choose a region reasonably close to the Render service.
2. Use the new project's default database or create a dedicated empty database/branch for Doraemon Lakehouse.
3. In **Connect**, copy both connection strings:
   - **Pooled** connection (`-pooler` in the host): use for routine API/worker queries as `DATABASE_URL`.
   - **Direct** connection (no `-pooler` in the host): use as `DATABASE_URL_UNPOOLED` for migrations/bootstrap and session-level advisory locks.
4. Keep `sslmode=require` in both URLs. Never commit real credentials to Git.

Neon distinguishes pooled and direct endpoints; transaction pooling does not guarantee the same server session, so this project uses the direct URL for the session-level pipeline and reconciliation locks.

## 2. Bootstrap the empty database

Run this from a local checkout of this v0.7.22 repository using Python with `requirements.txt` installed. Supply the two real Neon strings through environment variables; do not put your credentials in the command history if you use a shared terminal.

**PowerShell example**

```powershell
$env:DATABASE_URL = "<NEON_POOLED_CONNECTION_STRING>"
$env:DATABASE_URL_UNPOOLED = "<NEON_DIRECT_CONNECTION_STRING>"
python -m scripts.bootstrap_neon
```

**macOS/Linux example**

```bash
export DATABASE_URL='<NEON_POOLED_CONNECTION_STRING>'
export DATABASE_URL_UNPOOLED='<NEON_DIRECT_CONNECTION_STRING>'
python -m scripts.bootstrap_neon
```

The bootstrap script:

- Refuses non-Neon hosts and pooled-only connections.
- Refuses a non-empty `public` schema unless it recognizes its own migration ledger.
- Applies `sql/schema.sql`, then each file in `sql/migrations/` in ascending filename order, using a ledger to avoid reapplying successful steps.
- Verifies required tables and the `pg_trgm` extension.
- Does not import data, and does not clear or modify Supabase.

Expected result: `news_articles` has 0 rows. A small number of starter source and Topic records are expected; those are configuration, not migrated article history.

## 3. Configure Render

In the Render web service environment, set:

- `DATABASE_URL` = Neon pooled connection string.
- `DATABASE_URL_UNPOOLED` = Neon direct connection string.

Keep `OPENAI_API_KEY`, `OPENAI_MODEL`, `ADMIN_PASSWORD`, `SESSION_SECRET`, `TIMEZONE`, and other existing app configuration. Redeploy from the v0.7.22 branch after setting both URLs.

The included `render.yaml` declares both database variables as `sync: false`; it does not embed secrets.

## 4. Configure GitHub Actions secrets

Add/replace these repository secrets so all three independent workflows use the same new Neon database:

- `DATABASE_URL` — Neon pooled URL.
- `DATABASE_URL_UNPOOLED` — Neon direct URL.
- Keep `OPENAI_API_KEY` and `TELEGRAM_BOT_TOKEN` if the related functions are enabled.

The ingestion and catalog reconciliation workflows use the direct URL only for their session advisory lock; ordinary queries continue using `DATABASE_URL`. The Telegram digest reads from the same new database.

## 5. Start with fresh data

After Render deploys:

1. Open `/health` and confirm the service responds.
2. Run **Mini Lakehouse Ingestion** manually in GitHub Actions.
3. Verify new rows appear in `data_sources`, `pipeline_runs`, and `news_articles`.
4. Run **Mini Lakehouse Topic Entity Reconciliation + Entity Quality**.
5. Run **Mini Lakehouse Telegram Digest** only after you have configured Telegram if you use it.

Do not delete the old Supabase project immediately. This code no longer points to it after you change both connection variables, but keeping it temporarily provides a manual rollback reference. No rows are copied automatically, and the old database is never modified by the Neon bootstrap.

## Cloudflare

No Cloudflare integration, R2 object storage, Iceberg catalog, or Cloudflare environment variables are part of this v0.7.21-based v0.7.22 package.
