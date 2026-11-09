"""Hardened Playwright scraper orchestrator for Mapify with extended metadata extraction."""

import asyncio
import random
import re
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import quote_plus

from loguru import logger
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Error
from playwright_stealth import Stealth

from src.config import ScraperConfig
from src.db import CafeRecord, DatabaseManager
from src.extractor import extract_place_metadata
from src.interceptor import PhotoInterceptor
from src.matcher import evaluate_candidate, haversine_distance, MatchResult

RE_PIN_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
RE_VIEWPORT_COORDS = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")


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
        self.total_places_processed = 0

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
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
            ]
        )
        await self._create_new_context()

    async def _create_new_context(self) -> None:
        """Creates a clean browser context with dynamic UA matching Chromium engine version."""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass

        chrome_ver = self.browser.version
        major_ver = chrome_ver.split(".")[0]
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"

        self.context = await self.browser.new_context(
            user_agent=ua,
            locale="fa-IR",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "fa,en-US;q=0.9,en;q=0.8",
                "sec-ch-ua": f'"Chromium";v="{major_ver}", "Google Chrome";v="{major_ver}", "Not=A?Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )

        # Pre-seed consent cookies
        await self.context.add_cookies(self.config.CONSENT_COOKIES)

        self.page = await self.context.new_page()

        # Stealth & Chrome runtime polyfill
        await Stealth().apply_stealth_async(self.page)
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        # Block fonts & video to save bandwidth while keeping styles and images functional
        await self.page.route(
            "**/*",
            lambda r: r.abort() if r.request.resource_type in self.config.BLOCKED_RESOURCE_TYPES else r.continue_()
        )

        # Attach network interceptors
        self.page.on("response", self.interceptor.handle_response)
        self.page.on("request", self.interceptor.handle_request)
        self.requests_in_context = 0

    async def _ensure_browser_alive(self) -> None:
        """Auto-recovers from Chromium crashes or restarts browser every 300 places to prevent memory leaks."""
        needs_restart = (
            not self.browser
            or not self.browser.is_connected()
            or self.total_places_processed >= 300
        )
        if needs_restart:
            logger.info("Recycling Chromium browser process to maintain zero RAM leaks...")
            try:
                await self.close()
            except Exception:
                pass
            await self.start()
            self.total_places_processed = 0

    async def close(self) -> None:
        """Cleans up all browser resources."""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
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

    async def _handle_consent_modal_if_present(self) -> None:
        """Bypasses European/GDPR Google Consent modals if encountered."""
        try:
            consent_btn = self.page.locator(
                "form[action*='consent'] button, button:has-text('Accept all'), button:has-text('قبول همه'), button:has-text('I agree')"
            ).first
            if await consent_btn.is_visible(timeout=500):
                await consent_btn.click()
                await self.page.wait_for_timeout(1000)
        except Exception:
            pass

    async def _wait_for_page_signal(self, max_wait_seconds: float = 7.5) -> str:
        """
        Actively polls the page every 250ms up to max_wait_seconds.
        Returns: 'captcha', 'direct_place', 'search_feed', or 'not_found'.
        """
        start_time = asyncio.get_event_loop().time()
        await self._handle_consent_modal_if_present()

        while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
            cur_url = self.page.url
            if "google.com/sorry" in cur_url:
                return "captcha"

            # Direct Place check: URL is /maps/place/ AND h1.DUwDvf is visible
            if "/maps/place/" in cur_url:
                h1_el = self.page.locator("h1.DUwDvf, h1").first
                if await h1_el.is_visible():
                    return "direct_place"

            # Search Feed check: div[role='feed'] is in DOM
            if await self.page.locator("div[role='feed']").count() > 0:
                return "search_feed"

            # Not found messages
            not_found_el = self.page.locator(
                "text='نتیجه‌ای یافت نشد', text='هیچ نتیجه‌ای پیدا نشد', text=\"Google Maps can't find\""
            )
            if await not_found_el.count() > 0:
                return "not_found"

            await asyncio.sleep(0.25)

        # Fallback evaluation
        cur_url = self.page.url
        if "google.com/sorry" in cur_url:
            return "captcha"
        if "/maps/place/" in cur_url:
            return "direct_place"
        if await self.page.locator("div[role='feed']").count() > 0:
            return "search_feed"
        return "not_found"

    async def scrape_cafe(self, cafe: CafeRecord) -> Dict[str, Any]:
        """Scrapes photos and extended metadata for a single cafe record."""
        await self._ensure_browser_alive()
        self.requests_in_context += 1
        self.total_places_processed += 1

        if self.requests_in_context >= self.config.RECYCLE_CONTEXT_EVERY:
            logger.debug(f"Recycling browser context after {self.requests_in_context} requests to free RAM.")
            await self._create_new_context()

        self.interceptor.clear()

        # If canonical google_maps_url is already known, navigate directly (fast, zero search overhead)
        if cafe.google_maps_url and "/maps/place/" in cafe.google_maps_url:
            try:
                return await self._handle_direct_url(cafe, cafe.google_maps_url)
            except Error as e:
                if "target closed" in str(e).lower() or "connection closed" in str(e).lower():
                    logger.warning("Browser target closed. Triggering recovery...")
                    await self._create_new_context()
                    return {"status": "error", "error": "TargetClosedError", "photos": []}
                logger.error(f"Playwright error on direct place '{cafe.name}' ({cafe.id}): {e}")
                self.db.save_place_and_photos(cafe=cafe, status="error", search_url=cafe.google_maps_url)
                return {"status": "error", "error": str(e), "photos": []}
            except Exception as e:
                logger.error(f"Error on direct place '{cafe.name}' ({cafe.id}): {e}")
                self.db.save_place_and_photos(cafe=cafe, status="error", search_url=cafe.google_maps_url)
                return {"status": "error", "error": str(e), "photos": []}

        # Otherwise, fall back to geo search query
        search_url = self.build_search_url(cafe)
        logger.info(f"[{cafe.id}] Searching for '{cafe.name}' in {cafe.city or 'Iran'}...")

        try:
            resp = await self.page.goto(search_url, wait_until="domcontentloaded", timeout=self.config.PAGE_TIMEOUT_MS)
            if resp and resp.status in (429, 503):
                logger.warning(f"HTTP {resp.status} Rate Limit encountered on '{cafe.name}'!")
                return {"status": "rate_limited", "photos": []}

            signal = await self._wait_for_page_signal(max_wait_seconds=7.5)
            current_url = self.page.url

            if signal == "captcha" or "google.com/sorry" in current_url:
                logger.warning(f"Rate-limit / CAPTCHA detected on '{cafe.name}'! URL: {current_url}")
                return {"status": "rate_limited", "photos": []}

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

        except Error as e:
            if "target closed" in str(e).lower() or "connection closed" in str(e).lower():
                logger.warning("Browser target closed. Triggering recovery...")
                await self._create_new_context()
                return {"status": "error", "error": "TargetClosedError", "photos": []}
            logger.error(f"Playwright error scraping cafe '{cafe.name}' ({cafe.id}): {e}")
            self.db.save_place_and_photos(cafe=cafe, status="error", search_url=search_url)
            return {"status": "error", "error": str(e), "photos": []}
        except Exception as e:
            logger.error(f"Error scraping cafe '{cafe.name}' ({cafe.id}): {e}")
            self.db.save_place_and_photos(
                cafe=cafe,
                status="error",
                search_url=search_url
            )
            return {"status": "error", "error": str(e), "photos": []}

    @staticmethod
    def get_clean_place_url(url: str) -> str:
        """Returns clean Google Maps place overview URL, stripping any photo viewer parameters."""
        if not url:
            return ""
        from src.db import extract_place_identifiers
        ids = extract_place_identifiers(url)
        if ids.get("place_id"):
            return f"https://www.google.com/maps/place/?q=place_id:{ids['place_id']}&hl=fa"
        # Fallback: strip photo modal / streetview params if present
        clean = re.sub(r",3a,[^/]+", "", url)
        clean = re.sub(r"/data=![0-9a-zA-Z!_.-]+", "", clean)
        return clean

    async def _handle_direct_url(self, cafe: CafeRecord, target_url: str) -> Dict[str, Any]:
        """Directly loads canonical place URL, extracting Level 1 metadata and photos with zero search overhead."""
        clean_url = self.get_clean_place_url(target_url)
        logger.info(f"[{cafe.id}] Direct navigating to '{cafe.name}'...")
        resp = await self.page.goto(clean_url, wait_until="domcontentloaded", timeout=self.config.PAGE_TIMEOUT_MS)
        if resp and resp.status in (429, 503):
            logger.warning(f"HTTP {resp.status} Rate Limit encountered on '{cafe.name}'!")
            return {"status": "rate_limited", "photos": []}

        signal = await self._wait_for_page_signal(max_wait_seconds=7.5)
        current_url = self.page.url

        if signal == "captcha" or "google.com/sorry" in current_url:
            logger.warning(f"Rate-limit / CAPTCHA detected on '{cafe.name}'! URL: {current_url}")
            return {"status": "rate_limited", "photos": []}

        # Wait briefly for overview pane hydration
        await self.page.wait_for_timeout(self.config.SETTLE_WAIT_MS)

        metadata = await extract_place_metadata(self.page)
        title = metadata.get("title") or await self._get_h1_title() or cafe.name
        lat, lng = self._extract_coords_from_url(current_url)
        if lat is None or lng is None:
            lat, lng = cafe.latitude, cafe.longitude

        logger.success(f"Loaded '{cafe.name}' -> '{title}' ({metadata.get('category_name') or 'N/A'}, score={metadata.get('total_score')}, revs={metadata.get('reviews_count')})")

        photos = await self._harvest_photos(venue_title=title)
        owner_photos_count = sum(1 for p in photos if p.get("is_owner_photo") == 1)
        logger.info(f"Harvested {len(photos)} photos ({owner_photos_count} owner) for '{cafe.name}'.")

        from src.db import extract_place_identifiers
        ids = extract_place_identifiers(cafe.google_maps_url or target_url)

        self.db.save_place_and_photos(
            cafe=cafe,
            status="matched",
            title=title,
            category_name=metadata.get("category_name"),
            place_id=ids.get("place_id"),
            cid=ids.get("cid"),
            fid=ids.get("fid"),
            kgmid=ids.get("kgmid"),
            scraped_lat=lat,
            scraped_lng=lng,
            address=metadata.get("address") or cafe.address,
            phone=metadata.get("phone") or cafe.phone,
            phone_unformatted=metadata.get("phone_unformatted"),
            website=metadata.get("website") or cafe.website,
            total_score=metadata.get("total_score"),
            reviews_count=metadata.get("reviews_count"),
            plus_code=metadata.get("plus_code"),
            is_claimed=metadata.get("is_claimed"),
            is_permanently_closed=metadata.get("is_permanently_closed", False),
            is_temporarily_closed=metadata.get("is_temporarily_closed", False),
            price_level=metadata.get("price_level"),
            located_in=metadata.get("located_in"),
            menu_url=metadata.get("menu_url"),
            opening_hours=metadata.get("opening_hours"),
            popular_times=metadata.get("popular_times"),
            name_score=1.0,
            distance_meters=0.0,
            search_url="",
            google_maps_url=current_url if "/maps/place/" in current_url else target_url,
            photos=photos
        )

        return {"status": "matched", "title": title, "metadata": metadata, "photos": photos}

    async def _handle_direct_place_page(self, cafe: CafeRecord, search_url: str, place_url: str) -> Dict[str, Any]:
        """Handles when Google Maps navigated directly to a single place page via geo search."""
        await self.page.wait_for_timeout(self.config.SETTLE_WAIT_MS)
        metadata = await extract_place_metadata(self.page)
        title = metadata.get("title") or await self._get_h1_title() or cafe.name
        lat, lng = self._extract_coords_from_url(place_url)

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

        logger.success(f"Matched '{cafe.name}' -> '{title}' ({match.distance_meters:.1f}m, score={match.composite_score})")
        photos = await self._harvest_photos(venue_title=title)
        owner_photos_count = sum(1 for p in photos if p.get("is_owner_photo") == 1)
        logger.info(f"Harvested {len(photos)} photos ({owner_photos_count} owner) for '{cafe.name}'.")

        from src.db import extract_place_identifiers
        ids = extract_place_identifiers(cafe.google_maps_url or target_url)

        self.db.save_place_and_photos(
            cafe=cafe,
            status="matched",
            title=title,
            category_name=metadata.get("category_name"),
            place_id=ids.get("place_id"),
            cid=ids.get("cid"),
            fid=ids.get("fid"),
            kgmid=ids.get("kgmid"),
            scraped_lat=lat,
            scraped_lng=lng,
            address=metadata.get("address") or cafe.address,
            phone=metadata.get("phone") or cafe.phone,
            phone_unformatted=metadata.get("phone_unformatted"),
            website=metadata.get("website") or cafe.website,
            total_score=metadata.get("total_score"),
            reviews_count=metadata.get("reviews_count"),
            plus_code=metadata.get("plus_code"),
            is_claimed=metadata.get("is_claimed"),
            is_permanently_closed=metadata.get("is_permanently_closed", False),
            is_temporarily_closed=metadata.get("is_temporarily_closed", False),
            price_level=metadata.get("price_level"),
            located_in=metadata.get("located_in"),
            menu_url=metadata.get("menu_url"),
            opening_hours=metadata.get("opening_hours"),
            popular_times=metadata.get("popular_times"),
            name_score=match.name_score,
            distance_meters=match.distance_meters,
            search_url=search_url,
            google_maps_url=self.page.url,
            photos=photos
        )

        return {"status": "matched", "title": title, "metadata": metadata, "photos": photos}

    async def _handle_search_feed_page(self, cafe: CafeRecord, search_url: str) -> Dict[str, Any]:
        """Handles multi-result search feed by evaluating top candidates within 300m."""
        feed_container = self.page.locator("div[role='feed']")
        result_links = feed_container.locator("a[href*='/maps/place/']")
        count = await result_links.count()

        if count == 0:
            logger.info(f"No results found for '{cafe.name}' within search area.")
            self.db.save_place_and_photos(cafe=cafe, status="not_found", search_url=search_url)
            return {"status": "not_found", "photos": []}

        best_candidate = None
        best_match: Optional[MatchResult] = None
        inspect_limit = min(count, 10)

        for i in range(inspect_limit):
            link = result_links.nth(i)
            href = await link.get_attribute("href") or ""
            cand_title = await link.get_attribute("aria-label") or ""
            cand_lat, cand_lng = self._extract_coords_from_href(href)

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
            self.db.save_place_and_photos(cafe=cafe, status="not_found", search_url=search_url)
            return {"status": "not_found", "photos": []}

        # Navigate directly to the candidate's canonical place URL to prevent pushState delay
        logger.success(f"Feed match for '{cafe.name}' -> '{best_candidate['title']}' ({best_match.distance_meters:.1f}m)")
        try:
            await self.page.goto(best_candidate["href"], wait_until="domcontentloaded", timeout=15_000)
            await self.page.locator("h1.DUwDvf, h1").first.wait_for(state="visible", timeout=6_000)
        except Exception:
            try:
                await best_candidate["link_el"].click()
                await self.page.locator("h1.DUwDvf, h1").first.wait_for(state="visible", timeout=6_000)
            except Exception:
                pass

        await self.page.wait_for_timeout(self.config.SETTLE_WAIT_MS)
        metadata = await extract_place_metadata(self.page)
        title = metadata.get("title") or await self._get_h1_title() or best_candidate["title"]
        photos = await self._harvest_photos(venue_title=title)
        canonical_url = self.page.url if "/maps/place/" in self.page.url else best_candidate["href"]
        owner_photos_count = sum(1 for p in photos if p.get("is_owner_photo") == 1)
        logger.info(f"Harvested {len(photos)} photos ({owner_photos_count} owner) for '{cafe.name}'.")

        self.db.save_place_and_photos(
            cafe=cafe,
            status="matched",
            title=title,
            category_name=metadata.get("category_name"),
            scraped_lat=best_candidate["lat"],
            scraped_lng=best_candidate["lng"],
            address=metadata.get("address") or cafe.address,
            phone=metadata.get("phone") or cafe.phone,
            phone_unformatted=metadata.get("phone_unformatted"),
            website=metadata.get("website") or cafe.website,
            total_score=metadata.get("total_score"),
            reviews_count=metadata.get("reviews_count"),
            plus_code=metadata.get("plus_code"),
            is_claimed=metadata.get("is_claimed"),
            is_permanently_closed=metadata.get("is_permanently_closed", False),
            is_temporarily_closed=metadata.get("is_temporarily_closed", False),
            price_level=metadata.get("price_level"),
            located_in=metadata.get("located_in"),
            menu_url=metadata.get("menu_url"),
            opening_hours=metadata.get("opening_hours"),
            popular_times=metadata.get("popular_times"),
            name_score=best_match.name_score,
            distance_meters=best_match.distance_meters,
            search_url=search_url,
            google_maps_url=canonical_url,
            photos=photos
        )
        return {"status": "matched", "title": title, "metadata": metadata, "photos": photos}

    async def _harvest_photos(self, venue_title: Optional[str] = None) -> List[Dict[str, Any]]:
        """Harvests photos and distinguishes owner uploads from public uploads."""
        self.interceptor.set_venue_context(venue_title)

        # 1. Harvest hero/cover image first as guaranteed owner/cover photo
        try:
            hero_img = self.page.locator("div[jsaction*='pane.heroHeaderImage'] img, button[jsaction*='heroHeaderImage'] img").first
            if await hero_img.is_visible(timeout=1000):
                hero_src = await hero_img.get_attribute("src")
                if hero_src:
                    self.interceptor.add_photo(hero_src, source_type="cover", is_owner_photo=1)
        except Exception:
            pass

        # 2. Trigger photo drawer & owner tab
        await self._trigger_photo_drawer()
        await self.interceptor.drain_pending_tasks(timeout=2.0)

        # 3. Extract DOM images
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
        """Clicks on photos tab or hero image, checks for Owner tab, and triggers pagination RPCs."""
        try:
            photo_btn = self.page.locator(
                "button[role='tab'][aria-label*='عکس'], button[role='tab'][aria-label*='Photo'], "
                "button[role='tab'][aria-label*='Photos'], button.AoCdqe, button[aria-label*='عکس'], "
                "button[jsaction*='photo'], div[jsaction*='pane.heroHeaderImage']"
            ).first

            if await photo_btn.is_visible(timeout=1500):
                await photo_btn.click()
                await self.page.wait_for_timeout(1500)

                # Check if "از مالک" / "By owner" tab is available in the gallery header
                try:
                    owner_tab = self.page.locator(
                        "button[role='tab']:has-text('از مالک'), button[role='tab']:has-text('By owner'), "
                        "button:has-text('از مالک'), button:has-text('By owner'), "
                        "button[aria-label*='مالک'], button[aria-label*='owner' i]"
                    ).first
                    if await owner_tab.is_visible(timeout=800):
                        await owner_tab.click()
                        await self.page.wait_for_timeout(1000)
                        # Scroll owner tab
                        for _ in range(2):
                            await self.page.keyboard.press("PageDown")
                            await self.page.wait_for_timeout(400)

                        # Tag currently captured photos as owner
                        for photo in self.interceptor.captured_photos.values():
                            photo["is_owner_photo"] = 1
                            if photo.get("source_type") != "cover":
                                photo["source_type"] = "owner"

                        # Return to "همه" / "All" tab to capture full gallery as well
                        all_tab = self.page.locator(
                            "button[role='tab']:has-text('همه'), button[role='tab']:has-text('All'), "
                            "button:has-text('همه'), button:has-text('All')"
                        ).first
                        if await all_tab.is_visible(timeout=800):
                            await all_tab.click()
                            await self.page.wait_for_timeout(1000)
                except Exception:
                    pass

                # Scroll the photo drawer 2-3 times to trigger pagination RPCs for all photos
                for _ in range(3):
                    await self.page.keyboard.press("PageDown")
                    await self.page.wait_for_timeout(400)
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
        m_pin = RE_PIN_COORDS.search(url)
        if m_pin:
            return float(m_pin.group(1)), float(m_pin.group(2))
        m_vp = RE_VIEWPORT_COORDS.search(url)
        if m_vp:
            return float(m_vp.group(1)), float(m_vp.group(2))
        return None, None

    @staticmethod
    def _extract_coords_from_href(href: str) -> Tuple[Optional[float], Optional[float]]:
        m_pin = RE_PIN_COORDS.search(href)
        if m_pin:
            return float(m_pin.group(1)), float(m_pin.group(2))
        m_vp = RE_VIEWPORT_COORDS.search(href)
        if m_vp:
            return float(m_vp.group(1)), float(m_vp.group(2))
        return None, None
