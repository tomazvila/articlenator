# Twitter Articlenator

Convert Twitter/X content (tweets, threads, and articles) and web articles to e-reader friendly PDFs.

## Features

- **Twitter/X Support**: Convert tweets, threads, and long-form articles to PDF using Playwright with stealth mode
- **Web Articles**: Supports any HTTP(S) web article with smart content extraction
- **E-Reader Optimized**: Clean, readable PDFs designed for Kindle, Kobo, and other e-readers
- **Private Accounts**: Administrator-created logins with isolated credentials, jobs, bookmarks, and output
- **Zettelkasten Pipeline**: Resumable source ingestion, agentic synthesis/review, provenance,
  validation, and safe maintenance tooling under `zettel_ralph/`

## Quick Start

### Prerequisites

- [Nix](https://nixos.org/download.html) with flakes enabled
- Twitter/X account (for Twitter content)

### Run the Application
```bash
# Enter development shell (auto-installs dependencies)
nix develop

# Configure stable secrets
export TWITTER_ARTICLENATOR_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Create the first administrator, then start the server
python -m twitter_articlenator.app users create --username admin --admin
python -m twitter_articlenator.app
```

Open http://localhost:5001 in your browser.

See [MULTI_USER.md](MULTI_USER.md) for account administration, storage isolation, limits, migration behavior, and production deployment requirements.

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

## Zettelkasten Pipeline
The repository also contains the operator-oriented [Zettel-Ralph pipeline](zettel_ralph/README.md)
used to turn large Twitter and transcript corpora into atomic, linked Obsidian notes. It
currently runs separately from the Flask UI but shares Articlenator's collection workflow.
Its source-adapter contract supports the current direct bookmark and transcript paths and
defines the handoff for a future Articlenator PDF ingestion adapter.

The maintenance CLI preserves provenance/link diagnostics and controlled repair operations
from the first large run without hard-coded local paths:
```bash
python zettel_ralph/maintenance.py --help
```

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
| `TWITTER_ARTICLENATOR_SECRET_KEY` | required | Flask session-signing secret, at least 32 characters |
| `TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY` | required | Fernet key for all stored user credentials |
| `TWITTER_ARTICLENATOR_SESSION_COOKIE_SECURE` | `false` | Require HTTPS for login cookies |
| `TWITTER_ARTICLENATOR_TRUSTED_HOSTS` | unset | Comma-separated accepted hostnames |

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

Articlenator runs as a single Docker Compose service on the homelab host,
behind Caddy, at `articlenator.homelab.tomazvi.la`. State lives in
`/srv/articlenator`.

```bash
# Build the image (Linux)
nix build .#docker
docker load < result

# Recreate the service from the ~/homelab compose stack
cd ~/homelab && docker compose up -d articlenator
```

The full deployment, verification, rollback, and account-management guide is
in [DEPLOYMENT.md](DEPLOYMENT.md). Docker configuration details are in
[DOCKER.md](DOCKER.md).

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
├── zettel_ralph/          # Agentic source-to-zettelkasten pipeline and maintenance CLI
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
