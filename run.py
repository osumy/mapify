"""CLI runner for Mapify Google Maps Cafe Photo Scraper."""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

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


async def main():
    parser = argparse.ArgumentParser(description="Mapify: Google Maps Cafe Photo Scraper")
    parser.add_argument("--limit", type=int, default=None, help="Number of pending cafes to process (default: all)")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode (default: True)")
    parser.add_argument("--headful", dest="headless", action="store_false", help="Run browser in visible UI mode")
    parser.add_argument("--batch-size", type=int, default=35, help="Number of cafes per batch before cooldown")
    parser.add_argument("--source-db", type=str, default=r"c:\Users\amira\Desktop\DB\v3\1405-08-12-cafochino.db", help="Path to source cafochino.db")
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
    logger.info("🚀 MAPIFY: GOOGLE MAPS CAFE PHOTO SCRAPER")
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

    matched_count = 0
    not_found_count = 0
    total_photos_harvested = 0

    pbar = tqdm(total=total_to_process, desc="Harvesting Photos", unit="cafe")

    try:
        for idx, cafe in enumerate(pending_cafes, 1):
            res = await scraper.scrape_cafe(cafe)
            status = res.get("status")
            photos = res.get("photos", [])

            if status == "matched":
                matched_count += 1
                total_photos_harvested += len(photos)
            else:
                not_found_count += 1

            pbar.update(1)
            pbar.set_postfix({
                "matched": matched_count,
                "not_found": not_found_count,
                "photos": total_photos_harvested
            })

            # Check if batch cooldown is needed
            if idx < total_to_process:
                if idx % config.CAFES_PER_BATCH == 0:
                    logger.info(f"Batch checkpoint reached ({idx}/{total_to_process}). Cooling down for {config.BATCH_COOLDOWN_SECONDS}s...")
                    await asyncio.sleep(config.BATCH_COOLDOWN_SECONDS)
                else:
                    await scraper._safe_delay()

    except KeyboardInterrupt:
        logger.warning("Process interrupted by user (Ctrl+C). Saving and shutting down gracefully...")
    finally:
        pbar.close()
        await scraper.close()

    logger.info("=" * 60)
    logger.info("📊 SESSION SUMMARY")
    logger.info(f"Total Processed: {matched_count + not_found_count}/{total_to_process}")
    logger.info(f"Successfully Matched: {matched_count}")
    logger.info(f"Not Found / Rejected: {not_found_count}")
    logger.info(f"Total High-Res Photos Harvested: {total_photos_harvested}")
    if matched_count > 0:
        logger.info(f"Average Photos per Cafe: {total_photos_harvested / matched_count:.1f}")
    logger.info(f"Results stored in: {config.OUTPUT_DB_PATH}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
