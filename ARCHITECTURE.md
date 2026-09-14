# Architecture

## Hosting Reality

Articlenator runs as **one Docker Compose service on the homelab host**, behind
Caddy, at `articlenator.homelab.tomazvi.la`.

There is no Kubernetes, no Tailscale mesh, and no secondary nodes. The old
three-node K3s cluster (nixos laptop + Raspberry Pi worker + VPS quorum node,
Longhorn storage, Cloudflare tunnel) has been retired.

```
articlenator.homelab.tomazvi.la
            │
        Caddy  (~/homelab compose stack)
            │  reverse_proxy articlenator:5001
            ▼
articlenator container  (twitter-articlenator:latest, uid 10001)
  │ built on this host: cd ~/dev/articlenator && nix build .#docker
  ▼
/srv/articlenator  →  /data   (user DB, cookies, PDFs, videos, transcripts)
```

Deployment, verification, rollback, and account management:
[DEPLOYMENT.md](DEPLOYMENT.md).

## Application Architecture

Single Flask process (`twitter_articlenator.app:create_app`), Nix-built Python
3.14 environment, multi-user with local SQLite accounts.

```
src/twitter_articlenator/
├── app.py                 # App factory: config, auth, limits, CSP, blueprints
├── auth.py                # UserStore (SQLite), session auth, CSRF gate, rate limiter
├── security.py            # CSRF tokens, trusted hosts
├── config.py              # Env-driven configuration
├── resource_limits.py     # Per-user/global leases for playwright/download/transcription
├── user_data.py           # Per-account data paths under /data/users/<uuid>
├── routes/
│   ├── pages.py           # HTML pages (index, setup, bookmarks, videos, youtube)
│   ├── auth.py            # Login/logout, admin user management, account CLI
│   ├── api.py             # Convert, bookmarks, videos, YouTube, sessions (SSE streaming)
│   ├── ladder.py          # 13ft reader: /13ft page, /api/13ft, /api/13ft/pdf
│   ├── transcription.py   # Whisper.cpp transcription jobs
│   └── channel.py         # Channel crawl → PDFs
├── sources/               # Fetchers: twitter/web articles, ladder core (13ft),
│                          # botasaurus browser fallback
├── pdf/                   # WeasyPrint e-reader PDF generation
└── static/, templates/    # Tokyo Night UI, library-tunnel background
```

Key properties:

- **Public surface**: only `/login`, `/setup` (static guide), `/api/health`,
  static assets. Everything else requires a session; unsafe methods require CSRF.
- **Heavy work is lease-limited**: browser automation, downloads, and
  transcription each have per-account and global capacity limits.
- **State is per-account**: `/data/users/<uuid>/…`; credentials are encrypted
  at rest (see [MULTI_USER.md](MULTI_USER.md)).
- **Image**: built by the flake (`nix build .#docker`), includes Chromium for
  Playwright and the Xvfb virtual display used by the 13ft browser fallback.
