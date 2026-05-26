# YouTube Download Requirements

## Requested Change

Add a separate YouTube downloads tab, parallel to the existing Twitter/X video downloader. Users must be able to paste many YouTube links, provide YouTube session cookies/tokens for authenticated access, choose video download or MP3-only download, and download long-running podcast videos reliably.

Authenticated access must be verified with Netscape-format YouTube cookies. A restricted test URL is still required to prove access to non-public videos.

## Implementation Decisions

- Initial URL support: individual `youtube.com/watch`, `youtu.be`, `youtube.com/shorts`, `youtube.com/live`, and `youtube.com/embed` URLs.
- Playlists and channels are out of scope for the first implementation.
- MP3 quality uses yt-dlp defaults; no explicit `--audio-quality` override.
- YouTube cookies are supplied as Netscape-format `cookies.txt` content.
- YouTube cookies persist in browser localStorage, separate from existing Twitter/X cookies.
- YouTube downloads require a current `yt-dlp` with EJS challenge support and an explicit supported JavaScript runtime. The Nix shell and Docker runtime must provide `yt-dlp 2026.03.17` or newer, Node 22 or newer, and the command must pass `--js-runtimes node`.
- Real YouTube E2E tests are opt-in via environment flags so the deterministic fake-downloader E2E suite remains stable by default.
- Public real test URL: `https://www.youtube.com/watch?v=fv7TlVMETP0`.
- Long podcast real test URL: `https://www.youtube.com/watch?v=tc82YJfvXZo`.

## Verified Existing Baseline

- Current Twitter/X video UI is `GET /videos` rendered from `src/twitter_articlenator/templates/videos.html`.
- The current video UI posts to `POST /api/videos/download` and consumes an SSE stream with `start`, `waiting`, `retry`, `progress`, `complete`, and `error` events.
- Twitter/X video downloads use `src/twitter_articlenator/sources/video_downloader.py`, shelling out to `yt-dlp`.
- Current Twitter/X video files are written under `<output_dir>/videos` and served through `GET /download/video/<filename>`.
- Existing E2E coverage lives in `tests/e2e/test_video_download.py` with `tests/e2e/pages/videos_page.py`.
- Current `yt-dlp` call has a 300-second subprocess timeout, which is not enough to verify long-running podcast downloads.

## External Tooling Requirements

- Keep `yt-dlp` as the downloader engine unless implementation proves it cannot satisfy a requirement.
- YouTube session support must use a temporary Netscape-format cookies file passed to `yt-dlp --cookies FILE`; this is the format documented by yt-dlp for manually supplied cookies.
- MP3-only mode must use `yt-dlp -x --audio-format mp3`, which requires `ffmpeg` and `ffprobe`; add those dependencies to both the Nix dev shell and Docker runtime.
- Authenticated YouTube extraction must pass `--js-runtimes node` so EJS challenge solving uses the Node runtime supplied by the project.
- Video and MP3 source selection should prefer progressive MP4 format `18` before other single-file MP4 formats to avoid HLS fragment failures during public downloads.
- Do not log raw cookies, raw session tokens, or full cookie file contents.
- Delete temporary cookie files after each download attempt.

## Frontend Design Requirements

- Use the Impeccable frontend workflow for the YouTube tab UI.
- Apply the existing project Design Context from `CLAUDE.md`: dark, literary, refined, Tokyo Night palette, library background preserved, quiet utility, WCAG AA basics.
- Use the `frontend-design` skill before UI implementation; do not create a generic duplicate of the Twitter/X page without a design pass.
- Keep the UI consistent with the existing app shell while making the YouTube-specific controls clear, accessible, keyboard navigable, and responsive.
- Verify the UI from the browser/user perspective with Playwright click testing after implementation.

## Functional Requirements

1. Add a new top-level YouTube tab.
   - Route: `GET /youtube`.
   - Navigation label: `YouTube`.
   - It must not replace or rename the existing Twitter/X `Videos` tab.

2. Add a YouTube batch input UI.
   - Users can paste one YouTube URL per line.
   - Empty input shows a visible error without making an API request.
   - The textarea content persists locally under a YouTube-specific localStorage key, separate from the Twitter/X video key.
   - The clear button clears the textarea, persisted value, progress, warnings, errors, and results.

