"""Twitter/X content source using Playwright browser automation."""

import asyncio
import html
import re
from datetime import datetime

import structlog
from playwright.async_api._generated import SetCookieParam

from .base import Article, ContentSource
from .browser_pool import get_browser_pool

log = structlog.get_logger()


def _escape_html(text: str) -> str:
    """Escape HTML special characters in text content."""
    return html.escape(text, quote=False)


class TwitterPlaywrightSource(ContentSource):
    """Fetch tweets using Playwright browser automation.

    This is more reliable than API-based scrapers because it uses
    the same path as a real browser user.
    """

    # URL patterns for Twitter/X
    TWITTER_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/(\w+)/(?:status|article)/(\d+)"
    )

    # Maximum title length
    MAX_TITLE_LENGTH = 100

    # Maximum retry attempts for page load failures
    MAX_LOAD_RETRIES = 5

    def __init__(self, cookies: str | None = None) -> None:
        """Initialize Twitter source with cookies.

        Args:
            cookies: Twitter authentication cookies string (auth_token=...; ct0=...).
        """
        self._cookies_str = cookies

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Twitter/X status URL."""
        if not url:
            return False
        return bool(self.TWITTER_URL_PATTERN.match(url))

    def _parse_cookies(self) -> list[SetCookieParam]:
        """Parse cookie string into Playwright cookie format.

        Returns:
            List of cookie dicts for Playwright.
        """
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
                # Also add for twitter.com domain
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".twitter.com",
                        path="/",
                    )
                )
        return cookies

    async def fetch(self, url: str) -> Article:
        """Fetch a tweet using Playwright and convert to Article.

        Uses a browser pool for efficient resource management.

        Args:
            url: Twitter/X status URL.

        Returns:
            Article containing the tweet content.

        Raises:
            ValueError: If cookies not configured or URL invalid.
        """
        if not self._cookies_str:
            raise ValueError("Cookies are required to fetch tweets")

        # Normalize to https (pushState fails with http on https origin)
        if url.startswith("http://"):
            url = "https://" + url[7:]

        match = self.TWITTER_URL_PATTERN.match(url)
        if not match:
            raise ValueError(f"Invalid Twitter URL: {url}")

        username = match.group(1)
        tweet_id = match.group(2)

        log.info("fetching_tweet_playwright", tweet_id=tweet_id, url=url)

        # Use browser pool for efficient browser reuse
        pool = get_browser_pool()
        cookies = self._parse_cookies()

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # First go to home to establish session and let React app initialize
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Wait for home feed to load (proves we're logged in)
            is_authenticated = False
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
                log.info("home_feed_loaded")
                is_authenticated = True
            except Exception:
                log.warning(
                    "home_feed_not_loaded",
                    hint="Session not ready - may be rate limited or slow to load",
                )

            # Retry loop for navigation and content loading
            last_error = None
            for attempt in range(1, self.MAX_LOAD_RETRIES + 1):
                try:
                    # Now use React Router navigation by manipulating history
                    # This simulates clicking a link rather than direct navigation
                    await page.evaluate(f"""
                        window.history.pushState({{}}, '', '{url}');
                        window.dispatchEvent(new PopStateEvent('popstate'));
                    """)
                    await asyncio.sleep(3)

                    # If that didn't work, try clicking the URL in address bar style
                    # by using goto but with a referrer
                    if page.url != url:
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=60000,
                            referer="https://x.com/home",
                        )
                        await asyncio.sleep(3)

                    # Take screenshot for debugging
                    await page.screenshot(path="/tmp/twitter_nav_test.png")
                    log.info(
                        "navigation_complete",
                        url=page.url,
                        title=await page.title(),
                        attempt=attempt,
                    )

                    # Wait for tweet content to load (regular tweet OR article)
                    await page.wait_for_selector(
                        '[data-testid="tweetText"], [data-testid="longformRichTextComponent"]',
                        timeout=30000,
                    )
                    # Success - break out of retry loop
                    break

                except Exception as e:
                    last_error = e
                    log.warning(
                        "tweet_load_failed",
                        attempt=attempt,
                        max_attempts=self.MAX_LOAD_RETRIES,
                        error=str(e),
                        url=url,
                    )

                    if attempt < self.MAX_LOAD_RETRIES:
                        # Reload the page and try again
                        log.info("retrying_tweet_load", attempt=attempt + 1, url=url)
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                    else:
                        # Final attempt failed - save debug info and raise
                        screenshot_path = "/tmp/twitter_debug.png"
                        await page.screenshot(path=screenshot_path, full_page=True)
                        html_path = "/tmp/twitter_debug.html"
                        with open(html_path, "w") as f:
                            f.write(await page.content())
                        log.error(
                            "tweet_not_found_after_retries",
                            screenshot=screenshot_path,
                            html_path=html_path,
                            page_url=page.url,
                            page_title=await page.title(),
                            attempts=self.MAX_LOAD_RETRIES,
                        )
                        raise last_error

            # Extract tweet data
            tweet_data = await self._extract_tweet_data(page, username, is_authenticated)

            log.info(
                "tweet_extracted_playwright",
                author=tweet_data["author"],
                has_content=bool(tweet_data["content"]),
            )

            return self._create_article(tweet_data, url)

    async def _extract_tweet_data(
        self, page, expected_username: str, is_authenticated: bool = False
    ) -> dict:
        """Extract tweet data from the page including images and replies.

        Args:
            page: Playwright page object.
            expected_username: Expected author username.
            is_authenticated: Whether the session is authenticated (needed for replies).

        Returns:
            Dict with tweet data.
        """
        # Check if this is an article (long-form content)
        article_element = await page.query_selector('[data-testid="longformRichTextComponent"]')
        is_article = article_element is not None

        content = ""
        title = None
        images = []

        if is_article:
            # Scroll through the article to trigger lazy-loading of images
            log.info("scrolling_article_for_images")
            await self._scroll_article(page, article_element)

            # Extract article content with images preserved
            log.info("extracting_article_content")
            content, images = await self._extract_article_content(article_element, page)

            # Try to get article title from the article header or cover
            try:
                title_element = await page.query_selector(
                    'article h1, [data-testid="article-cover-image"] + div h1'
                )
                if title_element:
                    title = await title_element.inner_text()
            except Exception:
                pass

            if not title:
                page_title = await page.title()
                if " / X" in page_title:
                    title = page_title.replace(" / X", "").strip()
        else:
            # Regular tweet - get the main tweet first
            # The main/focal tweet has a larger format, find it specifically
            main_tweet = await page.query_selector('article[data-testid="tweet"][tabindex="-1"]')
            if not main_tweet:
                # Fallback to first tweet
                main_tweet = await page.query_selector('[data-testid="tweet"]')

            if main_tweet:
                # Extract text from main tweet
                text_el = await main_tweet.query_selector('[data-testid="tweetText"]')
                if text_el:
                    content = await text_el.inner_text()

                # Extract images from main tweet
                images = await self._extract_images(main_tweet)

        # Get author display name
        display_name = expected_username
        try:
            name_element = await page.query_selector('[data-testid="User-Name"] span')
            if name_element:
                display_name = await name_element.inner_text()
        except Exception:
            pass

        # Get timestamp
        timestamp = None
        try:
            time_element = await page.query_selector("time")
            if time_element:
                datetime_attr = await time_element.get_attribute("datetime")
                if datetime_attr:
                    timestamp = datetime.fromisoformat(datetime_attr.replace("Z", "+00:00"))
        except Exception:
            pass

        # Extract replies/conversation thread (only if authenticated)
        replies = []
        if not is_article and is_authenticated:
            replies = await self._extract_replies(page, expected_username)
        elif not is_article and not is_authenticated:
            log.info("skipping_replies", reason="not authenticated")

        return {
            "author": expected_username,
            "display_name": display_name,
            "content": content,
            "images": images,
            "replies": replies,
            "timestamp": timestamp,
            "title": title,
            "is_article": is_article,
        }

    async def _extract_images(self, container) -> list[str]:
        """Extract image URLs from a tweet container.

        Args:
            container: Playwright element containing the tweet.

        Returns:
            List of image URLs.
        """
        images = []
        try:
            # Find all tweet photos
            photo_elements = await container.query_selector_all('[data-testid="tweetPhoto"] img')
            for img in photo_elements:
                src = await img.get_attribute("src")
                if src and "twimg.com" in src:
                    # Get higher quality version
                    # Twitter uses format=jpg&name=small, change to name=large
                    if "name=" in src:
                        src = re.sub(r"name=\w+", "name=large", src)
                    images.append(src)
        except Exception as e:
            log.warning("image_extraction_failed", error=str(e))
        return images

    async def _scroll_article(self, page, article_element) -> None:
        """Scroll through article to trigger lazy-loading of images."""
        try:
            scroll_height = await page.evaluate("(el) => el.scrollHeight", article_element)
            viewport_height = await page.evaluate("window.innerHeight")
            steps = max(1, scroll_height // viewport_height)
            for i in range(steps + 1):
                await page.evaluate(f"window.scrollTo(0, {i * viewport_height})")
                await asyncio.sleep(0.3)
            # Scroll back to top
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("article_scroll_failed", error=str(e))

    async def _extract_article_content(self, article_element, page) -> tuple[str, list[str]]:
        """Extract structured content from a long-form article.

        Preserves images, code blocks, headings, lists, and blockquotes
        in their natural document order.

        Args:
            article_element: Playwright element for the longformRichTextComponent.
            page: Playwright page object.

        Returns:
            Tuple of (html_content, image_urls).
        """
        blocks = await page.evaluate(
            """(element) => {
            const blocks = [];
            const seenTexts = new Set();
            const seenImages = new Set();

            // Navigate into wrapper div if present
            let wrapper = element;
            if (element.children.length === 1
                && element.children[0].tagName === 'DIV') {
                wrapper = element.children[0];
            }

            function addText(content) {
                const trimmed = content.trim();
                if (trimmed && !seenTexts.has(trimmed)) {
                    seenTexts.add(trimmed);
                    blocks.push({type: 'text', content: trimmed});
                }
            }

            function addImage(src) {
                if (src && !seenImages.has(src)) {
                    seenImages.add(src);
                    blocks.push({type: 'image', src: src});
                }
            }

            function processElement(el) {
                if (!el || !el.tagName) return;
                const tag = el.tagName.toLowerCase();

                // Code block: <section> with markdown-code-block inside,
                // or any element containing <pre>/<code> with monospace text
                if (tag === 'section' || tag === 'div') {
                    const codeBlock = el.querySelector(
                        '[data-testid="markdown-code-block"], pre, code'
                    );
                    if (codeBlock) {
                        let lang = '';
                        const langSpan = el.querySelector(
                            '[data-testid="markdown-code-block"] div > span'
                        );
                        if (langSpan) lang = langSpan.textContent.trim();

                        let code = '';
                        const codeEl = el.querySelector('code');
                        if (codeEl) {
                            code = codeEl.innerText;
                        } else {
                            const pre = el.querySelector('pre');
                            code = pre ? pre.innerText : codeBlock.innerText;
                        }
                        if (code.trim()) {
                            blocks.push({
                                type: 'code', language: lang, content: code
                            });
                            return;
                        }
                    }
                }

                // Direct image
                if (tag === 'img') {
                    const src = el.src || '';
                    if (src && src.startsWith('http')) {
                        addImage(src);
                    }
                    return;
                }

                // Check for heading inside this element
                const heading = el.querySelector('h1,h2,h3,h4,h5,h6');
                if (heading) {
                    const level = parseInt(heading.tagName[1]);
                    const text = heading.innerText.trim();
                    if (text) {
                        blocks.push({
                            type: 'heading', level: level, content: text
                        });
                    }
                    return;
                }

                // Ordered list
                if (tag === 'ol') {
                    const items = Array.from(el.querySelectorAll('li'))
                        .map(li => li.innerText.trim())
                        .filter(t => t);
                    if (items.length) {
                        blocks.push({type: 'ordered_list', items: items});
                    }
                    return;
                }

                // Unordered list
                if (tag === 'ul') {
                    const items = Array.from(el.querySelectorAll('li'))
                        .map(li => li.innerText.trim())
                        .filter(t => t);
                    if (items.length) {
                        blocks.push({type: 'unordered_list', items: items});
                    }
                    return;
                }

                // Blockquote
                if (tag === 'blockquote') {
                    const text = el.innerText?.trim();
                    if (text) {
                        blocks.push({type: 'blockquote', content: text});
                    }
                    return;
                }

                // Check if this element contains images
                const imgs = el.querySelectorAll('img');
                const hasImages = Array.from(imgs).some(
                    img => (img.src || '').startsWith('http')
                );

                if (hasImages) {
                    for (const child of el.children) {
                        processElement(child);
                    }
                    return;
                }

                // Regular text block
                const text = el.innerText?.trim();
                if (text) {
                    addText(text);
                }
            }

            for (const child of wrapper.children) {
                processElement(child);
            }

            return blocks;
        }""",
            article_element,
        )

        # Convert blocks to clean HTML
        html_parts = []
        images = []

        for block in blocks:
            btype = block["type"]
            if btype == "text":
                paragraphs = block["content"].split("\n")
                for p in paragraphs:
                    p = p.strip()
                    if p:
                        html_parts.append(f"        <p>{_escape_html(p)}</p>")
            elif btype == "heading":
                level = block["level"]
                html_parts.append(f"        <h{level}>{_escape_html(block['content'])}</h{level}>")
            elif btype == "code":
                lang = _escape_html(block.get("language", ""))
                code = _escape_html(block["content"])
                lang_attr = f' class="language-{lang}"' if lang else ""
                lang_label = f'<div class="code-lang">{lang}</div>' if lang else ""
                html_parts.append(
                    f'        <div class="code-block">{lang_label}'
                    f"<pre><code{lang_attr}>{code}</code></pre></div>"
                )
            elif btype == "ordered_list":
                items_html = "\n".join(
                    f"            <li>{_escape_html(item)}</li>" for item in block["items"]
                )
                html_parts.append(f"        <ol>\n{items_html}\n        </ol>")
            elif btype == "unordered_list":
                items_html = "\n".join(
                    f"            <li>{_escape_html(item)}</li>" for item in block["items"]
                )
                html_parts.append(f"        <ul>\n{items_html}\n        </ul>")
            elif btype == "blockquote":
                html_parts.append(
                    f"        <blockquote><p>{_escape_html(block['content'])}</p></blockquote>"
                )
            elif btype == "image":
                src = block["src"]
                if "name=" in src:
                    src = re.sub(r"name=\w+", "name=large", src)
                images.append(src)
                html_parts.append(
                    f'        <div class="article-image">'
                    f'<img src="{src}" alt="Article image">'
                    f"</div>"
                )

        log.info(
            "article_content_extracted",
            block_count=len(blocks),
            image_count=len(images),
            text_blocks=sum(1 for b in blocks if b["type"] == "text"),
            code_blocks=sum(1 for b in blocks if b["type"] == "code"),
        )

        return "\n".join(html_parts), images

    async def _extract_replies(self, page, main_author: str) -> list[dict]:
        """Extract replies from the conversation thread.

        Args:
            page: Playwright page object.
            main_author: Username of the main tweet author.

        Returns:
            List of reply dicts with author, content, images.
        """
        replies = []

        try:
            # Wait for page to stabilize
            await asyncio.sleep(2)

            # Try to click "Show replies" or similar buttons if they exist
            try:
                show_replies = await page.query_selector('text="Show replies"')
                if show_replies:
                    await show_replies.click()
                    await asyncio.sleep(2)
                    log.info("clicked_show_replies")
            except Exception:
                pass

            # Scroll down to load replies - scroll past the main tweet
            for i in range(5):
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(1)

            # Wait for any lazy-loaded content
            await asyncio.sleep(2)

            # Save debug screenshot
            await page.screenshot(path="/tmp/twitter_replies_debug.png", full_page=True)

            # Get all tweet articles - these contain the actual tweet content
            tweet_elements = await page.query_selector_all('article[data-testid="tweet"]')
            log.info("found_tweet_elements", count=len(tweet_elements))

            # Also try to find tweets in the conversation section
            if len(tweet_elements) <= 1:
                # Twitter wraps conversation in cellInnerDiv elements
                cell_elements = await page.query_selector_all(
                    '[data-testid="cellInnerDiv"] article'
                )
                log.info("found_cell_article_elements", count=len(cell_elements))
                if len(cell_elements) > len(tweet_elements):
                    tweet_elements = cell_elements

            # Skip the first one (main tweet) and process replies
            for i, tweet in enumerate(tweet_elements):
                if i == 0:
                    continue  # Skip main tweet

                try:
                    # Get reply author
                    author = ""
                    author_el = await tweet.query_selector('[data-testid="User-Name"] a')
                    if author_el:
                        href = await author_el.get_attribute("href")
                        if href:
                            author = href.strip("/").split("/")[0]

                    # Get reply text
                    text = ""
                    text_el = await tweet.query_selector('[data-testid="tweetText"]')
                    if text_el:
                        text = await text_el.inner_text()

                    # Get reply images
                    images = await self._extract_images(tweet)

                    # Get display name
                    display_name = author
                    name_span = await tweet.query_selector('[data-testid="User-Name"] span')
                    if name_span:
                        display_name = await name_span.inner_text()

                    if text or images:
                        replies.append(
                            {
                                "author": author,
                                "display_name": display_name,
                                "content": text,
                                "images": images,
                                "is_op": author.lower() == main_author.lower(),
                            }
                        )

                except Exception as e:
                    log.warning("reply_extraction_failed", index=i, error=str(e))
                    continue

            log.info("replies_extracted", count=len(replies))

        except Exception as e:
            log.warning("replies_extraction_failed", error=str(e))

        return replies

    def _create_article(self, tweet_data: dict, source_url: str) -> Article:
        """Create an Article from tweet data.

        Args:
            tweet_data: Extracted tweet data.
            source_url: Original URL.

        Returns:
            Article object.
        """
        content = tweet_data["content"]
        author = tweet_data["author"]
        display_name = tweet_data.get("display_name", author)
        timestamp = tweet_data.get("timestamp")
        images = tweet_data.get("images", [])
        replies = tweet_data.get("replies", [])
        is_article = tweet_data.get("is_article", False)
        article_title = tweet_data.get("title")

        # Create title - use article title if available, otherwise truncate content
        if article_title:
            title = article_title
        elif content:
            title = self._truncate_title(content)
        else:
            title = f"Tweet by @{author}"

        # Build HTML content
        date_str = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else ""

        if is_article:
            # Content already contains structured HTML with images from
            # _extract_article_content, so use it directly.
            html_content = f"""<article class="twitter-article">
    <header class="article-header">
        <h1>{title}</h1>
        <div class="article-meta">
            <span class="displayname">{display_name}</span>
            <span class="username">@{author}</span>
            <span class="date">{date_str}</span>
        </div>
    </header>
    <div class="article-content">
{content}
    </div>
