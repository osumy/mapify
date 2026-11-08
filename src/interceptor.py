"""Network interception and photo URL harvesting for Google Maps with owner/customer classification."""

import asyncio
import re
from typing import Dict, List, Set, Any, Optional
from urllib.parse import unquote

from loguru import logger

PHOTO_URL_PATTERN = re.compile(
    r"https?://(?:lh[0-9]|streetviewpixels-pa)\.(?:googleusercontent|googleapis|ggpht)\.com/"
    r"(?:p|grass-cs|gpms-cs-s|gps-cs-s|gps-cs-p|panos|photos)/[a-zA-Z0-9_\-]+"
)

OWNER_URL_INDICATORS = ("/gpms-cs-s/", "/grass-cs/")


class PhotoInterceptor:
    """
    Listens to network traffic and DOM images in Playwright to harvest cafe photos.
    Distinguishes owner/merchant photos (cover, verified business uploads) from user-generated content.
    """

    def __init__(self, target_modifier: str = "=s0-rw"):
        self.target_modifier = target_modifier
        self.captured_photos: Dict[str, Dict[str, Any]] = {}
        self._pending_tasks: Set[asyncio.Task] = set()
        self.current_venue_title: Optional[str] = None

    def set_venue_context(self, venue_title: Optional[str] = None) -> None:
        """Sets the venue title to detect owner photos when author matches venue name."""
        self.current_venue_title = venue_title.strip() if venue_title else None

    def clear(self) -> None:
        """Clears captured photos and cancels in-flight background response tasks."""
        self.captured_photos.clear()
        self.current_venue_title = None
        for t in self._pending_tasks:
            if not t.done():
                t.cancel()
        self._pending_tasks.clear()

    async def drain_pending_tasks(self, timeout: float = 2.0) -> None:
        """Awaits all pending background response tasks before saving data."""
        if self._pending_tasks:
            active = [t for t in self._pending_tasks if not t.done()]
            if active:
                try:
                    await asyncio.wait(active, timeout=timeout)
                except Exception:
                    pass
            self._pending_tasks.clear()

    @staticmethod
    def to_high_res(url: str, modifier: str = "=s0-rw") -> str:
        """Transforms a Google CDN image URL into full camera master resolution."""
        if not url:
            return ""
        base = url.split("=")[0]
        return f"{base}{modifier}"

    def _determine_owner_status(
        self,
        base_url: str,
        source_type: str,
        author_name: Optional[str],
        explicit_is_owner: Optional[int]
    ) -> int:
        """Classifies whether a photo belongs to the business owner/merchant vs customer."""
        if explicit_is_owner is not None:
            return 1 if explicit_is_owner else 0

        # Cover photo or explicit owner tab/badge
        if source_type in ("cover", "owner", "merchant"):
            return 1

        # Google Profile Merchant Services & Curated brand assets
        if any(ind in base_url for ind in OWNER_URL_INDICATORS):
            return 1

        # Author name matches venue title
        if author_name and self.current_venue_title:
            a_clean = author_name.strip().lower()
            v_clean = self.current_venue_title.lower()
            if a_clean == v_clean or a_clean in v_clean or v_clean in a_clean:
                return 1

        return 0

    def add_photo(
        self,
        raw_url: str,
        source_type: str = "gallery",
        author_name: Optional[str] = None,
        is_owner_photo: Optional[int] = None
    ) -> None:
        """Adds a photo URL if valid, tagging with owner/customer status."""
        if not raw_url:
            return

        # Unescape slashes and decode url
        cleaned = raw_url.replace(r"\/", "/").replace("\\u002f", "/")
        decoded = unquote(cleaned)

        for m in PHOTO_URL_PATTERN.finditer(decoded):
            base_url = m.group(0)

            # Exclude user avatars, system icons, and placeholders
            if "/a/" in base_url or "/a-/" in base_url or "default-user" in base_url:
                continue

            # Ensure valid ID length (avoid truncated URLs)
            parts = base_url.rsplit("/", 1)
            if len(parts) == 2 and len(parts[1]) < 10:
                continue

            owner_flag = self._determine_owner_status(
                base_url=base_url,
                source_type=source_type,
                author_name=author_name,
                explicit_is_owner=is_owner_photo
            )

            if base_url not in self.captured_photos:
                self.captured_photos[base_url] = {
                    "photo_url": base_url,
                    "high_res_url": self.to_high_res(base_url, self.target_modifier),
                    "source_type": source_type,
                    "is_owner_photo": owner_flag,
                    "author_name": author_name
                }
            else:
                # If existing photo was UGC but new source marks it as owner, upgrade it
                if owner_flag == 1:
                    self.captured_photos[base_url]["is_owner_photo"] = 1
                    if source_type in ("cover", "owner", "merchant"):
                        self.captured_photos[base_url]["source_type"] = source_type

    def _extract_from_text(self, text: str, source: str = "rpc", is_owner_photo: Optional[int] = None) -> None:
        """Finds all googleusercontent photos inside text/JSON payloads."""
        if not text:
            return
        # Normalize escaped forward slashes in JSON/RPC payloads
        normalized = text.replace(r"\/", "/").replace("\\u002f", "/")
        decoded = unquote(normalized)
        for m in PHOTO_URL_PATTERN.finditer(decoded):
            self.add_photo(m.group(0), source_type=source, is_owner_photo=is_owner_photo)

    async def _safe_read_response(self, response) -> None:
        try:
            content_type = response.headers.get("content-type", "")
            if ("json" in content_type or "text" in content_type or "javascript" in content_type) and "image" not in content_type:
                body = await response.text()
                self._extract_from_text(body, source="rpc")
        except Exception:
            pass

    def handle_response(self, response) -> None:
        """Callback for page.on('response')."""
        url = response.url
        if "googleusercontent.com" in url or "ggpht.com" in url:
            self.add_photo(url, source_type="cdn_load")

        if any(k in url for k in ("/maps/rpc/", "/preview/place", "batchexecute", "photometa")):
            task = asyncio.create_task(self._safe_read_response(response))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

    def handle_request(self, request) -> None:
        """Callback for page.on('request') to catch requests before any aborts."""
        url = request.url
        if "googleusercontent.com" in url or "ggpht.com" in url:
            self.add_photo(url, source_type="request")

    def get_photos(self) -> List[Dict[str, Any]]:
        """Returns all deduplicated photos captured so far."""
        return list(self.captured_photos.values())
