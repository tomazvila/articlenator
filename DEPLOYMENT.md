# High Availability Deployment Guide

Deploy your twitter service at `tomazvi.la/twitter` with automatic failover.

---

## Your Nodes

| Node | IP | SSH Command |
|------|-----|-------------|
| nixos | 100.82.212.53 | `ssh lilvilla@100.82.212.53` |
| dressedpi | 100.81.123.70 | `ssh dressedpiuser@100.81.123.70` |
| srv1241853 (VPS) | 100.82.198.59 | `ssh root@100.82.198.59` |

**Important:** All `kubectl` commands run on **nixos** only.

---

## What You're Building

```
                         tomazvi.la/twitter
                                │
                    ┌───────────▼───────────┐
                    │   Cloudflare Tunnel   │
                    │  (automatic failover) │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
       ┌────────────┐    ┌────────────┐    ┌────────────┐
       │   nixos    │    │ dressedpi  │    │    VPS     │
       │            │    │            │    │            │
       │ cloudflared│    │ cloudflared│    │  QUORUM    │
       │ twitter-svc│    │ twitter-svc│    │   ONLY     │
       │  storage   │    │  storage   │    │ no pods    │
       └────────────┘    └────────────┘    └────────────┘
```

- **nixos & dressedpi**: Run your apps and store data
- **VPS**: Only votes in cluster decisions (prevents split-brain)

---

# PART 1: Set Up 3-Node Control Plane

## Step 1.1: Get the Cluster Token

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

sudo cat /var/lib/rancher/k3s/server/token
```

**Copy this token** — you need it for the next two steps.

---

## Step 1.2: Add VPS to Control Plane

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: srv1241853 (VPS)        │
# │  ssh root@100.82.198.59          │
# └──────────────────────────────────┘

sudo systemctl stop k3s-agent
sudo systemctl disable k3s-agent

curl -sfL https://get.k3s.io | K3S_TOKEN=<YOUR-TOKEN> sh -s - server \
    --server https://100.82.212.53:6443 \
    --node-ip=100.82.198.59 \
    --flannel-iface=tailscale0 \
    --disable=traefik
```

Wait 30 seconds, then verify:
```bash
sudo systemctl status k3s
```

---

## Step 1.3: Add dressedpi to Control Plane

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: dressedpi               │
# │  ssh dressedpiuser@100.81.123.70 │
# └──────────────────────────────────┘

sudo systemctl stop k3s-agent
sudo systemctl disable k3s-agent

curl -sfL https://get.k3s.io | K3S_TOKEN=<YOUR-TOKEN> sh -s - server \
    --server https://100.82.212.53:6443 \
    --node-ip=100.81.123.70 \
    --flannel-iface=tailscale0 \
    --disable=traefik
```

Wait 30 seconds, then verify:
```bash
sudo systemctl status k3s
```

---

## Step 1.4: Update nixos Configuration

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

sudo nano /etc/nixos/modules/k3s-server.nix
```

Replace contents with:

```nix
services.k3s = {
  enable = true;
  role = "server";
  clusterInit = true;
  extraFlags = toString [
    "--node-ip=100.82.212.53"
    "--advertise-address=100.82.212.53"
    "--flannel-iface=tailscale0"
    "--disable=traefik"
    "--cluster-cidr=10.42.0.0/16"
    "--service-cidr=10.43.0.0/16"
    "--node-label=workload=enabled"
  ];
};
```

Apply:
```bash
sudo nixos-rebuild switch --flake /etc/nixos#nixos
```

---

## Step 1.5: Configure Which Nodes Run Apps

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

# Allow apps on nixos and dressedpi
kubectl label nodes nixos workload=enabled
kubectl label nodes dressedpi workload=enabled

# Block apps on VPS (quorum only)
kubectl taint nodes srv1241853 quorum-only=true:NoSchedule
```

Verify all 3 nodes are Ready:
```bash
kubectl get nodes
```

Expected:
```
NAME          STATUS   ROLES                  AGE
nixos         Ready    control-plane,master   ...
dressedpi     Ready    control-plane,master   ...
srv1241853    Ready    control-plane,master   ...
```

---

# PART 2: Deploy Cloudflare Tunnel

## Step 2.1: Create Tunnel in Cloudflare

1. Go to https://one.dash.cloudflare.com/
2. Click **Networks → Tunnels → Create a tunnel**
3. Name: `k8s-tunnel`
4. Select **Cloudflared**
5. **Copy the token** (starts with `eyJ...`)

---

## Step 2.2: Store Token in Kubernetes

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

kubectl create secret generic cloudflared-token \
  --from-literal=token=<YOUR-TUNNEL-TOKEN>
```