</article>"""
        else:
            # Regular tweet format with images
            images_html = self._render_images(images)

            html_content = f"""<div class="tweet main-tweet">
    <div class="tweet-header">
        <span class="displayname">{display_name}</span>
        <span class="username">@{author}</span>
        <span class="date">{date_str}</span>
    </div>
    <div class="tweet-content">
        <p>{content}</p>
    </div>
    {images_html}
</div>"""

            # Add replies section if there are any
            if replies:
                html_content += '\n<div class="replies-section">\n'
                html_content += '    <h2 class="replies-header">Replies</h2>\n'

                for reply in replies:
                    reply_images_html = self._render_images(reply.get("images", []))
                    op_class = " op-reply" if reply.get("is_op") else ""
                    op_badge = ' <span class="op-badge">OP</span>' if reply.get("is_op") else ""

                    html_content += f"""    <div class="tweet reply{op_class}">
        <div class="tweet-header">
            <span class="displayname">{reply.get("display_name", reply["author"])}</span>{op_badge}
            <span class="username">@{reply["author"]}</span>
        </div>
        <div class="tweet-content">
            <p>{reply["content"]}</p>
        </div>
        {reply_images_html}
    </div>
"""
                html_content += "</div>"

        log.info(
            "tweet_converted_to_article",
            author=author,
            title=title,
            is_article=is_article,
            image_count=len(images),
            reply_count=len(replies),
        )

        return Article(
            title=title,
            author=author,
            content=html_content,
            published_at=timestamp,
            source_url=source_url,
            source_type="twitter_article" if is_article else "twitter",
        )

    def _render_images(self, images: list[str]) -> str:
        """Render images as HTML.

        Args:
            images: List of image URLs.

        Returns:
            HTML string with image tags.
        """
        if not images:
            return ""

        images_html = '<div class="tweet-images">\n'
        for img_url in images:
            images_html += f'        <img src="{img_url}" alt="Tweet image" loading="lazy">\n'
        images_html += "    </div>"
        return images_html

    def _truncate_title(self, text: str) -> str:
        """Truncate text to max title length.

        Args:
            text: Text to truncate.

        Returns:
            Truncated text with ellipsis if needed.
        """
        # Remove newlines for title
        text = text.replace("\n", " ").strip()
        if len(text) <= self.MAX_TITLE_LENGTH:
            return text
        return text[: self.MAX_TITLE_LENGTH - 3] + "..."
