# Commute Help

Commute Help is a local-first web application for comparing normal trips with
routes affected by planned road closures in the Portland–Vancouver region. It
is designed for the host Mac and trusted devices on the same local network.

The application is currently at **Phase 0: Foundation and launcher**. The API,
SQLite database, frontend proxy, and one-command development launcher are in
place. Road-network generation and routing begin in Phase 1.

## Requirements

- macOS
- Node.js 20 or newer
- Python 3.12

No Docker, cloud database, or API key is required.

## First-time setup

From the repository root:

```bash
npm run setup
```

This creates or validates `.venv`, installs the root and frontend Node
dependencies, and installs `backend/requirements.txt`. It does not download a
road network; graph generation belongs to Phase 1.

## Daily startup

From the repository root:

```bash
npm run dev
```

The launcher starts FastAPI and Vite together and prints the available local
and LAN URLs. Open <http://localhost:5173>. Press Control-C once to stop both
processes.

LAN access is intended only on a trusted network. If macOS asks whether Node or
Python may accept incoming connections, allow it only when LAN access is
needed. Do not configure public port forwarding.

## Configuration

Defaults work without configuration. To override them, copy `.env.example` to
`.env` and edit the local copy. `.env` and the SQLite database are ignored by
Git.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMMUTE_HELP_ENVIRONMENT` | `development` | Runtime label shown by `/api/status` |
| `COMMUTE_HELP_DATABASE_PATH` | `data/app.db` | Authoritative SQLite database path |

The frontend always calls relative `/api/...` URLs. Vite proxies those requests
to FastAPI, which keeps the application usable from LAN browsers.

## Checks

Run all current checks:

```bash
npm test
```

With the application running, the Phase 0 health gate is:

```bash
curl http://localhost:5173/api/health
```

The response reports both API and database readiness. `/api/status` also states
that the routing graph is not configured yet.

## Data and privacy

SQLite data, private environment settings, generated graphs, OSM extracts,
traffic data, and exports are local artifacts and must not be committed. Home
and work locations will be treated as sensitive data as later phases are built.

See [GAMEPLAN.md](GAMEPLAN.md) for the complete Max Viable Product plan and
[AGENTS.md](AGENTS.md) for the implementation contract.