---

## Step 2.3: Deploy Everything

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

cat << 'EOF' | kubectl apply -f -
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared
spec:
  replicas: 2
  selector:
    matchLabels:
      app: cloudflared
  template:
    metadata:
      labels:
        app: cloudflared
    spec:
      nodeSelector:
        workload: enabled
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchLabels:
                app: cloudflared
            topologyKey: kubernetes.io/hostname
      containers:
      - name: cloudflared
        image: cloudflare/cloudflared:latest
        args: ["tunnel", "--no-autoupdate", "run", "--token", "$(TUNNEL_TOKEN)"]
        env:
        - name: TUNNEL_TOKEN
          valueFrom:
            secretKeyRef:
              name: cloudflared-token
              key: token
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: twitter-service
spec:
  replicas: 2
  selector:
    matchLabels:
      app: twitter-service
  template:
    metadata:
      labels:
        app: twitter-service
    spec:
      nodeSelector:
        workload: enabled
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchLabels:
                  app: twitter-service
              topologyKey: kubernetes.io/hostname
      containers:
      - name: twitter
        image: your-twitter-image:latest
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: twitter-service
spec:
  selector:
    app: twitter-service
  ports:
  - port: 80
    targetPort: 8080
EOF
```

**Change `your-twitter-image:latest`** to your actual Docker image.

---

## Step 2.4: Verify Pods Are Running

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

kubectl get pods -o wide
```

You should see:
- 2 cloudflared pods (one on nixos, one on dressedpi)
- 2 twitter-service pods (one on nixos, one on dressedpi)
- **Nothing on srv1241853**

---

# PART 3: Configure Cloudflare Routing

## Step 3.1: Add Route in Cloudflare Dashboard

1. Go to https://one.dash.cloudflare.com/
2. Click **Networks → Tunnels → your tunnel → Public Hostname**
3. Click **Add a public hostname**
4. Fill in:

| Field | Value |
|-------|-------|
| Subdomain | *(leave empty)* |
| Domain | `tomazvi.la` |
| Path | `/twitter` |
| Type | HTTP |
| URL | `twitter-service.default.svc.cluster.local:80` |

5. Click **Save**

---

## Step 3.2: Enable HTTPS

1. Go to https://dash.cloudflare.com/
2. Select **tomazvi.la**
3. Click **SSL/TLS → Overview**
4. Set to **Full (strict)**

---

# PART 4: Disable Old Cloudflared

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

sudo systemctl stop cloudflared
sudo systemctl disable cloudflared
```

---

# PART 5: Test It

## Check Everything is Running

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

kubectl get nodes                    # All 3 should be Ready
kubectl get pods -o wide             # 4 pods on nixos/dressedpi only
kubectl logs -l app=cloudflared      # No errors
```

## Test the URL

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: any computer            │
# └──────────────────────────────────┘

curl https://tomazvi.la/twitter/
```

## Test Failover (Optional)

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

# Simulate nixos failure
kubectl drain nixos --ignore-daemonsets --delete-emptydir-data

# Test from another computer - should still work!
curl https://tomazvi.la/twitter/

# Restore nixos
kubectl uncordon nixos
```

---

# Quick Reference

## Where to Run Commands

| Command | Run On |
|---------|--------|
| `kubectl ...` | nixos |
| `sudo systemctl ...` | The node you're managing |

## What Runs Where

| Node | Runs Apps? | Why |
|------|------------|-----|
| nixos | Yes | Has `workload=enabled` label |
| dressedpi | Yes | Has `workload=enabled` label |
| srv1241853 | No | Has `quorum-only` taint (voting only) |

## Backup (Run Monthly)

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

sudo k3s etcd-snapshot save --name backup-$(date +%Y%m%d)
```

Snapshots saved to: `/var/lib/rancher/k3s/server/db/snapshots/`

---

# PART 6: CI/CD Setup (GitHub Actions)

Automate builds and deployments with GitHub Actions.

---

## Step 6.1: Architecture Overview

```
     GitHub Actions (on push to main)
                    │
     ┌──────────────┼──────────────┐
     │              │              │
     ▼              ▼              ▼
  Run Tests    Build Image    Push to ghcr.io
                    │
                    ▼
           Deploy to K8s cluster
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
     nixos                  dressedpi
   (x86_64)                (aarch64)
