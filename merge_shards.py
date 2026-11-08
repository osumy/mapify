"""Utility script to merge multiple scraped shard databases into a single master database."""

import argparse
import glob
from pathlib import Path
import sqlite3

def merge_databases(shard_db_paths: list[str], output_master_path: str):
    print("=" * 60)
    print(f"🔄 MERGING {len(shard_db_paths)} SHARD DATABASES INTO MASTER")
    print(f"Master Output: {output_master_path}")
    print("=" * 60)

    # Initialize master database
    conn_master = sqlite3.connect(output_master_path)
    cur_master = conn_master.cursor()

    cur_master.execute("""
    CREATE TABLE IF NOT EXISTS places (
        cafe_id INTEGER PRIMARY KEY,
        place_id TEXT,
        cid TEXT,
        fid TEXT,
        kgmid TEXT,
        title TEXT,
        searched_name TEXT,
        searched_lat REAL,
        searched_lng REAL,
        scraped_lat REAL,
        scraped_lng REAL,
        address TEXT,
        status TEXT,
        name_score REAL,
        distance_meters REAL,
        photos_count INTEGER DEFAULT 0,
        search_url TEXT,
        google_maps_url TEXT,
        scraped_at TEXT
    );
    """)

    cur_master.execute("""
    CREATE TABLE IF NOT EXISTS place_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cafe_id INTEGER NOT NULL,
        photo_url TEXT NOT NULL,
        high_res_url TEXT NOT NULL,
        source_type TEXT DEFAULT 'gallery',
        author_name TEXT,
        created_at TEXT,
        UNIQUE(cafe_id, photo_url),
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    );
    """)

    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);")
    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);")
    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_photos_cafe ON place_photos(cafe_id);")

    total_places_merged = 0
    total_photos_merged = 0

    for idx, path in enumerate(shard_db_paths, 1):
        p = Path(path)
        if not p.exists():
            print(f"⚠️ Shard not found: {path}")
            continue

        conn_shard = sqlite3.connect(path)
        cur_shard = conn_shard.cursor()

        # Check if tables exist
        t_check = cur_shard.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='places'").fetchone()
        if not t_check:
            conn_shard.close()
            continue

        # Merge places
        places = cur_shard.execute("""
        SELECT cafe_id, place_id, cid, fid, kgmid, title, searched_name, searched_lat, searched_lng,
               scraped_lat, scraped_lng, address, status, name_score, distance_meters,
               photos_count, search_url, google_maps_url, scraped_at
        FROM places
        """).fetchall()

        cur_master.executemany("""
        INSERT OR REPLACE INTO places (
            cafe_id, place_id, cid, fid, kgmid, title, searched_name, searched_lat, searched_lng,
            scraped_lat, scraped_lng, address, status, name_score, distance_meters,
            photos_count, search_url, google_maps_url, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, places)

        # Merge place_photos
        photos = cur_shard.execute("""
        SELECT cafe_id, photo_url, high_res_url, source_type, author_name, created_at
        FROM place_photos
        """).fetchall()

        cur_master.executemany("""
        INSERT OR IGNORE INTO place_photos (
            cafe_id, photo_url, high_res_url, source_type, author_name, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """, photos)

        conn_master.commit()
        conn_shard.close()

        total_places_merged += len(places)
        total_photos_merged += len(photos)
        print(f"  [{idx}/{len(shard_db_paths)}] Merged '{p.name}': {len(places)} places, {len(photos)} photos")

    final_places = cur_master.execute("SELECT count(*) FROM places").fetchone()[0]
    final_photos = cur_master.execute("SELECT count(*) FROM place_photos").fetchone()[0]
    conn_master.close()

    print("=" * 60)
    print("🎉 MERGE COMPLETED SUCCESSFULLY!")
    print(f"Total Unique Places in Master: {final_places}")
    print(f"Total Unique Photos in Master: {final_photos}")
    print(f"Master File: {output_master_path}")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge scraped shard databases into master DB")
    parser.add_argument("shards", nargs="*", help="Paths or glob pattern to shard DBs")
    parser.add_argument("--output", default="data/cafe_photos_master.db", help="Path to output master DB")
    args = parser.parse_args()

    files = []
    if args.shards:
        for item in args.shards:
            expanded = glob.glob(item)
            files.extend(expanded if expanded else [item])
    else:
        files = glob.glob("data/*shard*.db")

    if not files:
        print("No shard databases found. Usage: python merge_shards.py path/to/shard1.db path/to/shard2.db --output master.db")
    else:
        merge_databases(sorted(files), args.output)
