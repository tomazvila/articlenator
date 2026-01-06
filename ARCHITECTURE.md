# Homelab Infrastructure Documentation

## Overview

This is a three-node Kubernetes (K3s) cluster designed for high availability and distributed storage across multiple physical locations. The infrastructure supports hosting Discord bots, web applications, databases, and monitoring services.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Tailscale Mesh VPN                       │
│         (Secure communication between all nodes)            │
└─────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   ┌────▼─────┐      ┌────▼─────┐      ┌────▼─────┐
   │  nixos   │      │dressedpi │      │srv1241853│
   │ (Laptop) │      │  (RPi 4) │      │  (VPS)   │
   │          │      │          │      │          │
   │ K3s      │      │ K3s      │      │ K3s      │
   │ Server   │      │ Agent    │      │ Agent    │
   │ +Worker  │      │ Worker   │      │ Worker   │
   │          │      │          │      │          │
   │ Storage: │      │ Storage: │      │ Storage: │
   │ 1.3TB SSD│      │ 1.8TB SSD│      │  None    │
   └──────────┘      └──────────┘      └──────────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
        ┌─────▼─────┐           ┌──────▼──────┐
        │ Longhorn  │           │  Syncthing  │
        │ (K8s PVs) │           │(Backup Data)│
        │ 2 replicas│           │ Bidirection │
        └───────────┘           └─────────────┘
```

## Nodes

| Node | Role | Tailscale IP | Local IP | OS | Hardware |
|------|------|--------------|----------|----|----|
| nixos | K3s server + worker | 100.82.212.53 | 192.168.0.X | NixOS 24.11 | Laptop |
| dressedpi | K3s worker | 100.81.123.70 | 192.168.1.32 | Raspberry Pi OS (Bookworm 64-bit) | Raspberry Pi 4 8GB |
| srv1241853 | K3s worker (quorum) | 100.82.198.59 | 72.61.136.5 | Debian 13 | VPS |

## Storage

### Longhorn (Kubernetes Persistent Volumes)

Longhorn provides distributed block storage for Kubernetes workloads. Data is replicated across nixos and dressedpi nodes.

| Node | Longhorn Path | Capacity | Scheduling |
|------|---------------|----------|------------|
| nixos | /mnt/ssdb/longhorn/ | ~1.3TB | Enabled |
| dressedpi | /mnt/ssd/longhorn/ | ~1.8TB | Enabled |
| srv1241853 | N/A | None | Disabled |

**Default replication:** 2 replicas (one on each storage node)

### Syncthing (Non-Kubernetes Data Sync)

Syncthing provides bidirectional sync of backup/personal data between the two SSDs.

| Node | Syncthing Path | Ignore Patterns |
|------|----------------|-----------------|
| nixos | /mnt/ssdb | /longhorn |
| dressedpi | /mnt/ssd | /longhorn |

**Folder ID:** `ssd-backup`

## Network Configuration

### Tailscale

All nodes communicate over Tailscale mesh VPN using WireGuard encryption.

- **Network interface:** `tailscale0`
- **K3s flannel interface:** `tailscale0`

### Firewall Ports (nixos)

| Port | Protocol | Purpose |
|------|----------|---------|
| 6443 | TCP | K3s API server |
| 10250 | TCP | Kubelet |
| 22000 | TCP/UDP | Syncthing |

## K3s Configuration

### Control Plane (nixos)

Location: `/etc/nixos/modules/k3s-server.nix`

```nix
services.k3s = {
  enable = true;
  role = "server";
  extraFlags = toString [
    "--node-ip=100.82.212.53"
    "--advertise-address=100.82.212.53"
    "--flannel-iface=tailscale0"
    "--disable=traefik"
  ];
};
```

### Workers (dressedpi, srv1241853)

Joined via:
```bash
curl -sfL https://get.k3s.io | K3S_URL=https://100.82.212.53:6443 K3S_TOKEN=<token> sh -s - agent --node-ip=<tailscale-ip> --flannel-iface=tailscale0
```

### Kubectl Access

On nixos:
```bash
# Config location
~/.kube/config

# Or use directly
sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml <command>
```

**Note:** All kubectl commands must be run from the nixos node (control plane).

## NixOS Configuration Structure

```
/etc/nixos/
├── flake.nix                 # Main flake entry point
├── flake.lock
├── configuration.nix         # Main system configuration
├── hardware-configuration.nix
└── modules/
    └── k3s-server.nix        # K3s server + Longhorn prerequisites
```

### Key NixOS Services

| Service | Purpose |
|---------|---------|
| services.tailscale | Mesh VPN |
| services.k3s | Kubernetes |
| services.openiscsi | Longhorn requirement |
| services.syncthing | Data sync |
| systemd.services.cloudflared | Cloudflare tunnel for rustimenator |

## SSH Access

| Node | SSH Command |
|------|-------------|
| nixos | `ssh lilvilla@100.82.212.53` or `ssh lilvilla@ssh.tomazvi.la` |
| dressedpi | `ssh dressedpiuser@100.81.123.70` or `ssh dressedpiuser@192.168.1.32` |
| srv1241853 | `ssh root@100.82.198.59` or `ssh root@72.61.136.5` |

## Useful Commands

### Cluster Status
```bash
# Check nodes
kubectl get nodes

