"""Twitter/X video downloader using Playwright browser automation."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import structlog
from playwright.async_api._generated import SetCookieParam

from .browser_pool import get_browser_pool

log = structlog.get_logger()

TWITTER_VIDEO_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)"
)


class VideoDownloader:
    """Download videos from Twitter/X tweets using Playwright."""

    def __init__(self, cookies: str) -> None:
        """Initialize with Twitter cookies.

        Args:
            cookies: Twitter authentication cookies string (auth_token=...; ct0=...).
        """
        self._cookies_str = cookies

    def _parse_cookies(self) -> list[SetCookieParam]:
        """Parse cookie string into Playwright cookie format."""
        if not self._cookies_str:
            return []

        cookies: list[SetCookieParam] = []
        for part in self._cookies_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".x.com",
                        path="/",
                    )
                )
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".twitter.com",
                        path="/",
                    )
                )
        return cookies

    async def download(self, url: str, output_dir: Path) -> Path:
        """Download video from a tweet URL.

        Intercepts Twitter's GraphQL API response to extract mp4 variant URLs,
        then downloads the highest quality variant directly.

        Args:
            url: Twitter/X status URL containing a video.
            output_dir: Directory to save the video file.

        Returns:
            Path to the downloaded video file.

        Raises:
            ValueError: If URL is invalid or no video found.
        """
        match = TWITTER_VIDEO_URL_PATTERN.match(url)
        if not match:
            raise ValueError(f"Invalid Twitter URL: {url}")

        username = match.group(1)
        tweet_id = match.group(2)

        log.info("downloading_video", tweet_id=tweet_id, url=url)

        pool = get_browser_pool()
        cookies = self._parse_cookies()
        video_variants: list[dict] = []  # [{url, bitrate, content_type}, ...]

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # Intercept GraphQL API responses to extract video variant URLs.
            # Twitter's TweetDetail response contains video_info.variants with
            # direct mp4 URLs at various bitrates. These are complete files,
            # unlike the HLS segments served during playback.
            async def handle_api_route(route):
                response = await route.fetch()
                try:
                    body = await response.body()
                    data = json.loads(body)
                    self._extract_video_variants(data, video_variants)
                except Exception as e:
                    log.debug("api_response_parse_failed", error=str(e))
                await route.fulfill(response=response)

            await page.route("**/TweetDetail*", handle_api_route)
            await page.route("**/TweetResultByRestId*", handle_api_route)

            # Go to home first to establish session
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Navigate to the tweet
            for attempt in range(1, 4):
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                        referer="https://x.com/home",
                    )
                    await asyncio.sleep(3)

                    # Wait for tweet or video player to appear
                    await page.wait_for_selector(
                        '[data-testid="tweetText"], [data-testid="videoPlayer"], '
                        '[data-testid="videoComponent"]',
                        timeout=15000,
                    )
                    break
                except Exception as e:
                    log.warning(
                        "video_page_load_retry",
                        attempt=attempt,
                        error=str(e),
                    )
                    if attempt == 3:
                        await page.screenshot(path="/tmp/video_debug.png", full_page=True)
                        raise

            # Wait a moment for API response to be captured
            await asyncio.sleep(3)

        # Filter for mp4 variants only
        mp4_variants = [v for v in video_variants if v.get("content_type") == "video/mp4"]

        if not mp4_variants:
            log.error(
                "no_video_variants_found",
                all_variants=len(video_variants),
                url=url,
            )
            raise ValueError(f"No video found in tweet: {url}")

        # Pick the highest bitrate variant
        best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
        best_url = best["url"]
        log.info(
            "best_video_variant",
            url=best_url,
            bitrate=best.get("bitrate"),
            variants_found=len(mp4_variants),
        )

        # Download the video
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{username}_{tweet_id}.mp4"
        await self._download_file(best_url, output_path)

        file_size = output_path.stat().st_size
        log.info("video_downloaded", path=str(output_path), size_bytes=file_size)
        return output_path

    def _extract_video_variants(self, data, variants: list[dict]) -> None:
        """Recursively extract video variant URLs from API response JSON.

        Twitter's GraphQL response contains video_info objects with a variants
        array. Each variant has url, bitrate (for mp4), and content_type.

        Args:
            data: Parsed JSON data (dict or list).
            variants: List to append found variants to.
        """
        if isinstance(data, dict):
            if "video_info" in data:
                for v in data["video_info"].get("variants", []):
                    if "url" in v and "content_type" in v:
                        variants.append(
                            {
                                "url": v["url"],
                                "bitrate": v.get("bitrate", 0),
                                "content_type": v["content_type"],
                            }
                        )
                        log.debug(
                            "video_variant_found",
                            content_type=v["content_type"],
                            bitrate=v.get("bitrate"),
                        )
            for value in data.values():
                self._extract_video_variants(value, variants)
        elif isinstance(data, list):
            for item in data:
                self._extract_video_variants(item, variants)

    async def _download_file(self, url: str, output_path: Path) -> None:
        """Download file from URL using streaming.

        Args:
            url: URL to download.
            output_path: Path to save the file.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
