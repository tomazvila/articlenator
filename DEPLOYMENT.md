# Deployment Guide

Articlenator runs as a single Docker Compose service on the homelab host,
behind Caddy, at `articlenator.homelab.tomazvi.la`.

```
articlenator.homelab.tomazvi.la
            │
        Caddy (~/homelab/services/proxy/Caddyfile)
            │  reverse_proxy articlenator:5001
            ▼
 articlenator container (twitter-articlenator:latest)
  built on this host from ~/dev/articlenator via Nix
            │
      /srv/articlenator  (all state: user DB, cookies, PDFs, jobs)
```

There is no Kubernetes, no registry, and no remote nodes. The image is built
locally and loaded straight into Docker.

---

## Where Things Live

| What | Where |
|------|-------|
| Application repo | `~/dev/articlenator` |
| Compose stack | `~/homelab` (`compose.yaml` includes `services/articlenator/compose.yaml`) |
| Caddy proxy config | `~/homelab/services/proxy/Caddyfile` |
| Persistent state | `/srv/articlenator` (mounted at `/data`, owned by uid 10001) |
| Secrets | `~/homelab/.env` (`ARTICLENATOR_SECRET_KEY`, `ARTICLENATOR_COOKIE_ENCRYPTION_KEY`, `DOMAIN`) |

---

## Deploy a New Version

```bash
# 1. Commit and push the changes (CI must pass on GitHub)
cd ~/dev/articlenator
git push origin main

# 2. Build the image from the exact pushed commit (keeps deployed == pushed)
git worktree add /tmp/articlenator-deploy <commit-sha>
cd /tmp/articlenator-deploy
nix build .#docker

# 3. Back up the current image, then load the new one
docker tag twitter-articlenator:latest twitter-articlenator:before-<change>-$(date +%Y%m%d)
docker load < result

# 4. Recreate the service
cd ~/homelab
docker compose up -d articlenator

# 5. Clean up the worktree
git worktree remove --force /tmp/articlenator-deploy
```

Building from a worktree of the pushed commit is the recommended flow; the
working tree may contain unfinished work that has not passed CI.

## Verify the Deployment

```bash
# Health
curl https://articlenator.homelab.tomazvi.la/api/health
# → {"status":"ok"}

# Version (footer of any page)
curl -s https://articlenator.homelab.tomazvi.la/login | grep -o 'class="version">[^<]*'
# → matches the pushed pyproject.toml version and git commit
```

Also check after functional changes:

- The public setup guide: `GET /setup` returns 200 without login.
- Gated pages redirect to `/login` when logged out.
- `docker logs articlenator --tail 50` shows no tracebacks.

## Rollback

```bash
docker tag twitter-articlenator:before-<change>-YYYYMMDD twitter-articlenator:latest
cd ~/homelab && docker compose up -d articlenator
```

Old image tags are kept with a `before-<change>-<date>` naming convention.

## Managing Accounts

The app exposes a Flask CLI inside the container:

```bash
docker exec -it articlenator twitter-articlenator users create --username admin --admin
docker exec -it articlenator twitter-articlenator users set-password --username admin
docker exec -it articlenator twitter-articlenator users enable --username <name>
docker exec -it articlenator twitter-articlenator users disable --username <name>
```

Accounts live in `/srv/articlenator/config/articlenator.sqlite3` (never in Git).

## Notes

- The container runs as uid 10001, read-only root filesystem assumptions apply;
  all writable state must be under `/data`.
- `TWITTER_ARTICLENATOR_TRUSTED_HOSTS` is set to `articlenator.$DOMAIN` —
  requests with any other Host header fail closed.
- Configuration reference: [DOCKER.md](DOCKER.md). Account data layout:
  [MULTI_USER.md](MULTI_USER.md).