# Check all pods
kubectl get pods -A

# Check Longhorn
kubectl -n longhorn-system get pods
kubectl -n longhorn-system get nodes.longhorn.io
```

### Longhorn Storage
```bash
# Check storage nodes
kubectl -n longhorn-system get nodes.longhorn.io -o wide

# Check disk configuration
kubectl -n longhorn-system get nodes.longhorn.io -o jsonpath='{range .items[*]}{.metadata.name}: {.spec.disks}{"\n"}{end}'

# Check replicas
kubectl -n longhorn-system get replicas -o wide
```

### Syncthing
```bash
# On nixos
systemctl status syncthing

# On dressedpi
systemctl status syncthing@dressedpiuser

# Web UI access (via SSH tunnel)
ssh -L 8384:127.0.0.1:8384 lilvilla@100.82.212.53
# Then open http://127.0.0.1:8384
```

### Service Management
```bash
# nixos
sudo systemctl restart k3s
sudo nixos-rebuild switch --flake /etc/nixos#nixos

# dressedpi
sudo systemctl restart k3s-agent

# srv1241853
sudo systemctl restart k3s-agent
```

## Deploying Applications

### Basic Deployment Example

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app
        image: my-app:latest
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: my-app
spec:
  selector:
    app: my-app
  ports:
  - port: 80
    targetPort: 8080
```

### With Persistent Storage (Longhorn)

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-app-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  replicas: 1  # Use 1 replica for RWO volumes
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app
        image: my-app:latest
        volumeMounts:
        - name: data
          mountPath: /data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: my-app-data
```

### PostgreSQL StatefulSet Example

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-pvc
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn
  resources:
    requests:
      storage: 20Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
      - name: postgres
        image: postgres:16
        env:
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: postgres-secret
              key: password
        - name: POSTGRES_DB
          value: myapp
        ports:
        - containerPort: 5432
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: postgres-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
spec:
  selector:
    app: postgres
  ports:
  - port: 5432
    targetPort: 5432
```

## External Access Options

### Option 1: Cloudflare Tunnel (Already configured for rustimenator)

Config location: `/home/lilvilla/.cloudflared/config.yml`

### Option 2: NodePort Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app-nodeport
spec:
  type: NodePort
  selector:
    app: my-app
  ports:
  - port: 80
    targetPort: 8080
    nodePort: 30080  # Access via any node IP:30080
```

### Option 3: Ingress (Traefik disabled, needs setup)

Traefik was disabled in K3s config. For ingress, either:
- Re-enable Traefik
- Install nginx-ingress
- Use Cloudflare tunnels per service

## Important Notes

1. **All kubectl commands run from nixos** - The control plane and kubeconfig are only on the NixOS laptop.

2. **Longhorn replicas = 2** - Data is replicated to nixos and dressedpi SSDs. If one node fails, data is preserved on the other.

3. **VPS (srv1241853) has no storage** - It only participates in cluster quorum, not data storage.

4. **Syncthing excludes /longhorn** - The Longhorn directories are managed separately and excluded from Syncthing sync.

5. **Recovery procedure** - All NixOS config is in `/etc/nixos/`. Rebuilding from scratch is possible with `nixos-rebuild switch --flake /etc/nixos#nixos`.

## Planned Services

| Service | Type | Storage | Status |
|---------|------|---------|--------|
| Discord bots | Python + SQLite | Longhorn PVC | Not deployed |
| Rustimenator | Rust + PostgreSQL | Longhorn PVC | Running via Docker (migrate to K8s) |
| NextJS Blog | Static/SSR | None or small PVC | Not deployed |
| Grafana | Monitoring | Longhorn PVC | Not deployed |
| Prometheus/Loki | Metrics/Logs | Longhorn PVC | Not deployed |

## Troubleshooting

### K3s Agent Not Connecting
```bash
# Check agent logs
sudo journalctl -u k3s-agent -f

# Verify Tailscale connectivity
ping 100.82.212.53  # From agent to server
```

### Longhorn Issues
```bash
# Check manager pods
kubectl -n longhorn-system logs -l app=longhorn-manager

# iscsiadm missing (NixOS specific)
# Symlink is created via activation script in k3s-server.nix
ls -la /usr/bin/iscsiadm
```

### Syncthing Not Syncing
```bash
# Check service status
systemctl status syncthing  # nixos
systemctl status syncthing@dressedpiuser  # pi

# Check folder permissions
ls -la /mnt/ssdb  # nixos
ls -la /mnt/ssd   # pi
```
