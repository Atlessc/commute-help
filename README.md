# Commute Help

Commute Help is a local-first web application for comparing normal trips with
routes affected by planned road closures in the Portland–Vancouver region. It
is designed for the host Mac and trusted devices on the same local network.

The application has completed **Phase 0: Foundation and launcher** and
**Phase 1: Local road graph**. The graph tooling provides a fixed regional
boundary, OpenStreetMap download, normalization, validation, versioned
artifacts, and one-time backend loading.

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
and LAN URLs. Open [http://localhost:5173](http://localhost:5173). Press Control-C once to stop both
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
| `COMMUTE_HELP_GRAPH_PATH` | `data/graphs/portland-vancouver.graphml` | Generated routing graph |
| `COMMUTE_HELP_GRAPH_MANIFEST_PATH` | `data/graphs/graph-manifest.json` | Version and checksum manifest |

The frontend always calls relative `/api/...` URLs. Vite proxies those requests
to FastAPI, which keeps the application usable from LAN browsers.

## Build the regional road graph

The graph build downloads the driveable OpenStreetMap network inside the
committed `data/regions/portland-vancouver-v1.geojson` boundary. It can take
several minutes and uses substantial disk space. Generated GraphML, GeoParquet,
and Overpass cache files remain local and are ignored by Git.

```bash
npm run graph:build
```

The builder will not replace an existing graph unless the command is rerun as
`npm run graph:build -- --force`.

The builder adds explicit travel-time, lane, capacity, and penalty defaults,
preserves direction and OSM identities, then writes:

- `data/graphs/portland-vancouver.graphml`
- `data/graphs/nodes.parquet`
- `data/graphs/edges.parquet`
- `data/graphs/graph-manifest.json`
- `data/graphs/validation-report.json`

The manifest records checksums, graph version, source, region, artifact sizes,
and integrity metrics. Re-run the integrity report without downloading data:

```bash
npm run graph:validate
```

Restart `npm run dev` after a successful build. `/api/status` will report the
loaded graph version and counts, while `/api/graph/manifest` returns the
browser-safe build manifest.

## Checks

Run all current checks:

```bash
npm test
```

With the application running, the Phase 0 health gate is:

```bash
curl http://localhost:5173/api/health
```

The response reports both API and database readiness. `/api/status` reports
whether the routing graph is absent, ready, or failed validation/loading.

## Data and privacy

SQLite data, private environment settings, generated graphs, OSM extracts,
traffic data, and exports are local artifacts and must not be committed. Home
and work locations will be treated as sensitive data as later phases are built.

See [GAMEPLAN.md](GAMEPLAN.md) for the complete Max Viable Product plan and
[AGENTS.md](AGENTS.md) for the implementation contract.
