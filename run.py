"""CLI runner for Mapify Google Maps Cafe Photo Scraper."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from loguru import logger
from tqdm import tqdm

from src.config import ScraperConfig
from src.db import DatabaseManager
from src.scraper import MapifyScraper


def setup_logger(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    # Console handler
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
        colorize=True
    )
    # File handler
    logger.add(
        log_dir / "scraper_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="50 MB",
        retention="10 days",
        encoding="utf-8"
    )


async def run_pipeline(
    scraper: MapifyScraper,
    pending_cafes: list,
    config: ScraperConfig,
    shard_name: Optional[str] = None
) -> dict:
    """Executes scraping pipeline across pending cafes with exponential backoff, circuit breaking, and checkpointing."""
    shard_label = shard_name or Path(config.SOURCE_DB_PATH).stem
    total_to_process = len(pending_cafes)
    matched_count = 0
    not_found_count = 0
    error_count = 0
    total_photos = 0
    owner_photos = 0
    consecutive_rate_limits = 0
    backoff_delay = 60.0

    pbar = tqdm(total=total_to_process, desc="Harvesting Photos", unit="cafe")

    for idx, cafe in enumerate(pending_cafes, 1):
        retried = False
        while True:
            res = await scraper.scrape_cafe(cafe)
            status = res.get("status")

            if status == "rate_limited":
                consecutive_rate_limits += 1
                logger.warning(f"Rate limited on cafe {cafe.id}! Consecutive hit #{consecutive_rate_limits}")
                if consecutive_rate_limits >= 3:
                    logger.critical("Aborting batch safely: 3 consecutive CAPTCHA / rate-limit responses. Checkpoint saved.")
                    scraper.db.update_checkpoint(
                        shard_name=shard_label,
                        last_cafe_id=cafe.id,
                        last_cafe_name=cafe.name,
                        total_processed=idx,
                        matched_count=matched_count,
                        not_found_count=not_found_count,
                        error_count=error_count,
                        total_photos=total_photos,
                        status="rate_limited"
                    )
                    pbar.close()
                    return {
                        "matched": matched_count,
                        "not_found": not_found_count,
                        "errors": error_count,
                        "photos": total_photos,
                        "owner_photos": owner_photos,
                        "rate_limited": True
                    }

                logger.info(f"Backing off for {backoff_delay}s...")
                await asyncio.sleep(backoff_delay)
                backoff_delay = min(backoff_delay * 2.0, 600.0)
                await scraper._create_new_context()

                if not retried:
                    retried = True
                    continue  # Retry the current cafe once after backoff
                else:
                    error_count += 1
                    break
            else:
                consecutive_rate_limits = 0
                backoff_delay = 60.0

                if status == "matched":
                    matched_count += 1
                    photos = res.get("photos", [])
                    total_photos += len(photos)
                    owner_photos += sum(1 for p in photos if p.get("is_owner_photo") == 1)
                elif status == "not_found":
                    not_found_count += 1
                else:
                    error_count += 1
                break

        # Persist checkpoint after each venue
        scraper.db.update_checkpoint(
            shard_name=shard_label,
            last_cafe_id=cafe.id,
            last_cafe_name=cafe.name,
            total_processed=idx,
            matched_count=matched_count,
            not_found_count=not_found_count,
            error_count=error_count,
            total_photos=total_photos,
            status="in_progress"
        )

        pbar.update(1)
        pbar.set_postfix({
            "matched": matched_count,
            "not_found": not_found_count,
            "errors": error_count,
            "photos": total_photos,
            "owner": owner_photos
        })

        if idx < total_to_process:
            if idx % config.CAFES_PER_BATCH == 0:
                logger.info(f"Batch checkpoint reached ({idx}/{total_to_process}). Cooldown {config.BATCH_COOLDOWN_SECONDS}s...")
                await asyncio.sleep(config.BATCH_COOLDOWN_SECONDS)
            else:
                await scraper._safe_delay()

    pbar.close()

    # Finalize checkpoint status
    scraper.db.update_checkpoint(
        shard_name=shard_label,
        last_cafe_id=pending_cafes[-1].id if pending_cafes else 0,
        last_cafe_name=pending_cafes[-1].name if pending_cafes else "",
        total_processed=len(pending_cafes),
        matched_count=matched_count,
        not_found_count=not_found_count,
        error_count=error_count,
        total_photos=total_photos,
        status="completed"
    )

    return {
        "matched": matched_count,
        "not_found": not_found_count,
        "errors": error_count,
        "photos": total_photos,
        "owner_photos": owner_photos,
        "rate_limited": False
    }


async def main():
    default_source = "data/merged_cafe_photos_matched.db" if Path("data/merged_cafe_photos_matched.db").exists() else r"c:\Users\amira\Desktop\DB\v3\1405-08-12-cafochino.db"
    parser = argparse.ArgumentParser(description="Mapify: Google Maps Cafe Photo & Metadata Harvester")
    parser.add_argument("--limit", type=int, default=None, help="Number of pending cafes to process (default: all)")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode (default: True)")
    parser.add_argument("--headful", dest="headless", action="store_false", help="Run browser in visible UI mode")
    parser.add_argument("--batch-size", type=int, default=35, help="Number of cafes per batch before cooldown")
    parser.add_argument("--source-db", type=str, default=default_source, help="Path to source db (default: merged_cafe_photos_matched.db)")
    parser.add_argument("--output-db", type=str, default=None, help="Path to output cafe_photos.db")
    parser.add_argument("--delay-min", type=float, default=4.5, help="Minimum randomized delay per cafe (seconds)")
    parser.add_argument("--delay-max", type=float, default=8.5, help="Maximum randomized delay per cafe (seconds)")
    parser.add_argument("--shard-id", type=int, default=None, help="Shard index (1-based) for parallel multi-worker execution")
    parser.add_argument("--total-shards", type=int, default=None, help="Total number of parallel shards")
    parser.add_argument("--start-id", type=int, default=None, help="Filter cafes starting from this ID")
    parser.add_argument("--end-id", type=int, default=None, help="Filter cafes ending at this ID")

    args = parser.parse_args()

    project_root = Path(__file__).parent
    setup_logger(project_root / "logs")

    config = ScraperConfig(
        SOURCE_DB_PATH=args.source_db,
        OUTPUT_DB_PATH=args.output_db or str(project_root / "data" / "cafe_photos.db"),
        HEADLESS=args.headless,
        CAFES_PER_BATCH=args.batch_size,
        MIN_DELAY_PER_CAFE=args.delay_min,
        MAX_DELAY_PER_CAFE=args.delay_max
    )

    db = DatabaseManager(
        source_db_path=config.SOURCE_DB_PATH,
        output_db_path=config.OUTPUT_DB_PATH
    )

    pending_cafes = db.load_pending_cafes(
        limit=args.limit,
        shard_id=args.shard_id,
        total_shards=args.total_shards,
        start_id=args.start_id,
        end_id=args.end_id
    )
    total_to_process = len(pending_cafes)

    logger.info("=" * 60)
    logger.info("🚀 MAPIFY: GOOGLE MAPS CAFE PHOTO & EXTENDED METADATA HARVESTER")
    logger.info(f"Source Database: {config.SOURCE_DB_PATH}")
    logger.info(f"Output Database: {config.OUTPUT_DB_PATH}")
    if args.total_shards and args.shard_id:
        logger.info(f"Parallel Shard: {args.shard_id} of {args.total_shards}")
    logger.info(f"Target Cafes to Scrape: {total_to_process}")
    logger.info(f"Browser Mode: {'Headless' if config.HEADLESS else 'Visible'}")
    logger.info(f"Pacing: {config.MIN_DELAY_PER_CAFE}s - {config.MAX_DELAY_PER_CAFE}s per cafe")
    logger.info("=" * 60)

    if total_to_process == 0:
        logger.warning("No pending cafes found to process! (All cafes in database already scraped)")
        return

    scraper = MapifyScraper(config=config, db_manager=db)
    await scraper.start()

    try:
        stats = await run_pipeline(scraper=scraper, pending_cafes=pending_cafes, config=config)
    except KeyboardInterrupt:
        logger.warning("Process interrupted by user (Ctrl+C). Saving and shutting down gracefully...")
        stats = {}
    finally:
        await scraper.close()

    logger.info("=" * 60)
    logger.info("📊 SESSION SUMMARY")
    matched = stats.get("matched", 0)
    not_found = stats.get("not_found", 0)
    errors = stats.get("errors", 0)
    total_photos = stats.get("photos", 0)
    owner_photos = stats.get("owner_photos", 0)
    logger.info(f"Total Processed: {matched + not_found + errors}/{total_to_process}")
    logger.info(f"Successfully Matched: {matched}")
    logger.info(f"Not Found / Rejected: {not_found}")
    logger.info(f"Errors: {errors}")
    logger.info(f"Total High-Res Photos Harvested: {total_photos}")
    logger.info(f"Owner / Verified Photos: {owner_photos} ({round(owner_photos/max(1, total_photos)*100, 1)}%)")
    logger.info(f"Customer / UGC Photos: {total_photos - owner_photos}")
    if matched > 0:
        logger.info(f"Average Photos per Cafe: {total_photos / matched:.1f}")
    logger.info(f"Results stored in: {config.OUTPUT_DB_PATH}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
