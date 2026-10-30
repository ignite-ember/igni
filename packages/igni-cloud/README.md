# igni-cloud

igni's cloud layer. The desktop igni (`ignite-ember`, import name
`ember_code`) is the core; this package is a layer on top of it that runs
igni in a producer's microVM, with its state in PostgreSQL. The core never
imports it — it finds it through two entry points:

| Entry point | Name | What | Used when |
|---|---|---|---|
| `igni.layers` | `cloud` | `igni_cloud.layer:CloudLayer` | `IGNI_PROFILE=cloud` |
| `igni.storage` | `postgresql` | `igni_cloud.storage.postgres:PostgresStorage` | a `postgresql://` database URL |

The layer replaces the core's seams (`ember_code/core/layer.py`): logs as
JSON lines on standard output, credentials from the token the VM keeps
from its own credential, settings only from the image and igni server,
state required in `IGNI_DATABASE_URL`, the extension listing kept in the
process, MCP approvals per person on igni server, and the guest contract
with the VM's agents around the backend's boot and serving (`ready`, late
binding to the producer, `activity.json`, restores, the VM credential).
The storage gives a PostgreSQL database Agno's scoped storage, uploads as
large objects, and what `igni migrate` prepares as the admin role (Agno's
tables, row-level security). See `docs/CLOUD.md` section 9 in the
repository.

## Install

The image installs both packages and sets `IGNI_PROFILE=cloud`:

```sh
pip install ignite-ember igni-cloud
```

The desktop installs `ignite-ember` alone, which depends on neither psycopg
nor `agno[postgres]`.

## Commands

- `igni-cloud db grant-producer --org O --producer person:<id>|app:<id>|space:<id>
  --incarnation I --database-url <admin>` — create an incarnation's role
  (or rotate its password) and print `{role, password, database_url,
  producer, incarnation}`; `revoke-producer` drops it and keeps its rows.
  What the node agent runs on a new producer's first start: it imports
  click, SQLAlchemy and the role code only.
- `igni-cloud db export-producer … --incarnation I --out FILE` — a removed
  incarnation's data as a portable zip (`manifest.json`, `rows.jsonl`,
  `attachments/`), for an org admin during retention;
  `erase-producer … --incarnation I` deletes every row and upload of it
  and drops its role, printing the audit summary. Idempotent.
- `IGNI_PROFILE=cloud igni migrate --database-url <admin>` — igni's tables,
  Agno's tables and row-level security.
- `IGNI_PROFILE=cloud python -m ember_code.backend --ws-port …` — the
  backend, with the layer installed.

## Tests

```sh
uv pip install -e packages/igni-cloud
pytest packages/igni-cloud/tests
IGNI_TEST_DATABASE_URL=postgresql://… pytest packages/igni-cloud/tests
```
