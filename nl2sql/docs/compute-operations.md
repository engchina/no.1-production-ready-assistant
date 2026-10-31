# NL2SQL on OCI Compute — runtime and operations

NL2SQL is installed on the suite's single Compute instance by the deployment
scripts in `platform/deploy/` (the OCI Resource Manager Terraform stack was
removed in #12). For the shared Autonomous AI Database, the app DB user, Nginx,
HTTPS, updates, and the deployment check, see
[`platform/docs/deployment.md`](../../platform/docs/deployment.md).

This page covers what runs on the NL2SQL Compute instance and how to operate it.
The configuration administrator (login user ID `system_admin`) is shared with
RAG and the Agent Control Plane.

## Runtime Notes

The bootstrap script writes `/u01/aipoc/no.1-production-ready-suite/nl2sql/backend/.env`
and `/u01/aipoc/no.1-production-ready-suite/platform/.env` (both `0600`)
on the instance from the deployment inputs in `/u01/aipoc/props/`. Track
progress in `/var/log/suite-init.log` and `/var/log/nl2sql-init.log` until the
application services are ready.
The bootstrap starts:

- `production-ready-nl2sql-backend` on private upstream `127.0.0.1:8010`
  (changed from `8000` in #1316 so that RAG
  can use `8000` on the same Compute; local development uses `8110` instead, #25)
- the single Nginx site of the suite (`platform/deploy/suite-init.sh`), on
  `https_port` (HTTPS, default `443`) or `http_port` (default `80`)

The suite puts every selected product on one Compute (#1316). Nginx serves the
NL2SQL `frontend/dist` (built with `FRONTEND_BASE_PATH=/nl2sql/`) at `/nl2sql/`
and reverse proxies `/nl2sql/api/` to the backend with the `/nl2sql` prefix
removed. NL2SQL is not deployed on its own: the three products are deployed
together by the suite, and `nl2sql/init_script.sh` does not write an Nginx site.
Open TCP `https_port` (or `http_port` when HTTPS is off) publicly in the
security list or NSG. When HTTPS is on, `http_port` is neither opened nor
listened on, and HTTP is not redirected to HTTPS (#1638); see
[`platform/docs/deployment.md`](../../platform/docs/deployment.md) for the ports and the HTTPS
certificate. If ADB uses a
private endpoint, the selected network must still allow Compute to reach ADB on
TCP `1522`.

The bootstrap applies the core system schema and application security/RBAC
migrations idempotently. When both commands succeed, it enables and starts the
schema refresh, quality evaluation, and ontology workers. If either command
still fails after retries, the backend and Nginx remain available in degraded
mode and all three workers stay stopped. The affected settings/security page
shows a recoverable initialization error instead of an unhandled Oracle error.

After completing the manual recovery commands in the troubleshooting section,
operators can enable the workers with:

```bash
sudo systemctl enable --now production-ready-nl2sql-schema-refresh-worker
sudo systemctl enable --now production-ready-nl2sql-quality-evaluation-worker
sudo systemctl enable --now production-ready-nl2sql-ontology-worker
```

The configured `SYSTEM_ADMIN` login comes from the application administrator
values in `platform/.env`:

- `PLATFORM_ADMIN_LOGIN_USER_ID=system_admin`
- `PLATFORM_ADMIN_LOGIN_USER_PASSWORD`

Deep Data Security is disabled by default; turn it on only where it is used
(#1707). By default `NL2SQL_ORACLE_DEEPSEC_ENABLED=false` and the DATA USER
password is empty. With `NL2SQL_ORACLE_DEEPSEC_ENABLED=true`, the DATA USER
password is required:

- `NL2SQL_ORACLE_DEEPSEC_ENABLED` (`false` by default)
- `NL2SQL_ORACLE_DEEPSEC_DATA_USER=DEEPSEC_DATA_USER`
- `NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD`

To use it, open `セキュリティ設定 > Deep Data Security` after deployment (saving the
DATA USER password there also writes `NL2SQL_ORACLE_DEEPSEC_ENABLED=true` to
`backend/.env`), apply the V001 steps in order, then run the Data Grant verification.

The NL2SQL `backend/.env` reserves a fixed Oracle Select AI credential name and the default
Select AI region:

- `NL2SQL_SELECT_AI_CREDENTIAL_NAME=NL2SQL_OCI_CRED`
- `NL2SQL_SELECT_AI_REGION=us-chicago-1`

After deployment, open `運用設定 > Select AI Credential`.
The administrator explicitly creates `NL2SQL_OCI_CRED` for the current Oracle schema
from the server-side OCI config signing key. The private key is passed to
`DBMS_CLOUD.CREATE_CREDENTIAL` only as an Oracle bind variable; deployment inputs,
browser responses, and persisted application settings never receive it. An
existing credential is not overwritten automatically and requires the explicit
recreate confirmation flow. Historical Profile sync jobs are not retried
automatically after credential creation.

This configured administrator is independent from the database connection user,
does not read from `PLATFORM_USERS`, and does not require the auth/RBAC tables
to exist. Application-local users are checked from `PLATFORM_USERS`. The
configured administrator password can be changed from the application password
change screen; the backend writes the new value back to `platform/.env`.

The deployment does not create any ADB resource. The wallet of the existing ADB
and the database values come from the deployment inputs and are written into
`platform/.env`:

- `PLATFORM_ORACLE_USER`
- `PLATFORM_ORACLE_PASSWORD`
- `PLATFORM_ORACLE_DSN` (`existing_oracle_dsn`, or `<selected ADB db_name lowercased>_high`
  when left blank)
- `PLATFORM_ORACLE_CONNECTION_SECURITY` (`wallet_mtls` when mTLS is required,
  otherwise `walletless_tls`)
- `PLATFORM_ORACLE_WALLET_PASSWORD` (reuses `existing_oracle_password`)
- `PLATFORM_ORACLE_ADB_OCID`
- `PLATFORM_ORACLE_ADB_REGION`

実行環境は3製品共通の `PLATFORM_ENVIRONMENT` です。
共通 `.env` に `PLATFORM_ENVIRONMENT` と `PLATFORM_AUTH_MODE=authenticated` を設定します。
`staging` / `production` では HTTPS と Secure Cookie を使います。
HTTP を使うローカルの構成では `PLATFORM_ENVIRONMENT=local` を明示します。

## Updating an Existing Compute Deployment

### Renaming the Select AI credential to `NL2SQL_OCI_CRED` (#1712)

The Select AI credential is now named `NL2SQL_OCI_CRED` so that it follows the
database object prefix rule and does not collide with an `OCI_CRED` owned by
another application in the same schema. The backend no longer uses the old
name. After updating an instance whose credential is still `OCI_CRED`:

1. Open `運用設定 > Select AI Credential` as an administrator and create the
   credential. The screen shows `NL2SQL_OCI_CRED` as missing; creating it also
   writes `NL2SQL_SELECT_AI_CREDENTIAL_NAME=NL2SQL_OCI_CRED` to `backend/.env`.
   (Alternatively, set that value in `backend/.env` yourself and restart the
   backend before creating the credential.)
2. Run the Oracle 反映 (Profile sync) again for each business profile so that
   the `DBMS_CLOUD_AI` profiles reference `NL2SQL_OCI_CRED`.
3. Only if no other application in the schema uses it, drop the old credential
   with `BEGIN DBMS_CLOUD.DROP_CREDENTIAL('OCI_CRED'); END;`.

### Moving to the shared `platform/.env` (#211)

Deployments created before #211 keep every setting in `backend/.env` with the
old unprefixed names (`ORACLE_DSN`, `OCI_REGION`, `APP_ADMIN_*`, ...). The
backend no longer reads those names. Before running `update-after-pull.sh` on
such an instance, stop the backend and workers, pull the source, and move the
settings once:

```bash
cd /u01/aipoc/no.1-production-ready-suite
uv run --project platform/packages/backend_core \
  python platform/scripts/migrate_env_to_platform.py --product nl2sql          # review
uv run --project platform/packages/backend_core \
  python platform/scripts/migrate_env_to_platform.py --product nl2sql --apply  # rewrite
sudo chown opc:opc platform/.env && sudo chmod 0600 platform/.env
```

Then run `./scripts/update-after-pull.sh` as below. See
[docs/configuration.md](../docs/configuration.md) (「既存環境の更新手順（#211）」)
for details.

After manually pulling the required repositories, run the post-pull update
script as the `opc` user when passwordless sudo is available. The script uses
only non-interactive `sudo -n` and never prompts for the `opc` password. If
the instance does not grant passwordless sudo, start the script through an
existing root-capable path with `sudo ./scripts/update-after-pull.sh`. In root
mode, dependency synchronization, frontend builds, and database CLIs are still
executed as `opc`; only systemd, snapshots, logging, and permission repair
retain root privileges. The script does not run Git commands and does not
rewrite `backend/.env`, `platform/.env`, the Wallet contents, systemd units, or the Nginx
configuration.

```bash
cd /u01/aipoc/no.1-production-ready-suite/nl2sql
./scripts/update-after-pull.sh --check
./scripts/update-after-pull.sh --repair-only
./scripts/update-after-pull.sh
```

Without passwordless sudo, use the root-launch form instead; these commands do
not make the build output root-owned:

```bash
sudo ./scripts/update-after-pull.sh --check
sudo ./scripts/update-after-pull.sh --repair-only
sudo ./scripts/update-after-pull.sh
```

`--check` only inspects the fixed paths, Wallet and `.env` permissions, systemd
units, and system schema status. It does not create a lock, log, snapshot, stop a
service, or run a migration. `--repair-only` skips dependency synchronization
and frontend building, then snapshots and repairs the deployed instance,
applies migrations, and restores services. With no arguments, the script first
synchronizes and compile-checks the backend and builds the shared UI and NL2SQL
frontend into a staging directory while the current services remain available.
Only after those steps succeed does it enter the maintenance window.

Before stopping services, mutating modes create a root-only snapshot below
`/u01/aipoc/recovery`. They then enforce `/u01/aipoc` as `root:opc 0775`,
`/u01/aipoc/wallet` as `opc:opc 0700`, and Wallet files, the install lock,
`backend/.env`, and `platform/.env` as `0600`. `PLATFORM_ORACLE_WALLET_DIR` in
`platform/.env` remains fixed at `/u01/aipoc/wallet`; the `.env` file is parsed
without sourcing secret values.
System and security migrations are idempotent. The script never deletes
`.wallet.tmp-*` or `.wallet.backup-*`, never recreates the schema, and never
applies the administrator-confirmed DeepSec foundation plan.

If a migration, backend health check, or worker restart fails, all external
workers are disabled and the script makes a best-effort attempt to restore the
backend. The existing `frontend/dist` remains active. In a full update, the new
frontend is promoted only after the backend is healthy and every service is
active. If the final public/Nginx health check fails, the previous frontend is
restored while the successfully initialized backend and workers remain running.
The command log is appended to `/var/log/nl2sql-update.log`.

The shared platform defaults to `platform/` in the same suite checkout
(`/u01/aipoc/no.1-production-ready-suite/platform`). For a nonstandard installation,
override `PLATFORM_REPO_DIR`. `BACKEND_HEALTH_URL`, `PUBLIC_HEALTH_URL`,
`HEALTHCHECK_TIMEOUT_SECONDS`, and `HEALTHCHECK_INTERVAL_SECONDS` are also
available for nondefault ports or health-check timing.

## Troubleshooting

### Live application logs

`scripts/tail-logs.sh` is a read-only viewer that merges the four
`production-ready-nl2sql-*` units and the `/var/log` files into one stream and
renders the backend's JSON log lines in human-readable form. It never restarts
or otherwise mutates a service. Run it on the Compute instance:

```bash
cd /u01/aipoc/no.1-production-ready-suite/nl2sql
./scripts/tail-logs.sh --status          # unit state + backend/public health check
./scripts/tail-logs.sh                   # follow the four application units
./scripts/tail-logs.sh --backend         # backend only
./scripts/tail-logs.sh --all             # units + Nginx + init/update logs
./scripts/tail-logs.sh --level ERROR --since "-15 min" --no-follow
```

`--help` lists every option (`--workers`, `--unit`, `--nginx`, `--init`,
`--update`, `--lines`, `--grep`, `--raw`). It falls back to `sudo` automatically
when journald is not readable as the current user; if that fails, rerun with
`sudo ./scripts/tail-logs.sh`.

The equivalent manual commands, and everything the script does not cover:

```bash
sudo tail -f /var/log/suite-init.log
sudo tail -f /var/log/nl2sql-init.log
cd /u01/aipoc/no.1-production-ready-suite/nl2sql
sudo systemctl status production-ready-nl2sql-backend
sudo journalctl -u production-ready-nl2sql-backend -f
sudo journalctl -u production-ready-nl2sql-schema-refresh-worker -f
sudo journalctl -u production-ready-nl2sql-quality-evaluation-worker -f
sudo journalctl -u production-ready-nl2sql-ontology-worker -f
sudo nginx -t
sudo tail -f /var/log/nginx/production-ready-nl2sql-error.log /var/log/nginx/production-ready-suite-error.log
curl -i http://127.0.0.1:8010/api/health
curl -ik https://127.0.0.1:443/nl2sql/api/health   # https_port を変えた環境はその port。HTTPS が off なら http://127.0.0.1:<http_port>/nl2sql/api/health
curl -ik https://127.0.0.1:443/nl2sql/health
```

### Validate an updated or repaired Compute instance

After a successful update or repair, validate with relative UTC time instead of
converting the browser timestamp manually:

```bash
sudo systemctl --no-pager --full status \
  production-ready-nl2sql-backend.service \
  production-ready-nl2sql-schema-refresh-worker.service \
  production-ready-nl2sql-quality-evaluation-worker.service \
  production-ready-nl2sql-ontology-worker.service
sudo journalctl \
  -u production-ready-nl2sql-backend.service \
  -u production-ready-nl2sql-schema-refresh-worker.service \
  --since "-15 min" --no-pager -o short-iso
```

Then confirm Wallet refresh, application user management, the pending DeepSec
V001 plan, and completion of any previously pending schema refresh job from the
UI. Docker is not part of this recovery path, and OCI SDK circuit-breaker INFO
messages are not failures.

For a permanent rollout to a replacement Compute instance, keep the repaired old
instance until the replacement passes the same health/UI checks, and migrate `/u01/data/production-ready-nl2sql` before cutover
when it contains local documents or settings.

The deployment (`platform/deploy/suite-init.sh` and `nl2sql/init_script.sh`):

1. Installs Node.js 24, uv, and build dependencies with `dnf` (the Compute OS is
   Oracle Linux 9; Nginx, firewalld, and SELinux are configured by
   `platform/deploy/suite-init.sh`. #1611).
2. Clones the suite monorepo (NL2SQL and the shared platform) once.
3. Extracts the ADB wallet to `/u01/aipoc/wallet`.
4. Writes the runtime `backend/.env` and the shared `platform/.env`.
5. Installs backend dependencies with `uv sync --locked --no-dev --python 3.12`.
6. Keeps `/u01/aipoc/wallet` at `opc:opc 0700` with files at `0600`, and
   sets `/u01/aipoc` to `root:opc 0775` for Wallet install locks and atomic
   temporary/backup directories.
7. Runs `nl2sql_system_schema --initialize` and
   `app_security_migrate --apply --skip-bootstrap` with retries.
8. Builds the shared UI package and `frontend/dist`.
9. Starts the backend service with systemd and starts external workers only
   when both schema commands succeeded.
10. Leaves Nginx to `platform/deploy/suite-init.sh`, which serves the SPA at
    `/nl2sql/` and proxies `/nl2sql/api/` to the backend (#1316).

This deployment is a direct systemd + Nginx installation.
Docker is not installed or required on the Compute instance.

Node.js installation uses the Oracle Linux AppStream module `nodejs:24` first. If
enabling the module, installation, or post-install validation fails, the init script
falls back to the official Node.js `latest-v24.x` Linux tarball with
`SHASUMS256.txt` verification. Override `NODEJS_OFFICIAL_RELEASE_BASE_URL` or
`NODEJS_OFFICIAL_INSTALL_DIR` only for controlled mirrors or recovery testing;
`NODEJS_OFFICIAL_BIN_DIR` is also available when the symlink target must be
isolated.

Backend startup remains independent of database reachability and table
existence. The bootstrap tries both migrations first, but if the database is not
ready yet the application still starts in degraded mode. The deployment check at
the end of `suite-init.sh` then reports `check: nl2sql schema NG` (from
`nl2sql_system_schema --check`, which also requires the `app_security_migrate`
tables) and exits non-zero (#1648). Check `/var/log/nl2sql-init.log` for
`WARNING: Continuing in degraded mode` and the ORA code (for example `ORA-01031`
or `ORA-01950` when the database user lacks a privilege or tablespace quota; see
[`platform/docs/deployment.md`](../../platform/docs/deployment.md)), then run:

```bash
cd /u01/aipoc/no.1-production-ready-suite/nl2sql/backend
sudo -u opc /usr/local/bin/uv run python -m app.cli.nl2sql_system_schema --initialize
sudo -u opc /usr/local/bin/uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap
sudo systemctl restart production-ready-nl2sql-backend
sudo systemctl enable --now production-ready-nl2sql-schema-refresh-worker
sudo systemctl enable --now production-ready-nl2sql-quality-evaluation-worker
sudo systemctl enable --now production-ready-nl2sql-ontology-worker
```

The security migration creates `NL2SQL_DEEPSEC_MIGRATIONS`, allowing the Deep
Data Security page to show the pending V001 foundation plan. The bootstrap does
not apply those administrator-confirmed DeepSec foundation steps automatically.
