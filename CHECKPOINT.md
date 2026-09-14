# Project Checkpoint

Last updated: 2026-09-14

## Current State

**Single-host Docker Compose deployment, CI on GitHub Actions.**

- Articlenator runs on the homelab host as one container behind Caddy at
  `articlenator.homelab.tomazvi.la`; state in `/srv/articlenator`.
- The old 3-node K3s cluster (nixos + Raspberry Pi + VPS quorum, Longhorn,
  Cloudflare tunnel, Tailscale mesh) is retired.
- CI (`.github/workflows/ci.yml`) runs on every push to `main`: unit,
  integration, and browser suites, lint + format, flake check, and a container
  build/smoke job.
- Deployment is manual and simple: `nix build .#docker`, `docker load`,
  `docker compose up -d articlenator`. See [DEPLOYMENT.md](DEPLOYMENT.md).
- Accounts are managed with the in-container CLI
  (`twitter-articlenator users ...`); data is per-account under `/data`.

## Feature Status

- Article → e-reader PDF conversion, bookmarks, video + YouTube downloading,
  whisper.cpp transcription, channel crawls: live.
- 13ft reader (`/13ft`) with Botasaurus browser fallback and archive
  fallbacks: live.
- Public pages: `/login`, `/setup` guide, `/api/health`; everything else is
  login-gated.
