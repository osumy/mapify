"""Configuration settings for Mapify Google Maps Photo Scraper."""

from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass
class ScraperConfig:
    # Database Paths
    SOURCE_DB_PATH: str = r"c:\Users\amira\Desktop\DB\v3\1405-08-12-cafochino.db"
    OUTPUT_DB_PATH: str = str(Path(__file__).parent.parent / "data" / "cafe_photos.db")

    # Search & Viewport Settings
    ZOOM_LEVEL: int = 17
    MATCH_RADIUS_METERS: float = 300.0
    MATCH_THRESHOLD: float = 0.75

    # Humanized Delays (in seconds)
    MIN_DELAY_PER_CAFE: float = 4.5
    MAX_DELAY_PER_CAFE: float = 8.5
    
    # Cooldowns & Session Lifecycle
    CAFES_PER_BATCH: int = 35
    BATCH_COOLDOWN_SECONDS: float = 120.0  # 2 minutes pause every 35 cafes
    RECYCLE_CONTEXT_EVERY: int = 20        # Re-open browser context every 20 places to clear RAM

    # Browser & Network Optimization
    HEADLESS: bool = True
    PAGE_TIMEOUT_MS: int = 30_000
    SETTLE_WAIT_MS: int = 2_500
    
    # Resource Blocking to save RAM and bandwidth without breaking layout
    BLOCKED_RESOURCE_TYPES: tuple[str, ...] = (
        "media", "font"
    )

    # Pre-baked Consent Cookies to bypass EU/GDPR consent walls
    CONSENT_COOKIES: list[dict] = field(default_factory=lambda: [
        {
            "name": "SOCS",
            "value": "CAESNQgDEitwSgJBUhADGgEGATAW",
            "domain": ".google.com",
            "path": "/",
        },
        {
            "name": "CONSENT",
            "value": "YES+",
            "domain": ".google.com",
            "path": "/",
        }
    ])

    # Photo Quality Modifier: '=s0-rw' forces original maximum camera resolution in WebP format
    PHOTO_MODIFIER: str = "=s0-rw"
