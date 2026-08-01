# Commute Help frontend

This directory contains the React, TypeScript, and Vite frontend for Commute
Help. Daily development is orchestrated from the repository root:

```bash
npm run dev
```

Do not start this package by itself during normal use. The root launcher also
starts FastAPI, and Vite proxies relative `/api/...` requests to that backend.

See the repository-level [`README.md`](../README.md) for setup, testing, and LAN
instructions.