3. Add YouTube session cookie/token input.
   - The UI accepts a pasted YouTube `cookies.txt` value in Netscape cookie format.
   - The value is stored separately from existing Twitter/X cookies.
   - UI status must show whether YouTube cookies are configured without exposing secret values.
   - API requests include the cookies only for YouTube downloads.
   - The server writes cookies to a temporary file, passes that file to `yt-dlp --cookies`, then deletes it.

4. Add a download mode control.
   - Default mode: video file.
   - Separate option: MP3-only audio.
   - The selected mode is included in the API request and reflected in progress/result text.
   - Video mode returns downloadable MP4 files.
   - MP3 mode returns downloadable MP3 files.

5. Add a YouTube download API.
   - Route: `POST /api/youtube/download`.
   - Request JSON includes `links`, `mode`, and optional `cookies`.
   - Response is an SSE stream using the same event style as the existing Twitter/X video endpoint.
   - Each URL is processed independently; failures for one URL do not stop the remaining URLs.
   - Final event includes succeeded count, failed count, total count, and per-link errors.

6. Add a YouTube downloader module.
   - Create a YouTube-specific downloader instead of overloading the Twitter/X downloader.
   - Output paths must be separate from Twitter/X downloads, for example `<output_dir>/youtube/videos` and `<output_dir>/youtube/audio`.
   - Use safe, deterministic filenames that avoid path traversal and excessive filename length.
   - Use `--no-playlist` unless playlist support is explicitly approved.
   - Use retry/fragment retry options appropriate for long YouTube downloads.

7. Support long-running podcast downloads.
   - Remove the current hard 300-second subprocess timeout for YouTube or replace it with a configurable YouTube-specific timeout long enough for the approved test podcast.
   - Keep the HTTP response alive during long downloads with SSE keepalive messages.
   - Preserve per-item progress status so the browser does not appear frozen.
   - Do not mark the feature verified until a real long-running YouTube podcast URL has been downloaded end to end.

8. Add YouTube download routes.
   - Serve YouTube MP4 and MP3 files through YouTube-specific download routes.
   - Reject path traversal and unsupported extensions.
   - Return correct content types: `video/mp4` for MP4 and `audio/mpeg` for MP3.

9. Preserve existing Twitter/X behavior.
   - Existing `/videos`, `/api/videos/download`, and `/download/video/<filename>` behavior must continue to work.
   - Existing Twitter/X E2E tests must still pass.

## Implementation Task List With Required E2E Verification

1. Create the YouTube page and navigation tab.
   - Implementation files: `routes/pages.py`, `templates/base.html`, new `templates/youtube.html`, new page object under `tests/e2e/pages`.
   - E2E verification: navigate from home to `/youtube`, assert title/header, assert the Twitter/X `Videos` tab still exists and still points to `/videos`, and verify the page follows the Impeccable frontend requirements from the browser perspective.

2. Build the YouTube batch form.
   - Implementation files: `templates/youtube.html`, page object.
   - E2E verification: paste three URLs, reload page, assert values persist; click clear, assert values and UI state are cleared; submit empty form and assert visible error without network download.

3. Add YouTube cookie/token UI and local persistence.
   - Implementation files: `templates/youtube.html`, possibly `templates/base.html` if a shared status indicator is added.
   - E2E verification: paste a sample Netscape cookie file, reload page, assert configured status is shown; assert raw cookie values are not rendered back into status text or result text.

4. Add mode selection for Video vs MP3.
   - Implementation files: `templates/youtube.html`, page object.
   - E2E verification: select MP3 mode, submit through the browser against a deterministic fake downloader, assert API payload includes `mode: "mp3"` and result links end in `.mp3`; repeat default mode and assert `.mp4`.

5. Add an injectable YouTube downloader command path for deterministic E2E.
   - Implementation files: config/downloader module/test fixture.
   - E2E verification: run Flask with a fake `yt-dlp` executable or configured downloader path that records arguments and creates small fixture files; assert the browser workflow completes and the recorded command contains the expected YouTube options.

6. Implement `POST /api/youtube/download` SSE endpoint.
   - Implementation files: `routes/api.py`, downloader module.
   - E2E verification: through the browser, submit multiple links to the fake downloader; assert start/progress/complete UI updates, per-link result count, final summary, and that one fake failure does not stop later links.

7. Implement YouTube video downloads.
   - Implementation files: new YouTube downloader module and output handling.
   - E2E verification: fake downloader creates `.mp4`; browser shows a download link; Playwright `page.request.get()` downloads it from Flask and verifies status 200 and non-empty body.
   - Real verification: with `TEST_YOUTUBE_PUBLIC_URL`, download an actual public YouTube video end to end.

