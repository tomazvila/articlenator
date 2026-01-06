# Twitter Articlenator

Convert Twitter/X content (tweets, threads, and articles) and web articles to e-reader friendly PDFs.

## Features

- **Twitter/X Support**: Convert tweets, threads, and long-form articles to PDF using Playwright with stealth mode
- **Web Articles**: Supports any HTTP(S) web article with smart content extraction
- **E-Reader Optimized**: Clean, readable PDFs designed for Kindle, Kobo, and other e-readers

## Quick Start

### Prerequisites

- [Nix](https://nixos.org/download.html) with flakes enabled
- Twitter/X account (for Twitter content)

### Run the Application

```bash
# Enter development shell (auto-installs dependencies)
nix develop

# Start the server
uv run twitter-articlenator
```

Open http://localhost:5001 in your browser.

### Set Up Twitter Cookies

1. Go to http://localhost:5001/setup
2. Follow the browser-specific instructions to extract cookies
3. Paste your `auth_token` and `ct0` cookies
4. Click "Test Cookies" to verify they work

### Convert Articles

1. Go to http://localhost:5001
2. Paste article URLs (one per line)
3. Click "Convert to PDF"
4. Download the generated PDFs

## Supported Sources

| Source | URL Pattern | Auth Required |
|--------|-------------|---------------|
| Twitter/X Tweets & Threads | `https://x.com/*/status/*` | Yes (cookies) |
| Twitter/X Long-form Articles | `https://x.com/*/status/*` | Yes (cookies) |
| Twitter/X (legacy domain) | `https://twitter.com/*/status/*` | Yes (cookies) |
| Any HTTP(S) | General web articles | No |

Twitter Articles (long-form content) are automatically detected and formatted with proper article styling.

## Architecture

```
src/twitter_articlenator/
├── app.py                  # Flask application factory
├── config.py               # Configuration management
├── logging.py              # Structured logging (structlog + orjson)
├── routes/
│   ├── api.py              # API endpoints blueprint
│   └── pages.py            # HTML pages blueprint
├── sources/
│   ├── base.py             # ContentSource Protocol & Article dataclass
│   ├── browser_pool.py     # Playwright browser pooling with stealth
│   ├── twitter_playwright.py  # Twitter/X source using Playwright
│   └── web.py              # Generic web article source
├── pdf/
│   └── generator.py        # WeasyPrint PDF generation (50MB limit)
├── templates/              # Jinja2 HTML templates
└── static/
    └── style.css           # Tokyo Night themed styles
```

### Key Components

#### Content Sources (`sources/`)

All sources implement the `ContentSource` Protocol (PEP 544):

```python
from typing import Protocol

class ContentSource(Protocol):
    def can_handle(self, url: str) -> bool:
        """Check if this source can handle the given URL."""
        ...

    async def fetch(self, url: str) -> Article:
        """Fetch and parse content from the URL."""
        ...
```

Sources are checked in order:
1. `TwitterPlaywrightSource` - Handles x.com and twitter.com URLs
2. `WebArticleSource` - Fallback for any HTTP(S) URL

#### Browser Pool (`sources/browser_pool.py`)

Manages reusable Playwright browser instances with stealth features:
- WebDriver property removal
- Plugin/language spoofing
- Chrome runtime emulation
- WebGL vendor spoofing
- Randomized viewport dimensions

#### PDF Generation (`pdf/generator.py`)

Uses WeasyPrint to convert HTML to PDF:
- E-reader optimized styles (large fonts, good margins)
- 50MB content size limit
- Automatic filename generation from title/date

#### Security Headers

All responses include security headers:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=()`

### Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `TWITTER_ARTICLENATOR_CONFIG_DIR` | `~/.config/twitter-articlenator` | Config/cookies storage |
| `TWITTER_ARTICLENATOR_OUTPUT_DIR` | `~/Downloads/twitter-articles` | PDF output directory |
| `TWITTER_ARTICLENATOR_LOG_LEVEL` | `INFO` | Logging level |
| `TWITTER_ARTICLENATOR_JSON_LOGGING` | `true` | Enable JSON log format |
| `PORT` | `5001` | Server port |
| `SECRET_KEY` | `dev-secret-key` | Flask secret key |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Main UI |
| GET | `/setup` | Cookie setup page |
| GET | `/api/health` | Health check |
| POST | `/api/convert` | Convert URLs to PDFs |
| GET | `/api/cookies/status` | Check cookie status |
| GET | `/api/cookies/status?test=true` | Validate cookie format |
| POST | `/api/cookies` | Save cookies |
| GET | `/api/cookies/current` | Get current cookies (masked) |
| GET | `/download/<filename>` | Download generated PDF |

## Deployment

### Kubernetes (Homelab)

Complete guide for deploying to your homelab Kubernetes cluster.

#### Step 1: Build the Docker Image

The Docker image must be built on Linux (or using a Linux remote builder):

```bash
# On a Linux machine
nix build .#docker
docker load < result

# Verify the image
docker images | grep twitter-articlenator
```

#### Step 2: Push to Your Registry

Push the image to a registry accessible by your cluster:

```bash
# Option A: Private registry
docker tag twitter-articlenator:latest registry.homelab.local/twitter-articlenator:latest
docker push registry.homelab.local/twitter-articlenator:latest

# Option B: Docker Hub
docker tag twitter-articlenator:latest yourusername/twitter-articlenator:latest
docker push yourusername/twitter-articlenator:latest

# Option C: Load directly on cluster nodes (k3s/single node)
docker save twitter-articlenator:latest | ssh node1 'docker load'
```

#### Step 3: Customize the Deployment

Edit `k8s/deployment.yaml` for your cluster:

```bash
# Update the image reference if using a registry
sed -i 's|image: twitter-articlenator:latest|image: registry.homelab.local/twitter-articlenator:latest|' k8s/deployment.yaml

# Update the ingress hostname
sed -i 's|articlenator.example.com|articlenator.homelab.local|' k8s/deployment.yaml
```

Or manually edit these values:
- `spec.template.spec.containers[0].image` - Your registry image path
- `spec.rules[0].host` in the Ingress - Your domain/hostname

#### Step 4: Deploy to Kubernetes

```bash
# Create namespace (optional)
kubectl create namespace articlenator

# Apply the manifests
kubectl apply -f k8s/deployment.yaml -n articlenator

# Or apply to default namespace
kubectl apply -f k8s/deployment.yaml
```

#### Step 5: Verify Deployment

```bash
# Check pod status
kubectl get pods -l app=twitter-articlenator
kubectl logs -l app=twitter-articlenator -f

# Check service
kubectl get svc twitter-articlenator

# Check ingress
kubectl get ingress twitter-articlenator

# Test health endpoint (port-forward if no ingress)
kubectl port-forward svc/twitter-articlenator 5000:80
curl http://localhost:5000/api/health
```

#### Step 6: Set Up Twitter Cookies

Access the web UI and configure your cookies:

```bash
# If using ingress
open http://articlenator.homelab.local/setup

# If using port-forward
kubectl port-forward svc/twitter-articlenator 5000:80
open http://localhost:5000/setup
```

#### Included Manifests

The `k8s/deployment.yaml` includes:

| Resource | Description |
|----------|-------------|
| Deployment | Single replica with health checks, 512Mi-2Gi memory |
| Service | ClusterIP on port 80 → container 5000 |
| PVC | 1Gi storage for cookies and PDFs |
| Ingress | HTTP routing (configure for your ingress controller) |

#### Ingress Controller Notes

For **Traefik** (k3s default):
```yaml
annotations:
  traefik.ingress.kubernetes.io/router.entrypoints: web
```

For **nginx-ingress**:
```yaml
annotations:
  nginx.ingress.kubernetes.io/proxy-body-size: "100m"
```

For **Cloudflare Tunnel**:
```yaml
annotations:
  cloudflare-tunnel.com/tunnel: "your-tunnel-id"
```

#### Persistent Data

Data is stored in the PVC at `/data`:
- `/data/config/cookies.json` - Twitter authentication
- `/data/output/*.pdf` - Generated PDFs

To backup:
```bash
kubectl cp articlenator/$(kubectl get pod -l app=twitter-articlenator -o jsonpath='{.items[0].metadata.name}'):/data ./backup
```

See [DOCKER.md](DOCKER.md) for additional Docker deployment options.

---

### Local Docker (Alternative)

Run directly with Docker:

```bash
# Build on Linux
nix build .#docker
docker load < result

# Run the container
docker run -d \
  --name twitter-articlenator \
  -p 5000:5000 \
  -v twitter-articlenator-data:/data \
  twitter-articlenator:latest

# Access at http://localhost:5000
```

### Production with Gunicorn

```bash
# Install gunicorn
uv add gunicorn

# Run with multiple workers
uv run gunicorn -w 4 -b 0.0.0.0:5001 'twitter_articlenator.app:create_app()'
```

### Reverse Proxy (nginx)

```nginx
server {
    listen 80;
    server_name articlenator.example.com;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Logging

The application uses [structlog](https://www.structlog.org/) for structured logging.

### Log Format

Production logs are JSON formatted:

```json
{
  "event": "article_converted",
  "level": "info",
  "timestamp": "2025-12-29T10:30:45.123456Z",
  "filename": "twitter_playwright.py",
  "func_name": "fetch",
  "lineno": 142,
  "url": "https://x.com/user/status/123",
  "duration_ms": 1234
}
```

### Development Logging

```bash
# Force console logging (human-readable)
TWITTER_ARTICLENATOR_JSON_LOGGING=false uv run twitter-articlenator
```

## Development

### Run Tests

```bash
# Enter dev shell
nix develop

# All tests (excluding E2E)
uv run pytest tests/unit tests/integration

# With coverage report
uv run pytest --cov=twitter_articlenator --cov-report=html

# Unit tests only
uv run pytest tests/unit

# Integration tests only
uv run pytest tests/integration

# E2E tests (requires running server)
uv run pytest tests/e2e
```

**Current test coverage: 74%** (232 tests)

### Project Structure

```
twitterArticleNator/
├── flake.nix              # Nix flake (dev shell, packages, Docker)
├── pyproject.toml         # Python project config
├── uv.lock                # Dependency lock file
├── src/
│   └── twitter_articlenator/
├── tests/
│   ├── unit/              # Unit tests
│   ├── integration/       # Flask route tests
│   └── e2e/               # Playwright browser tests
├── k8s/
│   └── deployment.yaml    # Kubernetes manifests
├── DOCKER.md              # Docker/K8s deployment guide
├── REPORT.md              # Code quality report
└── README.md
```

### Adding a New Source

1. Create `sources/mysource.py`:

```python
from .base import Article, ContentSource

class MySource:  # Implements ContentSource Protocol
    def can_handle(self, url: str) -> bool:
        return "mysource.com" in url

    async def fetch(self, url: str) -> Article:
        # Fetch and parse content
        return Article(
            title="...",
            author="...",
            content="<html>...</html>",
            published_at=None,
            source_url=url,
            source_type="mysource",
        )
```

2. Register in `sources/__init__.py`:

```python
from .mysource import MySource

_SOURCES: list[type[ContentSource]] = [
    TwitterPlaywrightSource,
    MySource,           # Add before WebArticleSource
    WebArticleSource,
]
```

## Troubleshooting

### Cookies not working

1. Make sure you copied both `auth_token` AND `ct0`
2. Format: `auth_token=VALUE; ct0=VALUE`
3. Both tokens should be 20+ characters
4. Cookies expire - you may need to re-extract them
5. Use "Test Cookies" button to verify format

### Twitter page not loading

The app uses Playwright with stealth mode to avoid bot detection. If Twitter blocks requests:
1. Try again after a few minutes
2. Re-extract fresh cookies
3. Check if your account has restrictions

### WeasyPrint errors

The Nix dev shell includes all dependencies. If running outside Nix:

```bash
# macOS
brew install pango cairo gdk-pixbuf

# Ubuntu/Debian
apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0

# Or use nix develop (recommended)
nix develop
```

### Content too large

PDF generation has a 50MB content size limit to prevent memory issues. If you hit this limit, the article content is unusually large.

## License

MIT
