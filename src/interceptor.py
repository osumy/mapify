"""Network interception and photo URL harvesting for Google Maps."""

import json
import re
from typing import Dict, List, Set, Any, Optional
from urllib.parse import unquote

from loguru import logger

PHOTO_URL_PATTERN = re.compile(
    r"https?://lh[0-9]\.googleusercontent\.com/(?:p|grass-cs|gpms-cs-s|gps-cs-s|gps-cs-p)/[a-zA-Z0-9_\-]+"
)


class PhotoInterceptor:
    """Listens to network traffic and DOM images in Playwright to harvest cafe photos."""

    def __init__(self, target_modifier: str = "=s0-rw"):
        self.target_modifier = target_modifier
        self.captured_photos: Dict[str, Dict[str, Any]] = {}

    def clear(self) -> None:
        self.captured_photos.clear()

    @staticmethod
    def to_high_res(url: str, modifier: str = "=s0-rw") -> str:
        """Transforms a Google CDN image URL into full camera master resolution."""
        if not url:
            return ""
        base = url.split("=")[0]
        return f"{base}{modifier}"

    def add_photo(self, raw_url: str, source_type: str = "gallery", author_name: Optional[str] = None) -> None:
        """Adds a photo URL if valid and not an avatar or placeholder."""
        if not raw_url:
            return
        decoded = unquote(raw_url)
        for m in PHOTO_URL_PATTERN.finditer(decoded):
            base_url = m.group(0)
            # Exclude user avatars and system icons
            if "/a/" in base_url or "/a-/" in base_url or "default-user" in base_url:
                continue

            # Ensure valid ID length (avoid truncated URLs)
            parts = base_url.rsplit("/", 1)
            if len(parts) == 2 and len(parts[1]) < 10:
                continue

            if base_url not in self.captured_photos:
                self.captured_photos[base_url] = {
                    "photo_url": base_url,
                    "high_res_url": self.to_high_res(base_url, self.target_modifier),
                    "source_type": source_type,
                    "author_name": author_name
                }

    def _extract_from_text(self, text: str, source: str = "rpc") -> None:
        """Finds all googleusercontent photos inside text/JSON payloads."""
        if not text:
            return
        decoded = unquote(text)
        for m in PHOTO_URL_PATTERN.finditer(decoded):
            self.add_photo(m.group(0), source_type=source)

    async def handle_response(self, response) -> None:
        """Callback for page.on('response')."""
        url = response.url
        # 1. Harvest from response URL directly
        if "googleusercontent.com" in url:
            self.add_photo(url, source_type="cdn_load")

        # 2. Check for Google Maps Photo / Place RPC endpoints
        if (
            "/maps/rpc/" in url
            or "/preview/place" in url
            or "batchexecute" in url
            or "photometa" in url
        ):
            try:
                content_type = response.headers.get("content-type", "")
                if ("json" in content_type or "text" in content_type or "javascript" in content_type) and "image" not in content_type:
                    body = await response.text()
                    self._extract_from_text(body, source="rpc")
            except Exception as e:
                logger.trace(f"Error reading response body from {url[:80]}: {e}")

    def handle_request(self, request) -> None:
        """Callback for page.on('request') to catch requests before any aborts."""
        url = request.url
        if "googleusercontent.com" in url:
            self.add_photo(url, source_type="request")

    def get_photos(self) -> List[Dict[str, Any]]:
        """Returns all deduplicated photos captured so far."""
        return list(self.captured_photos.values())
