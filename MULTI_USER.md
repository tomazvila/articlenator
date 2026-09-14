# Multi-user operation
Articlenator requires a local account for every user. There is no public registration or invite flow. An administrator creates accounts, and every account gets isolated credentials, browser state, jobs, and output.

## First boot
Generate and retain two independent secrets:
```bash
export TWITTER_ARTICLENATOR_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```
Both values must remain stable across restarts. Losing the session secret signs everyone out. Losing the encryption key makes stored Twitter cookies, YouTube cookies, and YouTube OAuth tokens unreadable.

Create the first administrator before sharing the URL:
```bash
twitter-articlenator users create --username admin --admin
```
The command prompts twice for a password of at least 12 characters. Start the server without arguments afterward:
```bash
twitter-articlenator
```
An administrator can create additional accounts at `/admin/users` or with the same CLI command without `--admin`. There is no public sign-up endpoint.

Password recovery and access revocation are CLI-only administrative operations:
```bash
twitter-articlenator users set-password --username alice
twitter-articlenator users disable --username alice
twitter-articlenator users enable --username alice
```
Password resets, disabling, and re-enabling increment the account's authentication version, invalidating all existing login sessions on their next request without deleting user data.

## Production configuration
Authenticated production startup fails unless `TWITTER_ARTICLENATOR_SECRET_KEY` contains at least 32 characters and `TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY` is a valid Fernet key. Credential encryption is forced on in authenticated production mode.

Set these values when TLS terminates at a reverse proxy:
- `TWITTER_ARTICLENATOR_SESSION_COOKIE_SECURE=true`
- `TWITTER_ARTICLENATOR_TRUST_PROXY_HEADERS=true` only when exactly one trusted proxy sits in front of the app
- `TWITTER_ARTICLENATOR_TRUSTED_HOSTS=articlenator.homelab.tomazvi.la`

The proxy setting trusts one `X-Forwarded-For` and `X-Forwarded-Proto` hop. Do not enable it when clients can connect directly to the application port and supply those headers.

## Data ownership
The immutable UUID in the user database is the storage owner. Usernames may differ only by case and are not used as paths.

Persistent data is organized as follows:
```text
<config-root>/
  articlenator.sqlite3
  users/<user-uuid>/
    twitter-cookies.enc
    youtube-cookies.enc
    youtube-cookies.json
    youtube-oauth-token.enc
<output-root>/users/<user-uuid>/
  *.pdf
  sessions/
  videos/
  youtube/
  transcriptions/
  channels/
```
Credential values never return through status APIs. Account roots and credential files are created with owner-only permissions where the filesystem permits it. Downloads, persisted jobs, resumable sessions, and in-memory YouTube job lookups are all resolved against the logged-in user's UUID.

Browser-side bookmarks, pending links, resumable job IDs, and UI state use keys suffixed with the same UUID. Twitter and YouTube credentials are not stored in `localStorage`.

## Existing data
There is intentionally no migration of legacy global data. Existing jobs, outputs, cookies, and OAuth tokens outside `users/<user-uuid>/` are not inherited by the first administrator or exposed through authenticated routes. Archive or remove that legacy data separately after confirming it is no longer needed.

## Resource limits
Heavy work is rejected with HTTP 429 when a cap is full. Defaults are:

| Resource | Per user | Global |
|---|---:|---:|
| Playwright conversion/bookmark work | 1 | 2 |
| Transcription/channel work | 1 | 1 |
| Twitter/YouTube downloads | 1 | 2 |

Configure the caps with:
- `TWITTER_ARTICLENATOR_PLAYWRIGHT_PER_USER_LIMIT`
- `TWITTER_ARTICLENATOR_PLAYWRIGHT_GLOBAL_LIMIT`
- `TWITTER_ARTICLENATOR_TRANSCRIPTION_PER_USER_LIMIT`
- `TWITTER_ARTICLENATOR_TRANSCRIPTION_GLOBAL_LIMIT`
- `TWITTER_ARTICLENATOR_DOWNLOAD_PER_USER_LIMIT`
- `TWITTER_ARTICLENATOR_DOWNLOAD_GLOBAL_LIMIT`

The limiter is in-process. Run one application replica when these global caps must be authoritative. Persisted channel jobs resumed at startup also acquire transcription capacity; jobs beyond the cap remain resumable but are deferred.

## Deployment bootstrap
Secrets live in `~/homelab/.env` (never in Git):
```
ARTICLENATOR_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
ARTICLENATOR_COOKIE_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
DOMAIN=homelab.tomazvi.la
```
The compose stack (`~/homelab/services/articlenator/compose.yaml`) injects them
and uses persistent `/srv/articlenator` as `/data`, secure cookies, one trusted
proxy hop, and the host `articlenator.homelab.tomazvi.la`.
Create the first administrator in the running container:
```bash
docker exec -it articlenator \
  twitter-articlenator users create --username admin --admin
```
