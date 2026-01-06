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
