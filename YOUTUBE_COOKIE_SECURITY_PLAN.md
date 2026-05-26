# YouTube Cookie Security Plan

## Problem

The previous YouTube page stored Netscape `cookies.txt` content in browser `localStorage` under `articlenator_youtube_cookies`. That is not acceptable for long-lived YouTube session cookies. Any successful XSS on the app origin can read `localStorage`, and localStorage also persists across browser restarts.

The safer direction is to keep raw YouTube cookies out of browser-readable storage entirely. The browser should only see masked status metadata and short-lived CSRF/session state. The server should own cookie persistence, validation, rotation, use, and deletion.

## Research Summary

- OWASP HTML5 Security guidance says sensitive data should not be stored in localStorage because one XSS can steal all data stored there.
- MDN documents that localStorage is origin-scoped and persists across browser sessions.
- OWASP Session Management and Flask security guidance recommend hardened cookies with `Secure`, `HttpOnly`, and `SameSite`.
- OWASP File Upload guidance requires allowlisted file types, size limits, server-side validation, generated filenames, and storage outside the webroot.
- Kubernetes documentation treats Secrets as confidential data, recommends encryption at rest and least-privilege RBAC, and warns that Secrets are stored unencrypted in etcd by default unless encryption is configured.

Sources:

- https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
- https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage
- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://flask.palletsprojects.com/en/stable/web-security/
- https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- https://kubernetes.io/docs/concepts/configuration/secret/
- https://kubernetes.io/docs/concepts/security/secrets-good-practices/

## Implemented Architecture

1. Remove YouTube cookie persistence from client JavaScript.
   - Do not write YouTube cookie text to `localStorage`, `sessionStorage`, IndexedDB, or non-HttpOnly browser cookies.
   - The upload form may hold cookie text only in the active textarea until submitted or cleared.
   - After a successful save, clear the textarea immediately.

2. Add server-side YouTube cookie storage.
   - Store the uploaded Netscape cookie file under the configured data directory, for example `/data/config/youtube-cookies.txt`.
   - Use restrictive permissions: directory `0700`, file `0600`.
   - Store outside static/download-served directories.
   - Never expose raw cookie contents through an API response.

3. Add optional encryption at rest for the cookie file.
   - Use `TWITTER_ARTICLENATOR_SECRET_KEY` for Flask sessions and `TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY` for encrypted YouTube cookie storage.
   - If configured, encrypt the cookie file before writing it to the PVC and decrypt only into a temporary file for `yt-dlp`.
   - If no encryption key is configured, either refuse persistent server-side cookie storage in production or show a clear security error.
   - Keep encryption keys in Kubernetes Secret or external secret manager, not in Git.

4. Add YouTube cookie management endpoints.
   - `GET /api/youtube/cookies/status`
     - Returns only metadata: configured boolean, cookie row count, YouTube-domain row count, expired count, soon-expiring count, last uploaded time, last verified time, last verification result.
   - `POST /api/youtube/cookies`
     - Accepts either multipart file upload or pasted text.
     - Validates Netscape format, size, domains, required-looking YouTube session rows, and no expired-only state.
     - Optionally runs live verification with `yt-dlp --cookies <tempfile> -F <public-test-url>` and confirms real media formats exist.
     - Saves only after validation succeeds.
   - `DELETE /api/youtube/cookies`
     - Deletes the stored cookie file and clears status metadata.
   - `POST /api/youtube/cookies/verify`
     - Re-runs live verification against the configured test URL without returning secrets.

5. Change YouTube download flow.
   - `/api/youtube/download` should use the stored server cookie file by default.
   - Raw cookie text is no longer accepted by `/api/youtube/download`.
   - Pass cookies to `yt-dlp` via a temporary file that is deleted after each attempt.
   - Do not log cookie values, temporary cookie paths if avoidable, or full `yt-dlp` commands containing cookie paths.