8. Implement YouTube authenticated-cookie downloads.
   - Implementation files: YouTube downloader cookie handling.
   - E2E verification: fake downloader asserts a `--cookies` argument points to a real temporary file containing the supplied Netscape cookie content; browser result must not expose cookie content.
   - Real verification: with `TEST_YOUTUBE_COOKIES_FILE` or `TEST_YOUTUBE_COOKIES` and `TEST_YOUTUBE_AUTH_URL`, download an authenticated/restricted YouTube video end to end.

9. Implement MP3-only downloads.
   - Implementation files: YouTube downloader, download route, Nix/Docker dependency updates for `ffmpeg`.
   - E2E verification: fake downloader creates `.mp3`; browser result link ends in `.mp3`; `page.request.get()` returns status 200, `audio/mpeg`, and non-empty body.
   - Real verification: with `TEST_YOUTUBE_PUBLIC_URL`, download MP3 end to end and verify the file exists, has `.mp3`, and is non-trivially sized.

10. Implement long-running podcast support.
    - Implementation files: YouTube downloader timeout/retry/keepalive behavior and API stream.
    - E2E verification: fake downloader sleeps longer than the previous short timeout threshold used by tests, while the UI remains in processing state and the request completes.
    - Real verification: with `TEST_YOUTUBE_LONG_URL` and an approved slow-test flag, download a real long-running podcast end to end.

11. Implement YouTube download route security.
    - Implementation files: `routes/pages.py` or a new route module.
    - E2E verification: Playwright request checks valid MP4 and MP3 downloads; path traversal and unsupported extensions return 400 or 404.

12. Run regression E2E for existing Twitter/X videos.
    - Implementation files: no intended behavior changes.
    - E2E verification: existing `tests/e2e/test_video_download.py` still passes, or any failures are fixed without reducing coverage.

## Required Verification Commands

Run these before marking implementation complete:

```bash
pytest tests/e2e/test_youtube_download.py
pytest tests/e2e/test_video_download.py
pytest tests/integration
pytest tests/unit
ruff check src/
```

Run real YouTube verification only after the required environment values are provided:

```bash
RUN_REAL_YOUTUBE_E2E=1 TEST_YOUTUBE_PUBLIC_URL="..." pytest tests/e2e/test_youtube_download.py -k public_real
RUN_REAL_YOUTUBE_E2E=1 TEST_YOUTUBE_COOKIES_FILE="..." TEST_YOUTUBE_AUTH_URL="..." pytest tests/e2e/test_youtube_download.py -k auth_real
RUN_REAL_YOUTUBE_E2E=1 RUN_SLOW_YOUTUBE_E2E=1 TEST_YOUTUBE_LONG_URL="..." pytest tests/e2e/test_youtube_download.py -k long_real
```

## Verification Status

- Deterministic YouTube E2E with fake downloader: verified.
- Real public YouTube MP4/MP3 E2E: verified with the configured public test URL.
- Real long-podcast E2E: verified with the configured long podcast URL.
- Real authenticated YouTube E2E: cookie handoff verified with valid cookies and the public test URL; a restricted URL is still required to prove restricted-video access.
- Existing Twitter/X video E2E regression: verified.

## Open Questions And Required Test Inputs

1. Provide an authenticated/restricted YouTube URL for restricted-video verification.
2. Provide valid YouTube Netscape-format cookies for the test account when re-running authenticated verification.

## YouTube Cookie Export Instructions

1. Open a new private/incognito browser window.
2. Log into YouTube.
3. In the same tab, open `https://www.youtube.com/robots.txt`.
4. Export `youtube.com` cookies in Netscape `cookies.txt` format from that private/incognito window.
5. Close the private/incognito window and do not reuse that session.
6. Save the file outside the repository, for example `/private/tmp/youtube-cookies.txt`.
7. Run auth verification with:

```bash
RUN_REAL_YOUTUBE_E2E=1 TEST_YOUTUBE_AUTH_URL="..." TEST_YOUTUBE_COOKIES_FILE="/private/tmp/youtube-cookies.txt" pytest tests/e2e/test_youtube_download.py -k auth
```

The cookie file should include tab-separated Netscape cookie rows for domains such as `.youtube.com`, `.google.com`, or `.youtube-nocookie.com`. Do not commit cookie files.
