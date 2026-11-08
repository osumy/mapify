"""Core scraper orchestrator for Mapify: Playwright Stealth + Network Interception."""

import asyncio
import random
import re
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import quote_plus, unquote

from loguru import logger
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

from src.config import ScraperConfig
from src.db import CafeRecord, DatabaseManager
from src.interceptor import PhotoInterceptor
from src.matcher import evaluate_candidate, haversine_distance, MatchResult

RE_HREF_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
RE_URL_COORDS = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")


class MapifyScraper:
    def __init__(self, config: ScraperConfig, db_manager: DatabaseManager):
        self.config = config
        self.db = db_manager
        self.interceptor = PhotoInterceptor(target_modifier=config.PHOTO_MODIFIER)
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.requests_in_context = 0

    async def start(self) -> None:
        """Launches Chromium with anti-bot bypass parameters."""
        logger.info("Initializing Playwright Chromium browser...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.config.HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        await self._create_new_context()

    async def _create_new_context(self) -> None:
        """Creates a clean browser context with stealth, cookies, and route filtering."""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass

        logger.debug("Creating fresh browser context with stealth & consent cookies...")
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="fa-IR",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "fa,en-US;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="128", "Google Chrome";v="128", "Not=A?Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
        )

        # Pre-seed consent cookies
        await self.context.add_cookies(self.config.CONSENT_COOKIES)

        self.page = await self.context.new_page()
        await Stealth().apply_stealth_async(self.page)

        # Block fonts and media to save bandwidth while keeping styles and images functional
        await self.page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in self.config.BLOCKED_RESOURCE_TYPES
            else route.continue_()
        )

        # Attach network interceptor for both responses and requests
        self.page.on("response", self.interceptor.handle_response)
        self.page.on("request", self.interceptor.handle_request)
        self.requests_in_context = 0

    async def close(self) -> None:
        """Cleans up browser resources."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Scraper browser closed successfully.")

    def build_search_url(self, cafe: CafeRecord) -> str:
        """Constructs a geo-targeted Google Maps search URL."""
        encoded_name = quote_plus(cafe.name.strip())
        return f"https://www.google.com/maps/search/{encoded_name}/@{cafe.latitude},{cafe.longitude},{self.config.ZOOM_LEVEL}z?hl=fa"

    async def _safe_delay(self) -> None:
        """Humanized randomized delay with jitter."""
        delay = random.uniform(self.config.MIN_DELAY_PER_CAFE, self.config.MAX_DELAY_PER_CAFE)
        logger.debug(f"Pacing pause for {delay:.2f}s...")
        await asyncio.sleep(delay)

    async def _wait_for_page_signal(self, max_wait_seconds: float = 7.5) -> str:
        """
        Actively polls the page every 250ms up to max_wait_seconds.
        Returns: 'captcha', 'direct_place', 'search_feed', or 'not_found'.
        """
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
            cur_url = self.page.url
            if "google.com/sorry" in cur_url:
                return "captcha"

            # Check if URL transitioned to /maps/place/
            if "/maps/place/" in cur_url:
                h1_el = self.page.locator("h1.DUwDvf, h1").first
                if await h1_el.is_visible():
                    return "direct_place"

            # Check if search result cards are rendered
            feed_links = self.page.locator("a[href*='/maps/place/']")
            count = await feed_links.count()
            if count > 0:
                if "/maps/place/" in cur_url:
                    return "direct_place"
                return "search_feed"

            # Check for explicit "not found" message
            not_found_el = self.page.locator("text='نتیجه‌ای یافت نشد', text='هیچ نتیجه‌ای پیدا نشد', text='Google Maps can\'t find'")
            if await not_found_el.count() > 0:
                return "not_found"

            await asyncio.sleep(0.25)

        # Fallback evaluation after max_wait_seconds
        cur_url = self.page.url
        if "google.com/sorry" in cur_url:
            return "captcha"
        if "/maps/place/" in cur_url:
            return "direct_place"
        if await self.page.locator("a[href*='/maps/place/']").count() > 0:
            return "search_feed"
        return "not_found"

    async def scrape_cafe(self, cafe: CafeRecord) -> Dict[str, Any]:
        """Scrapes photos and details for a single cafe record."""
        # Context recycling to prevent Chromium memory leaks
        self.requests_in_context += 1
        if self.requests_in_context >= self.config.RECYCLE_CONTEXT_EVERY:
            logger.debug(f"Recycling browser context after {self.requests_in_context} requests to free RAM.")
            await self._create_new_context()

        self.interceptor.clear()
        search_url = self.build_search_url(cafe)
        logger.info(f"[{cafe.id}] Searching for '{cafe.name}' in {cafe.city or 'Iran'}...")

        try:
            # 1. Navigate to search URL
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=self.config.PAGE_TIMEOUT_MS)

            # 2. Wait for active page signal (direct place, search feed, not found, or captcha)
            signal = await self._wait_for_page_signal(max_wait_seconds=7.5)
            current_url = self.page.url

            # 3. Check for CAPTCHA / rate-limit redirect
            if signal == "captcha" or "google.com/sorry" in current_url:
                logger.warning(f"Rate-limit / CAPTCHA detected on '{cafe.name}'! URL: {current_url}")
                logger.info("Triggering defensive backoff cooldown of 60 seconds...")
                await asyncio.sleep(60.0)
                await self._create_new_context()
                return {"status": "rate_limited", "photos": []}

            # 4. Handle Direct Place vs Search Feed vs Not Found
            if signal == "direct_place":
                return await self._handle_direct_place_page(cafe, search_url, current_url)
            elif signal == "search_feed":
                return await self._handle_search_feed_page(cafe, search_url)
            else:
                logger.info(f"No results found for '{cafe.name}' within search area.")
                self.db.save_place_and_photos(
                    cafe=cafe,
                    status="not_found",
                    search_url=search_url
                )
                return {"status": "not_found", "photos": []}

        except Exception as e:
            logger.error(f"Error scraping cafe '{cafe.name}' ({cafe.id}): {e}")
            self.db.save_place_and_photos(
                cafe=cafe,
                status="error",
                search_url=search_url
            )
            return {"status": "error", "error": str(e), "photos": []}

    async def _handle_direct_place_page(self, cafe: CafeRecord, search_url: str, place_url: str) -> Dict[str, Any]:
        """Handles when Google Maps navigated directly to a single place page."""
        title = await self._get_h1_title()
        lat, lng = self._extract_coords_from_url(place_url)

        # Match evaluation
        match = evaluate_candidate(
            target_name=cafe.name,
            target_lat=cafe.latitude,
            target_lng=cafe.longitude,
            candidate_title=title or cafe.name,
            candidate_lat=lat if lat is not None else cafe.latitude,
            candidate_lng=lng if lng is not None else cafe.longitude,
            max_radius=self.config.MATCH_RADIUS_METERS,
            match_threshold=self.config.MATCH_THRESHOLD
        )

        if not match.is_match:
            logger.warning(f"Direct place rejected for '{cafe.name}': {match.rejection_reason} (Found: '{title}')")
            self.db.save_place_and_photos(
                cafe=cafe,
                status="not_found",
                title=title,
                scraped_lat=lat,
                scraped_lng=lng,
                name_score=match.name_score,
                distance_meters=match.distance_meters,
                search_url=search_url,
                google_maps_url=place_url
            )
            return {"status": "not_found", "photos": []}

        # Candidate is matched! Extract photos
        logger.success(f"Matched '{cafe.name}' -> '{title}' ({match.distance_meters:.1f}m, score={match.composite_score})")
        photos = await self._harvest_photos()
        logger.info(f"Harvested {len(photos)} photos for '{cafe.name}'.")

        self.db.save_place_and_photos(
            cafe=cafe,
            status="matched",
            title=title,
            scraped_lat=lat,
            scraped_lng=lng,
            name_score=match.name_score,
            distance_meters=match.distance_meters,
            search_url=search_url,
            google_maps_url=place_url,
            photos=photos
        )

        return {"status": "matched", "title": title, "photos": photos}

    async def _handle_search_feed_page(self, cafe: CafeRecord, search_url: str) -> Dict[str, Any]:
        """Handles multi-result search feed by evaluating top 10 candidates within 300m."""
        result_links = self.page.locator("a[href*='/maps/place/']")
        count = await result_links.count()

        if count == 0:
            logger.info(f"No results found for '{cafe.name}' within search area.")
            self.db.save_place_and_photos(
                cafe=cafe,
                status="not_found",
                search_url=search_url
            )
            return {"status": "not_found", "photos": []}

        best_candidate = None
        best_match: Optional[MatchResult] = None
        inspect_limit = min(count, 10)

        for i in range(inspect_limit):
            link = result_links.nth(i)
            href = await link.get_attribute("href") or ""
            cand_title = await link.get_attribute("aria-label") or ""

            # Extract coordinates from href (!3dlat!4dlng or /@lat,lng)
            coords = self._extract_coords_from_href(href)
            cand_lat, cand_lng = coords if coords else (None, None)

            m = evaluate_candidate(
                target_name=cafe.name,
                target_lat=cafe.latitude,
                target_lng=cafe.longitude,
                candidate_title=cand_title,
                candidate_lat=cand_lat,
                candidate_lng=cand_lng,
                max_radius=self.config.MATCH_RADIUS_METERS,
                match_threshold=self.config.MATCH_THRESHOLD
            )

            if m.is_match:
                if not best_match or m.composite_score > best_match.composite_score:
                    best_match = m
                    best_candidate = {
                        "link_el": link,
                        "title": cand_title,
                        "href": href,
                        "lat": cand_lat,
                        "lng": cand_lng
                    }

        if not best_candidate or not best_match:
            logger.warning(f"No candidate matched for '{cafe.name}' among {inspect_limit} results.")
            self.db.save_place_and_photos(
                cafe=cafe,
                status="not_found",
                search_url=search_url
            )
            return {"status": "not_found", "photos": []}

        # Click into matched place to trigger full gallery / photos
        logger.success(
            f"Feed match for '{cafe.name}' -> '{best_candidate['title']}' "
            f"({best_match.distance_meters:.1f}m, score={best_match.composite_score})"
        )
        try:
            await best_candidate["link_el"].click()
            await self._wait_for_page_signal(max_wait_seconds=5.0)
        except Exception as e:
            logger.trace(f"Click on result link failed, navigating directly: {e}")
            await self.page.goto(best_candidate["href"], wait_until="domcontentloaded", timeout=15_000)
            await self._wait_for_page_signal(max_wait_seconds=5.0)

        photos = await self._harvest_photos()
        logger.info(f"Harvested {len(photos)} photos for '{cafe.name}'.")

        self.db.save_place_and_photos(
            cafe=cafe,
            status="matched",
            title=best_candidate["title"],
            scraped_lat=best_candidate["lat"],
            scraped_lng=best_candidate["lng"],
            name_score=best_match.name_score,
            distance_meters=best_match.distance_meters,
            search_url=search_url,
            google_maps_url=self.page.url,
            photos=photos
        )

        return {"status": "matched", "title": best_candidate["title"], "photos": photos}

    async def _harvest_photos(self) -> List[Dict[str, Any]]:
        """Harvests photos by triggering the photo gallery and reading DOM images."""
        # 1. Trigger photo drawer / tab
        await self._trigger_photo_drawer()

        # 2. Extract DOM images
        try:
            dom_imgs = await self.page.locator("img, div[style*='background-image']").evaluate_all('''elements => {
                const res = [];
                for (let el of elements) {
                    if (el.src) res.push(el.src);
                    const bg = el.style.backgroundImage;
                    if (bg) res.push(bg);
                }
                return res;
            }''')
            for src in dom_imgs:
                self.interceptor.add_photo(src, source_type="dom")
        except Exception as e:
            logger.trace(f"Error harvesting DOM images: {e}")

        return self.interceptor.get_photos()

    async def _trigger_photo_drawer(self) -> None:
        """Clicks on photos tab or hero image to trigger Google Maps photo gallery RPCs."""
        try:
            # Try finding the photos tab or hero header button
            photo_btn = self.page.locator(
                "button[role='tab'][aria-label*='عکس'], button[role='tab'][aria-label*='Photo'], "
                "button[role='tab'][aria-label*='Photos'], button.AoCdqe, button[aria-label*='عکس'], "
                "button[jsaction*='photo'], div[jsaction*='pane.heroHeaderImage']"
            ).first

            if await photo_btn.is_visible(timeout=1500):
                await photo_btn.click()
                await self.page.wait_for_timeout(1500)

                # Scroll the photo drawer 2-3 times to trigger pagination RPCs
                for _ in range(3):
                    await self.page.keyboard.press("PageDown")
                    await self.page.wait_for_timeout(500)
        except Exception as e:
            logger.trace(f"Photo drawer trigger note: {e}")

    async def _get_h1_title(self) -> Optional[str]:
        try:
            el = self.page.locator("h1.DUwDvf, h1").first
            if await el.is_visible(timeout=1500):
                text = await el.inner_text()
                return text.strip()
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_coords_from_url(url: str) -> Tuple[Optional[float], Optional[float]]:
        m = RE_URL_COORDS.search(url)
        if m:
            return float(m.group(1)), float(m.group(2))
        return None, None

    @staticmethod
    def _extract_coords_from_href(href: str) -> Optional[Tuple[float, float]]:
        m = RE_HREF_COORDS.search(href)
        if m:
            return float(m.group(1)), float(m.group(2))
        m2 = RE_URL_COORDS.search(href)
        if m2:
            return float(m2.group(1)), float(m2.group(2))
        return None
