# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

The active application is **Telegram Backup** (`telegram-backup/`) — a Python app that backs up Telegram chats to a private channel, detects deleted messages, and provides a control bot + Flask REST API + web login UI. Run via the "Telegram Backup" workflow (`python telegram-backup/run.py`, port 5000). Requires secrets: `API_ID`, `API_HASH`, `BOT_TOKEN`, `BACKUP_CHANNEL_ID`, `ADMIN_CHAT_ID`. Python deps are managed with uv (pyproject.toml); note python-telegram-bot 13.15 pins apscheduler==3.6.3.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