```

**Multi-arch support:** The CI/CD pipeline builds for both x86_64 (nixos) and ARM64 (dressedpi/Raspberry Pi) using QEMU emulation. The images are combined into a multi-arch manifest, so Kubernetes automatically pulls the correct architecture.

---

## Step 6.2: Get Kubeconfig from nixos

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

# Copy the kubeconfig
sudo cat /etc/rancher/k3s/k3s.yaml

# Replace 127.0.0.1 with the Tailscale IP for remote access
# Change: server: https://127.0.0.1:6443
# To:     server: https://100.82.212.53:6443
```

Save this modified kubeconfig locally.

---

## Step 6.3: Configure GitHub Secrets

1. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Add these secrets:

| Secret | Value | How to get it |
|--------|-------|---------------|
| `KUBECONFIG` | Base64-encoded kubeconfig | `cat kubeconfig.yaml \| base64` |

The `GITHUB_TOKEN` is automatic and provides ghcr.io access.

---

## Step 6.4: Create GitHub Environment

1. Go to your GitHub repo → **Settings** → **Environments**
2. Click **New environment**
3. Name: `production`
4. (Optional) Add protection rules:
   - Require reviewers for deployments
   - Limit to `main` branch only

---

## Step 6.5: Building Images Locally (Alternative)

If you prefer to build images locally instead of using CI/CD:

### On nixos (x86_64):

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

cd /path/to/twitter-articlenator

# Pull latest code
git pull origin main

# Build the Docker image
nix build .#docker

# Load into Docker/containerd
sudo k3s ctr images import result

# Or if using Docker
docker load < result
docker tag twitter-articlenator:latest ghcr.io/YOUR_USERNAME/twitter-articlenator:latest
```

### On dressedpi (aarch64 - Raspberry Pi):

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: dressedpi               │
# └──────────────────────────────────┘

cd /path/to/twitter-articlenator

# Pull latest code
git pull origin main

# Install Nix if not already installed
curl -L https://nixos.org/nix/install | sh

# Build ARM64 Docker image
nix build .#docker

# Load into k3s containerd
sudo k3s ctr images import result
```

---

## Step 6.6: Update Deployment for CI/CD

Once CI/CD is pushing to ghcr.io, update your deployment:

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

# Create registry secret for ghcr.io (if repo is private)
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_GITHUB_PAT \
  --docker-email=your@email.com

# Apply the updated manifests
kubectl apply -f k8s/twitter-app.yaml
```

---

## Step 6.7: Verify CI/CD Pipeline

After pushing to main:

1. Go to GitHub repo → **Actions** tab
2. Watch the workflow run
3. Check deployment status:

```bash
# ┌──────────────────────────────────┐
# │  RUN ON: nixos                   │
# └──────────────────────────────────┘

# Watch the rollout
kubectl rollout status deployment/twitter-articlenator

# Check pods are running new version
kubectl get pods -l app=twitter-articlenator -o wide

# Verify the image
kubectl describe pod -l app=twitter-articlenator | grep Image:
```

---

# Troubleshooting

## Pods stuck in Pending?
```bash
kubectl describe pod <pod-name>
```
Look at Events section for the reason.

## Cloudflare tunnel not connecting?
```bash
kubectl logs -l app=cloudflared
```

## Node not Ready?
```bash
# On that specific node:
sudo systemctl status k3s
sudo journalctl -u k3s -f
```

## CI/CD image pull failed?
```bash
# Check if secret exists
kubectl get secret ghcr-secret

# Check pod events
kubectl describe pod <pod-name> | grep -A5 Events

# Verify image exists in registry
docker pull ghcr.io/YOUR_USERNAME/twitter-articlenator:main
```

## ARM64 / Raspberry Pi image issues?
The CI/CD builds multi-arch images (x86_64 + ARM64). If you still have issues:
1. Verify the manifest exists: `docker manifest inspect ghcr.io/YOUR_USERNAME/twitter-articlenator:main`
2. Check containerd on dressedpi: `sudo k3s ctr images ls | grep twitter`
3. Force re-pull: `kubectl rollout restart deployment/twitter-articlenator`
