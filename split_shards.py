import os
from pathlib import Path
import sqlite3

def split_database():
    source_db = r"c:\Users\amira\Desktop\DB\v3\1405-08-12-cafochino.db"
    shards_dir = Path(r"c:\Users\amira\Desktop\DB\v3\shards")
    shards_dir.mkdir(parents=True, exist_ok=True)

    print(f"Opening source database: {source_db}")
    conn_src = sqlite3.connect(source_db)
    cur_src = conn_src.cursor()

    # Get schema for cafes_cafe
    table_sql = cur_src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cafes_cafe'"
    ).fetchone()[0]

    # Get index statements for cafes_cafe
    indices = cur_src.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='cafes_cafe' AND sql IS NOT NULL"
    ).fetchall()

    # Fetch all cafes with valid coordinates, ordered by id
    rows = cur_src.execute("""
        SELECT * FROM cafes_cafe
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY id ASC
    """).fetchall()

    # Get column names
    col_names = [desc[0] for desc in cur_src.description]
    placeholders = ", ".join(["?"] * len(col_names))
    cols_str = ", ".join([f'"{c}"' for c in col_names])

    total_cafes = len(rows)
    print(f"Total valid cafes to shard: {total_cafes}")

    shard_size = 1000
    total_shards = (total_cafes + shard_size - 1) // shard_size
    print(f"Creating {total_shards} shards of up to {shard_size} cafes each in: {shards_dir}\n")

    shard_manifest = []

    for shard_idx in range(1, total_shards + 1):
        start_idx = (shard_idx - 1) * shard_size
        end_idx = min(start_idx + shard_size, total_cafes)
        shard_rows = rows[start_idx:end_idx]

        min_id = shard_rows[0][col_names.index("id")]
        max_id = shard_rows[-1][col_names.index("id")]
        shard_filename = f"cafochino_shard_{shard_idx:02d}_of_{total_shards:02d}_{len(shard_rows)}cafes.db"
        shard_path = shards_dir / shard_filename

        # Remove old if exists
        if shard_path.exists():
            shard_path.unlink()

        conn_shard = sqlite3.connect(shard_path)
        cur_shard = conn_shard.cursor()

        # Create table & indices
        cur_shard.execute(table_sql)
        for idx_sql in indices:
            try:
                cur_shard.execute(idx_sql[0])
            except Exception:
                pass

        # Insert rows
        insert_query = f'INSERT INTO cafes_cafe ({cols_str}) VALUES ({placeholders})'
        cur_shard.executemany(insert_query, shard_rows)

        conn_shard.commit()
        conn_shard.close()

        file_size_kb = shard_path.stat().st_size / 1024
        print(f"✅ Shard {shard_idx}/{total_shards}: {shard_filename}")
        print(f"   Count: {len(shard_rows)} cafes | ID Range: {min_id} -> {max_id} | Size: {file_size_kb:.1f} KB")

        shard_manifest.append({
            "shard": shard_idx,
            "filename": shard_filename,
            "cafes_count": len(shard_rows),
            "id_range": f"{min_id} - {max_id}",
            "size_kb": round(file_size_kb, 1)
        })

    conn_src.close()
    print("\n🎉 All shards created successfully!")
    return shard_manifest

if __name__ == "__main__":
    split_database()
