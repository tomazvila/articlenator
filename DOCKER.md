# Docker & Kubernetes Deployment

## Building the Docker Image

The Docker image is built using Nix for reproducible builds. It only works on Linux systems.

### On a Linux machine:

```bash
# Build the Docker image
nix build .#docker

# Load the image into Docker
docker load < result

# Tag for your registry (optional)
docker tag twitter-articlenator:latest your-registry.com/twitter-articlenator:latest
docker push your-registry.com/twitter-articlenator:latest
```

### Cross-compile from macOS (using remote builder):

```bash
# If you have a Linux remote builder configured
nix build .#packages.x86_64-linux.docker --builders 'ssh://your-linux-builder'

# Or use a Linux VM/container
```

## Running with Docker

```bash
# Run the container
docker run -d \
  --name twitter-articlenator \
  -p 5000:5000 \
  -v twitter-articlenator-data:/data \
  twitter-articlenator:latest

# View logs
docker logs -f twitter-articlenator

# Access the web UI
open http://localhost:5000
```

## Kubernetes Deployment

### Quick Start

```bash
# Apply the manifests
kubectl apply -f k8s/deployment.yaml

# Check status
kubectl get pods -l app=twitter-articlenator
kubectl logs -l app=twitter-articlenator -f
```

### Configuration

Edit `k8s/deployment.yaml` to customize:

- **Ingress host**: Change `articlenator.example.com` to your domain
- **Storage**: Adjust PVC size (default: 1Gi)
- **Resources**: Tune CPU/memory limits based on your cluster

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TWITTER_ARTICLENATOR_JSON_LOGGING` | `true` | JSON logs for Kubernetes |
| `TWITTER_ARTICLENATOR_CONFIG_DIR` | `/data/config` | Cookie storage |
| `TWITTER_ARTICLENATOR_OUTPUT_DIR` | `/data/output` | Generated PDFs |

### Persistent Data

The `/data` volume contains:
- `/data/config/cookies.json` - Twitter authentication cookies
- `/data/output/*.pdf` - Generated PDF files

## Image Details

- **Base**: Nix-built (no traditional base image)
- **Size**: ~1.5GB (includes Chromium for Playwright)
- **Exposed Port**: 5000
- **Health Check**: `GET /api/health`

## Monitoring

The application outputs JSON-structured logs suitable for:
- Kubernetes log aggregation (Loki, Elasticsearch)
- Prometheus metrics (via log parsing)

Example log entry:
```json
{
  "event": "article_converted",
  "level": "info",
  "timestamp": "2025-12-29T10:30:45.123456Z",
  "url": "https://x.com/user/status/123",
  "duration_ms": 1234
}
```
