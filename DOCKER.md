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
  -p 5001:5001 \
  -v twitter-articlenator-data:/data \
  -e TWITTER_ARTICLENATOR_SECRET_KEY="$(nix develop --command python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY="$(nix develop --command python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  -e TWITTER_ARTICLENATOR_REQUIRE_COOKIE_ENCRYPTION=true \
  twitter-articlenator:latest

# View logs
docker logs -f twitter-articlenator

# Access the web UI
open http://localhost:5001
```

## Kubernetes Deployment

### Quick Start

```bash
# Create required app secrets first
FLASK_SECRET_KEY="$(nix develop --command python -c 'import secrets; print(secrets.token_urlsafe(48))')"
YOUTUBE_COOKIE_KEY="$(nix develop --command python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
kubectl create secret generic twitter-articlenator-secrets \
  --from-literal=flask-secret-key="$FLASK_SECRET_KEY" \
  --from-literal=youtube-cookie-encryption-key="$YOUTUBE_COOKIE_KEY"

# Apply the manifests
kubectl apply -f k8s/twitter-app.yaml

# Check status
kubectl get pods -l app=twitter-articlenator
kubectl logs -l app=twitter-articlenator -f
```

### Configuration

Edit `k8s/twitter-app.yaml` to customize:

- **Ingress host**: Change `articlenator.example.com` to your domain
- **Storage**: Adjust PVC size (default: 1Gi)
- **Resources**: Tune CPU/memory limits based on your cluster

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TWITTER_ARTICLENATOR_JSON_LOGGING` | `true` | JSON logs for Kubernetes |
| `TWITTER_ARTICLENATOR_OUTPUT_DIR` | `/data/output` | Generated PDFs |
| `TWITTER_ARTICLENATOR_CONFIG_DIR` | `/data/config` | Server-side cookie metadata and encrypted YouTube cookie storage |
| `TWITTER_ARTICLENATOR_SECRET_KEY` | required in deployment | Flask session signing key for CSRF/session state |
| `TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY` | required when encryption is enforced | Fernet key for encrypted YouTube cookie storage |
| `TWITTER_ARTICLENATOR_REQUIRE_COOKIE_ENCRYPTION` | `false` | Set to `true` in deployment so persistent YouTube cookies cannot be saved as plaintext |
| `TWITTER_ARTICLENATOR_SESSION_COOKIE_SECURE` | `false` | Set to `true` behind HTTPS ingress/tunnel |

### Persistent Data

The `/data` volume contains:
- `/data/config/youtube-cookies.txt` - encrypted YouTube cookie blob when uploaded through the UI/API
- `/data/config/youtube-cookies.json` - metadata only; no raw cookie values
- `/data/output/*.pdf` - Generated PDF files

YouTube cookie rotation is done through the YouTube page: upload a new `cookies.txt`,
verify it, and the previous encrypted blob is overwritten. Do not put YouTube cookies
in Kubernetes manifests, ConfigMaps, image layers, CI logs, or Git.

## Image Details

- **Base**: Nix-built (no traditional base image)
- **Size**: ~1.5GB (includes Chromium for Playwright)
- **Exposed Port**: 5001
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