6. Add app session and CSRF protection before accepting cookie uploads.
   - Configure Flask session cookies with `Secure`, `HttpOnly`, and `SameSite=Lax` or `Strict` in deployed environments.
   - Add CSRF protection for state-changing endpoints: cookie upload, verification, delete, and download if it uses stored credentials.
   - Keep the app behind HTTPS. If accessed through ingress, ensure the app can identify secure requests correctly.

7. Harden browser security.
   - Add a Content Security Policy.
   - Remove inline JavaScript from templates or use nonces/hashes so CSP can avoid broad `unsafe-inline`.
   - Avoid DOM sinks such as `innerHTML` for user-controlled strings in YouTube pages.
   - Keep displayed cookie status masked and metadata-only.

8. Kubernetes deployment setup.
   - Mount `/data` as already planned.
   - Add a Kubernetes Secret for the app encryption key.
   - Restrict RBAC access to that Secret.
   - Enable Kubernetes Secret encryption at rest in the cluster or use an external secret store if available.
   - Do not put YouTube cookies directly in ConfigMaps, manifests, image layers, logs, or GitHub Actions output.

## Implementation Task List

1. [x] Add secure cookie storage module.
   - Create a small service that validates Netscape cookie text, computes safe metadata, writes/deletes the server cookie file, and produces a temporary cookie file for `yt-dlp`.
   - Unit tests: valid Netscape file, malformed rows, expired rows, non-YouTube domains, permission checks, temp-file cleanup, no raw value in metadata.

2. [x] Add encryption-at-rest support.
   - Add config for an encryption key and implement authenticated encryption for the stored cookie file.
   - Unit tests: round-trip encryption, wrong key fails, plaintext cookie value does not appear in stored bytes, missing key fails in production mode.

3. [x] Add cookie management API.
   - Implement status, upload, delete, and verify endpoints.
   - Integration tests: upload valid file, reject oversized/malformed file, status is metadata-only, delete removes file, verify handles success/failure without leaking raw values.

4. [x] Update YouTube frontend.
   - Replace persistent cookie textarea with an upload/replace control and status panel.
   - Remove `articlenator_youtube_cookies` writes and reads from localStorage.
   - Clear upload input after save.
   - E2E tests: browser localStorage never contains cookie text, uploaded cookie status is shown, replace works, delete works, download uses stored server cookies.

5. [x] Update download endpoint.
   - Make downloads use the stored server-side cookie file when present.
   - Remove raw-cookie request body support after migration tests are in place.
   - E2E tests: authenticated download succeeds after upload, request payload does not contain cookie text, results do not leak cookie values.

6. [x] Add CSRF/session hardening.
   - Add hardened Flask cookie settings and CSRF checks for state-changing routes.
   - Integration/E2E tests: upload without CSRF fails, upload with valid CSRF succeeds, session cookie has expected flags in production-style config.

7. [x] Add CSP and DOM hardening.
   - Move inline JavaScript out of templates or add nonce-based CSP.
   - Replace unsafe DOM writes where needed.
   - E2E tests: CSP header exists, no inline script violation in browser console, YouTube workflow still works.

8. [x] Update Kubernetes manifests and deployment docs.
   - Add encryption key Secret wiring.
   - Document rotation: upload new cookies through UI/API, verify, then old encrypted blob is overwritten.
   - Deployment verification: rollout succeeds, `/api/youtube/cookies/status` works, real YouTube auth download works after upload.

## Migration Plan

1. The YouTube page clears the legacy `articlenator_youtube_cookies` key without reading or reusing the value.
2. Users upload a fresh `cookies.txt` through the YouTube page.
3. Downloads use the server-side cookie file when one is configured.

## Decisions

1. Server-side YouTube cookies are single-profile/global for this app instance.
2. The app remains single-user/private-deployment oriented; this pass adds CSRF/session hardening but not user accounts.
3. Production encryption keys are provided through Kubernetes Secret-backed environment variables.
4. The UI keeps both file upload and pasted-text upload, but neither path persists raw cookie text in browser storage.
