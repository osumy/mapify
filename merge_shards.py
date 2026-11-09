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
        category_name TEXT,
        searched_name TEXT,
        searched_lat REAL,
        searched_lng REAL,
        scraped_lat REAL,
        scraped_lng REAL,
        address TEXT,
        phone TEXT,
        phone_unformatted TEXT,
        website TEXT,
        total_score REAL,
        reviews_count INTEGER,
        plus_code TEXT,
        is_claimed INTEGER,
        is_permanently_closed INTEGER DEFAULT 0,
        is_temporarily_closed INTEGER DEFAULT 0,
        price_level TEXT,
        located_in TEXT,
        menu_url TEXT,
        opening_hours TEXT,
        popular_times TEXT,
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
        is_owner_photo INTEGER DEFAULT 0,
        author_name TEXT,
        created_at TEXT,
        UNIQUE(cafe_id, photo_url),
        FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
    );
    """)

    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);")
    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);")
    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_photos_cafe ON place_photos(cafe_id);")
    cur_master.execute("CREATE INDEX IF NOT EXISTS idx_photos_owner ON place_photos(is_owner_photo);")

    cur_master.execute("""
    CREATE TABLE IF NOT EXISTS checkpoints (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        shard_name TEXT,
        last_cafe_id INTEGER,
        last_cafe_name TEXT,
        total_processed INTEGER DEFAULT 0,
        matched_count INTEGER DEFAULT 0,
        not_found_count INTEGER DEFAULT 0,
        error_count INTEGER DEFAULT 0,
        total_photos INTEGER DEFAULT 0,
        status TEXT DEFAULT 'in_progress',
        updated_at TEXT
    );
    """)

    # Ensure master has all columns
    master_cols = {c[1] for c in cur_master.execute("PRAGMA table_info(places)").fetchall()}
    all_needed_cols = {
        "cid": "TEXT", "fid": "TEXT", "kgmid": "TEXT", "category_name": "TEXT",
        "phone": "TEXT", "phone_unformatted": "TEXT", "website": "TEXT",
        "total_score": "REAL", "reviews_count": "INTEGER", "plus_code": "TEXT",
        "is_claimed": "INTEGER", "is_permanently_closed": "INTEGER DEFAULT 0",
        "is_temporarily_closed": "INTEGER DEFAULT 0", "price_level": "TEXT",
        "located_in": "TEXT", "menu_url": "TEXT", "opening_hours": "TEXT",
        "popular_times": "TEXT"
    }
    for col, col_t in all_needed_cols.items():
        if col not in master_cols:
            cur_master.execute(f"ALTER TABLE places ADD COLUMN {col} {col_t};")

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

        shard_cols = [c[1] for c in cur_shard.execute("PRAGMA table_info(places)").fetchall()]
        cols_common = [c for c in shard_cols if c in master_cols or c in all_needed_cols]

        cols_str = ", ".join(cols_common)
        placeholders = ", ".join(["?"] * len(cols_common))

        rows = cur_shard.execute(f"SELECT {cols_str} FROM places").fetchall()

        # Build ON CONFLICT clause dynamically for common columns
        update_clauses = []
        for c in cols_common:
            if c == "cafe_id":
                continue
            if c == "photos_count":
                update_clauses.append("photos_count = MAX(places.photos_count, excluded.photos_count)")
            elif c == "status":
                update_clauses.append("status = CASE WHEN places.status = 'matched' THEN 'matched' ELSE excluded.status END")
            elif c == "title":
                update_clauses.append("title = CASE WHEN excluded.status = 'matched' THEN excluded.title ELSE places.title END")
            else:
                update_clauses.append(f"{c} = COALESCE(excluded.{c}, places.{c})")

        conflict_sql = f"ON CONFLICT(cafe_id) DO UPDATE SET {', '.join(update_clauses)}"

        cur_master.executemany(f"""
        INSERT INTO places ({cols_str}) VALUES ({placeholders})
        {conflict_sql};
        """, rows)

        # Merge place_photos if table exists
        photo_t_check = cur_shard.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='place_photos'").fetchone()
        if photo_t_check:
            photo_cols = {c[1] for c in cur_shard.execute("PRAGMA table_info(place_photos)").fetchall()}
            has_owner = "is_owner_photo" in photo_cols

            if has_owner:
                photos = cur_shard.execute("""
                SELECT cafe_id, photo_url, high_res_url, source_type, is_owner_photo, author_name, created_at
                FROM place_photos
                """).fetchall()

                cur_master.executemany("""
                INSERT INTO place_photos (
                    cafe_id, photo_url, high_res_url, source_type, is_owner_photo, author_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cafe_id, photo_url) DO UPDATE SET
                    is_owner_photo = MAX(place_photos.is_owner_photo, excluded.is_owner_photo);
                """, photos)
            else:
                photos = cur_shard.execute("""
                SELECT cafe_id, photo_url, high_res_url, source_type, author_name, created_at
                FROM place_photos
                """).fetchall()

                cur_master.executemany("""
                INSERT OR IGNORE INTO place_photos (
                    cafe_id, photo_url, high_res_url, source_type, author_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """, photos)

            total_photos_merged += len(photos)

        conn_master.commit()
        conn_shard.close()
        total_places_merged += len(rows)
        print(f"  [{idx}/{len(shard_db_paths)}] Merged '{p.name}': {len(rows)} places")

    final_places = cur_master.execute("SELECT count(*) FROM places").fetchone()[0]
    final_matched = cur_master.execute("SELECT count(*) FROM places WHERE status = 'matched'").fetchone()[0]
    final_with_meta = cur_master.execute("SELECT count(*) FROM places WHERE category_name IS NOT NULL OR total_score IS NOT NULL").fetchone()[0]
    final_photos = cur_master.execute("SELECT count(*) FROM place_photos").fetchone()[0]
    final_owner = cur_master.execute("SELECT count(*) FROM place_photos WHERE is_owner_photo = 1").fetchone()[0]
    conn_master.close()

    print("=" * 60)
    print("🎉 MERGE COMPLETED SUCCESSFULLY!")
    print(f"Total Unique Places in Master: {final_places} ({final_matched} matched, {final_with_meta} with Level 1 metadata)")
    print(f"Total Unique Photos in Master: {final_photos}")
    print(f"Total Owner / Verified Photos: {final_owner} ({round(final_owner/max(1, final_photos)*100, 1)}%)")
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
