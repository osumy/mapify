"""Utility script to shard matched cafes database for parallel multi-worker execution on Kaggle."""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

def split_database(
    source_db: str = "data/merged_cafe_photos_matched.db",
    output_dir: str = "data/shards",
    num_shards: int = 8
) -> list[dict]:
    src_path = Path(source_db)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if not src_path.exists():
        print(f"❌ Error: Source database not found at '{source_db}'")
        sys.exit(1)

    print("=" * 60)
    print("✂️  MAPIFY DATABASE SHARDER")
    print(f"Source: {src_path.resolve()}")
    print(f"Output Directory: {out_path.resolve()}")
    print(f"Target Shards: {num_shards}")
    print("=" * 60)

    conn_src = sqlite3.connect(src_path)
    cur_src = conn_src.cursor()

    # Detect table: places or cafes_cafe
    tables = {r[0] for r in cur_src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "places" in tables:
        rows = cur_src.execute("""
            SELECT * FROM places
            WHERE status = 'matched'
            ORDER BY cafe_id ASC
        """).fetchall()
        col_names = [desc[0] for desc in cur_src.description]
        id_col_idx = col_names.index("cafe_id")
        table_name = "places"
    elif "cafes_cafe" in tables:
        rows = cur_src.execute("""
            SELECT * FROM cafes_cafe
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY id ASC
        """).fetchall()
        col_names = [desc[0] for desc in cur_src.description]
        id_col_idx = col_names.index("id")
        table_name = "cafes_cafe"
    else:
        conn_src.close()
        raise ValueError(f"No compatible table found in {source_db}")

    total_records = len(rows)
    print(f"Total records to shard: {total_records} from '{table_name}' table")

    cols_str = ", ".join([f'"{c}"' for c in col_names])
    placeholders = ", ".join(["?"] * len(col_names))

    # Calculate shard sizes evenly
    base_size = total_records // num_shards
    remainder = total_records % num_shards

    manifest = []
    current_idx = 0

    for shard_idx in range(1, num_shards + 1):
        # Distribute remainder across first few shards
        shard_len = base_size + (1 if shard_idx <= remainder else 0)
        shard_rows = rows[current_idx:current_idx + shard_len]
        current_idx += shard_len

        min_id = shard_rows[0][id_col_idx]
        max_id = shard_rows[-1][id_col_idx]

        shard_filename = f"matched_shard_{shard_idx:02d}_of_{num_shards:02d}_{len(shard_rows)}cafes.db"
        shard_file_path = out_path / shard_filename

        if shard_file_path.exists():
            shard_file_path.unlink()

        conn_shard = sqlite3.connect(shard_file_path)
        cur_shard = conn_shard.cursor()

        # Create places table schema with full Level 1 columns
        cur_shard.execute("""
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

        cur_shard.execute("""
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

        cur_shard.execute("""
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

        cur_shard.execute("CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);")
        cur_shard.execute("CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);")

        # Insert shard records
        cur_shard.executemany(f'INSERT INTO places ({cols_str}) VALUES ({placeholders})', shard_rows)
        conn_shard.commit()
        conn_shard.close()

        size_kb = shard_file_path.stat().st_size / 1024
        print(f"✅ Shard {shard_idx}/{num_shards}: {shard_filename}")
        print(f"   Count: {len(shard_rows)} cafes | ID Range: {min_id} -> {max_id} | Size: {size_kb:.1f} KB")

        manifest.append({
            "shard": shard_idx,
            "filename": shard_filename,
            "path": str(shard_file_path),
            "cafes_count": len(shard_rows),
            "id_range": f"{min_id} - {max_id}",
            "size_kb": round(size_kb, 1)
        })

    conn_src.close()
    print("\n🎉 All shards created successfully!")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split database into parallel shards")
    parser.add_argument("--source", default="data/merged_cafe_photos_matched.db", help="Source SQLite DB path")
    parser.add_argument("--out-dir", default="data/shards", help="Output directory for shards")
    parser.add_argument("--shards", type=int, default=8, help="Number of shards to create (default: 8)")
    args = parser.parse_args()

    split_database(source_db=args.source, output_dir=args.out_dir, num_shards=args.shards)
