# Project Checkpoint

Last updated: 2026-01-06

## Current State: HA Cluster Complete, CI/CD Pending

### Infrastructure (DONE)

**3-node HA K3s cluster** with embedded etcd:

| Node | IP (Tailscale) | Role |
|------|----------------|------|
| nixos | 100.82.212.53 | control-plane, runs workloads |
| dressedpi | 100.81.123.70 | control-plane, runs workloads |
| srv1241853 | 100.82.198.59 | quorum only (no workloads) |

**Storage**: Longhorn on SSDs
- nixos: `/mnt/ssdb/longhorn/`
- dressedpi: `/mnt/ssd/longhorn/`

**Ingress**: Cloudflare Tunnel (2 replicas with anti-affinity)

**App URL**: https://twitter.tomazvi.la/

**Failover**: Tested and working

---

## Next Task: CI/CD Setup

### Goal
Auto-deploy on push to main with tests

### Architecture
```
GitHub Push to main
        |
        v
+------------------------+
|  nixos (self-hosted    |
|  GitHub Actions runner)|
|                        |
|  1. Run tests (uv)     |
|  2. Build x86_64 image |
|  3. SSH to dressedpi   |
|     to build arm64     |
|  4. kubectl rollout    |
+------------------------+
```

### Blocker
GitHub Actions self-hosted runner on nixos was **commented out** - started failing.

### To Resume

1. SSH to nixos:
   ```bash
   ssh lilvilla@100.82.212.53
   ```

2. Check runner status:
   ```bash
   systemctl status github-runner-*
   journalctl -u github-runner-* -n 50
   ```

3. View/fix NixOS config:
   ```bash
   vim /etc/nixos/configuration.nix
   # Find and uncomment github-runner section
   ```

4. Rebuild:
   ```bash
   sudo nixos-rebuild switch --flake /etc/nixos#nixos
   ```

5. Create workflow file:
   ```
   .github/workflows/deploy.yml
   ```

---

## Key Files

| File | Purpose |
|------|---------|
| `k8s/twitter-app.yaml` | K8s deployment (PVC, Deployment, Service) |
| `k8s/longhorn-pvc.yaml` | Longhorn storage claim |
| `flake.nix` | Nix build for Docker images |
| `pyproject.toml` | Python deps (flask, playwright, weasyprint) |
| `DEPLOYMENT.md` | Full cluster setup guide |

## Quick Reference

```bash
# Check cluster
kubectl get nodes
kubectl get pods -o wide

# Check app
curl https://twitter.tomazvi.la/

# Build Docker image (on each arch)
nix build .#docker
sudo k3s ctr images import result

# Restart deployment
kubectl rollout restart deployment/twitter-articlenator
```
