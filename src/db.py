"""Database layer for Mapify: reads source cafes and persists scraped photo data & extended metadata."""

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import time
from typing import Optional, List, Dict, Any, Set
from urllib.parse import unquote


@dataclass
class CafeRecord:
    id: int
    name: str
    latitude: float
    longitude: float
    city: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    instagram: Optional[str] = None
    website: Optional[str] = None
    google_maps_url: Optional[str] = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def extract_place_identifiers(url: str) -> Dict[str, Optional[str]]:
    """
    Extracts canonical Google Maps identifiers from URL:
    - place_id: Universal Google Place ID ('ChIJ...') computed offline from FID protobuf
    - cid: Customer ID decimal integer string
    - fid: Feature ID hex pair ('0x...:0x...')
    - kgmid: Knowledge Graph ID ('/g/...')
    """
    ids: Dict[str, Optional[str]] = {
        "place_id": None,
        "cid": None,
        "fid": None,
        "kgmid": None,
    }
    if not url:
        return ids

    # 1. Match hex FID: !1s(0x...:0x...)
    m_hex = re.search(r"1s(0x[0-9a-fA-F]+):(0x[0-9a-fA-F]+)", url)
    if m_hex:
        hex1, hex2 = m_hex.group(1), m_hex.group(2)
        ids["fid"] = f"{hex1}:{hex2}"
        try:
            val1 = int(hex1, 16)
            val2 = int(hex2, 16)
            ids["cid"] = str(val2)
            # Binary protobuf structure for Google Place ID
            payload = b'\n\x12\t' + struct.pack('<Q', val1) + b'\x11' + struct.pack('<Q', val2)
            ids["place_id"] = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
        except Exception:
            pass

    # 2. Match direct place_id param if present (e.g. ?place_id=... or ?q=place_id:...)
    m_pid = re.search(r"(?:[?&](?:query_place_id|place_id)=|place_id:)([a-zA-Z0-9_\-]+)", url)
    if m_pid and not ids["place_id"]:
        ids["place_id"] = m_pid.group(1)

    # 3. Match decimal cid param if present
    m_cid = re.search(r"[?&]cid=(\d+)", url)
    if m_cid and not ids["cid"]:
        ids["cid"] = m_cid.group(1)

    # 4. Match kgmid: 16s%2F... or /g/...
    m_kgmid = re.search(r"16s(?:%2F|/)([^?&!]+)", url)
    if m_kgmid:
        val = unquote(m_kgmid.group(1))
        ids["kgmid"] = val if val.startswith("/") else f"/{val}"

    return ids


class DatabaseManager:
    def __init__(self, source_db_path: str, output_db_path: str):
        self.source_db_path = source_db_path
        self.output_db_path = output_db_path
        self._init_output_db()

    def _get_connection(self, db_path: str, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            abs_path = os.path.abspath(db_path).replace("\\", "/")
            uri = f"file:{abs_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=60.0)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=60.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 60000;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_output_db(self) -> None:
        """Create output tables and indices for venues, metadata, and photos."""
        conn = self._get_connection(self.output_db_path)
        cur = conn.cursor()

        # Table for matched/scraped places with comprehensive Level 1 metadata
        cur.execute("""
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
            status TEXT,                -- 'matched', 'not_found', 'error'
            name_score REAL,
            distance_meters REAL,
            photos_count INTEGER DEFAULT 0,
            search_url TEXT,
            google_maps_url TEXT,
            scraped_at TEXT
        );
        """)

        # Table for extracted photo URLs with owner vs UGC classification
        cur.execute("""
        CREATE TABLE IF NOT EXISTS place_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cafe_id INTEGER NOT NULL,
            photo_url TEXT NOT NULL,
            high_res_url TEXT NOT NULL,
            source_type TEXT DEFAULT 'gallery',  -- 'cover', 'owner', 'gallery', 'dom', 'rpc', 'cdn_load'
            is_owner_photo INTEGER DEFAULT 0,    -- 1 = uploaded by business owner/cover, 0 = customer/UGC
            author_name TEXT,
            created_at TEXT,
            UNIQUE(cafe_id, photo_url),
            FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
        );
        """)

        # Migrations: ensure columns exist in existing output database
        places_cols = {c["name"] for c in cur.execute("PRAGMA table_info(places)").fetchall()}
        new_cols = {
            "cid": "TEXT",
            "fid": "TEXT",
            "kgmid": "TEXT",
            "category_name": "TEXT",
            "phone": "TEXT",
            "phone_unformatted": "TEXT",
            "website": "TEXT",
            "total_score": "REAL",
            "reviews_count": "INTEGER",
            "plus_code": "TEXT",
            "is_claimed": "INTEGER",
            "is_permanently_closed": "INTEGER DEFAULT 0",
            "is_temporarily_closed": "INTEGER DEFAULT 0",
            "price_level": "TEXT",
            "located_in": "TEXT",
            "menu_url": "TEXT",
            "opening_hours": "TEXT",
            "popular_times": "TEXT",
        }
        for col, col_type in new_cols.items():
            if col not in places_cols:
                cur.execute(f"ALTER TABLE places ADD COLUMN {col} {col_type};")

        photo_cols = {c["name"] for c in cur.execute("PRAGMA table_info(place_photos)").fetchall()}
        if "is_owner_photo" not in photo_cols:
            cur.execute("ALTER TABLE place_photos ADD COLUMN is_owner_photo INTEGER DEFAULT 0;")

        # Indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_cafe ON place_photos(cafe_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_owner ON place_photos(is_owner_photo);")

        # Checkpoint table for seamless auto-resume, status tracking, and monitoring
        cur.execute("""
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

        conn.commit()
        conn.close()

    def get_already_processed_ids(self) -> Set[int]:
        """Returns set of cafe IDs already resolved in output database."""
        if not os.path.exists(self.output_db_path):
            return set()
        conn = self._get_connection(self.output_db_path, read_only=True)
        cur = conn.cursor()

        t_check = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='places'").fetchone()
        if not t_check:
            conn.close()
            return set()

        cols = {c["name"] for c in cur.execute("PRAGMA table_info(places)").fetchall()}

        # If running in-place (source == output) and category_name exists, only count items where metadata was extracted
        if os.path.abspath(self.source_db_path) == os.path.abspath(self.output_db_path) and "category_name" in cols:
            rows = cur.execute("SELECT cafe_id FROM places WHERE category_name IS NOT NULL OR status = 'not_found'").fetchall()
        else:
            rows = cur.execute("SELECT cafe_id FROM places WHERE status IN ('matched', 'not_found')").fetchall()

        processed = {r["cafe_id"] for r in rows}
        conn.close()
        return processed

    def load_pending_cafes(
        self,
        limit: Optional[int] = None,
        shard_id: Optional[int] = None,
        total_shards: Optional[int] = None,
        start_id: Optional[int] = None,
        end_id: Optional[int] = None
    ) -> List[CafeRecord]:
        """
        Loads cafes from source database that haven't been scraped yet.
        Supports:
        1. Pre-matched database format (table 'places' with canonical 'google_maps_url')
        2. Original source database format (table 'cafes_cafe')
        """
        processed_ids = self.get_already_processed_ids()
        conn = self._get_connection(self.source_db_path, read_only=True)
        cur = conn.cursor()

        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        pending = []
        if "places" in tables:
            query = """
            SELECT cafe_id AS id, COALESCE(title, searched_name) AS name,
                   COALESCE(scraped_lat, searched_lat) AS latitude,
                   COALESCE(scraped_lng, searched_lng) AS longitude,
                   address, google_maps_url
            FROM places
            WHERE (status = 'matched' OR status IS NULL)
            ORDER BY cafe_id ASC
            """
            rows = cur.execute(query).fetchall()
            conn.close()

            for r in rows:
                cid = r["id"]
                if cid in processed_ids:
                    continue

                if start_id is not None and cid < start_id:
                    continue
                if end_id is not None and cid > end_id:
                    continue

                if total_shards and shard_id:
                    if (cid % total_shards) != (shard_id - 1):
                        continue

                pending.append(CafeRecord(
                    id=cid,
                    name=r["name"],
                    latitude=float(r["latitude"]) if r["latitude"] is not None else 0.0,
                    longitude=float(r["longitude"]) if r["longitude"] is not None else 0.0,
                    address=r["address"],
                    google_maps_url=r["google_maps_url"]
                ))

                if limit and len(pending) >= limit:
                    break

            return pending

        elif "cafes_cafe" in tables:
            query = """
            SELECT id, name, latitude, longitude, city, address, phone, instagram, website
            FROM cafes_cafe
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY id ASC
            """
            rows = cur.execute(query).fetchall()
            conn.close()

            for r in rows:
                cid = r["id"]
                if cid in processed_ids:
                    continue

                if start_id is not None and cid < start_id:
                    continue
                if end_id is not None and cid > end_id:
                    continue

                if total_shards and shard_id:
                    if (cid % total_shards) != (shard_id - 1):
                        continue

                pending.append(CafeRecord(
                    id=cid,
                    name=r["name"],
                    latitude=float(r["latitude"]),
                    longitude=float(r["longitude"]),
                    city=r["city"],
                    address=r["address"],
                    phone=r["phone"],
                    instagram=r["instagram"],
                    website=r["website"]
                ))

                if limit and len(pending) >= limit:
                    break

            return pending
        else:
            conn.close()
            raise ValueError(f"Source database at '{self.source_db_path}' contains neither 'places' nor 'cafes_cafe' table.")

    def save_place_and_photos(
        self,
        cafe: CafeRecord,
        status: str,
        title: Optional[str] = None,
        category_name: Optional[str] = None,
        place_id: Optional[str] = None,
        cid: Optional[str] = None,
        fid: Optional[str] = None,
        kgmid: Optional[str] = None,
        scraped_lat: Optional[float] = None,
        scraped_lng: Optional[float] = None,
        address: Optional[str] = None,
        phone: Optional[str] = None,
        phone_unformatted: Optional[str] = None,
        website: Optional[str] = None,
        total_score: Optional[float] = None,
        reviews_count: Optional[int] = None,
        plus_code: Optional[str] = None,
        is_claimed: Optional[bool] = None,
        is_permanently_closed: bool = False,
        is_temporarily_closed: bool = False,
        price_level: Optional[str] = None,
        located_in: Optional[str] = None,
        menu_url: Optional[str] = None,
        opening_hours: Optional[Any] = None,
        popular_times: Optional[Any] = None,
        name_score: float = 0.0,
        distance_meters: Optional[float] = None,
        search_url: str = "",
        google_maps_url: Optional[str] = None,
        photos: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Atomically saves a place record with extended Level 1 metadata and all associated photos."""
        photos = photos or []

        # Auto-extract identifiers from google_maps_url if not explicitly provided
        target_url = google_maps_url or cafe.google_maps_url
        if target_url and not place_id:
            extracted = extract_place_identifiers(target_url)
            place_id = place_id or extracted.get("place_id")
            cid = cid or extracted.get("cid")
            fid = fid or extracted.get("fid")
            kgmid = kgmid or extracted.get("kgmid")

        if opening_hours is not None and not isinstance(opening_hours, str):
            opening_hours = json.dumps(opening_hours, ensure_ascii=False)
        if popular_times is not None and not isinstance(popular_times, str):
            popular_times = json.dumps(popular_times, ensure_ascii=False)

        is_claimed_int = int(is_claimed) if is_claimed is not None else None

        conn = self._get_connection(self.output_db_path)
        cur = conn.cursor()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            cur.execute("""
            INSERT INTO places (
                cafe_id, place_id, cid, fid, kgmid, title, category_name,
                searched_name, searched_lat, searched_lng, scraped_lat, scraped_lng,
                address, phone, phone_unformatted, website,
                total_score, reviews_count, plus_code, is_claimed,
                is_permanently_closed, is_temporarily_closed, price_level,
                located_in, menu_url, opening_hours, popular_times,
                status, name_score, distance_meters, photos_count,
                search_url, google_maps_url, scraped_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(cafe_id) DO UPDATE SET
                place_id = COALESCE(excluded.place_id, places.place_id),
                cid = COALESCE(excluded.cid, places.cid),
                fid = COALESCE(excluded.fid, places.fid),
                kgmid = COALESCE(excluded.kgmid, places.kgmid),
                title = CASE WHEN excluded.status = 'matched' THEN excluded.title ELSE places.title END,
                category_name = COALESCE(excluded.category_name, places.category_name),
                scraped_lat = COALESCE(excluded.scraped_lat, places.scraped_lat),
                scraped_lng = COALESCE(excluded.scraped_lng, places.scraped_lng),
                address = COALESCE(excluded.address, places.address),
                phone = COALESCE(excluded.phone, places.phone),
                phone_unformatted = COALESCE(excluded.phone_unformatted, places.phone_unformatted),
                website = COALESCE(excluded.website, places.website),
                total_score = COALESCE(excluded.total_score, places.total_score),
                reviews_count = COALESCE(excluded.reviews_count, places.reviews_count),
                plus_code = COALESCE(excluded.plus_code, places.plus_code),
                is_claimed = COALESCE(excluded.is_claimed, places.is_claimed),
                is_permanently_closed = COALESCE(excluded.is_permanently_closed, places.is_permanently_closed),
                is_temporarily_closed = COALESCE(excluded.is_temporarily_closed, places.is_temporarily_closed),
                price_level = COALESCE(excluded.price_level, places.price_level),
                located_in = COALESCE(excluded.located_in, places.located_in),
                menu_url = COALESCE(excluded.menu_url, places.menu_url),
                opening_hours = COALESCE(excluded.opening_hours, places.opening_hours),
                popular_times = COALESCE(excluded.popular_times, places.popular_times),
                status = CASE WHEN places.status = 'matched' THEN 'matched' ELSE excluded.status END,
                name_score = CASE WHEN excluded.status = 'matched' THEN excluded.name_score ELSE places.name_score END,
                distance_meters = CASE WHEN excluded.status = 'matched' THEN excluded.distance_meters ELSE places.distance_meters END,
                photos_count = MAX(places.photos_count, excluded.photos_count),
                google_maps_url = CASE WHEN excluded.status = 'matched' THEN excluded.google_maps_url ELSE places.google_maps_url END,
                scraped_at = excluded.scraped_at;
            """, (
                cafe.id, place_id, cid, fid, kgmid, title or cafe.name, category_name,
                cafe.name, cafe.latitude, cafe.longitude, scraped_lat, scraped_lng,
                address or cafe.address, phone or cafe.phone, phone_unformatted, website or cafe.website,
                total_score, reviews_count, plus_code, is_claimed_int,
                int(is_permanently_closed), int(is_temporarily_closed), price_level,
                located_in, menu_url, opening_hours, popular_times,
                status, round(name_score, 2),
                round(distance_meters, 1) if distance_meters is not None else None,
                len(photos), search_url, target_url, now_iso
            ))

            for p in photos:
                cur.execute("""
                INSERT INTO place_photos (
                    cafe_id, photo_url, high_res_url, source_type, is_owner_photo, author_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cafe_id, photo_url) DO UPDATE SET
                    is_owner_photo = MAX(place_photos.is_owner_photo, excluded.is_owner_photo),
                    source_type = CASE WHEN excluded.is_owner_photo = 1 THEN excluded.source_type ELSE place_photos.source_type END;
                """, (
                    cafe.id,
                    p["photo_url"],
                    p.get("high_res_url", p["photo_url"]),
                    p.get("source_type", "gallery"),
                    int(p.get("is_owner_photo", 0)),
                    p.get("author_name"),
                    now_iso
                ))

            conn.commit()
        finally:
            conn.close()

    def update_checkpoint(
        self,
        shard_name: str,
        last_cafe_id: int,
        last_cafe_name: str,
        total_processed: int,
        matched_count: int,
        not_found_count: int,
        error_count: int,
        total_photos: int,
        status: str = "in_progress"
    ) -> None:
        """Atomically persists latest progress checkpoint for auto-resume and monitoring."""
        conn = self._get_connection(self.output_db_path)
        cur = conn.cursor()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            cur.execute("""
            INSERT INTO checkpoints (
                id, shard_name, last_cafe_id, last_cafe_name, total_processed,
                matched_count, not_found_count, error_count, total_photos, status, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                shard_name = excluded.shard_name,
                last_cafe_id = excluded.last_cafe_id,
                last_cafe_name = excluded.last_cafe_name,
                total_processed = excluded.total_processed,
                matched_count = excluded.matched_count,
                not_found_count = excluded.not_found_count,
                error_count = excluded.error_count,
                total_photos = excluded.total_photos,
                status = excluded.status,
                updated_at = excluded.updated_at;
            """, (
                shard_name, last_cafe_id, last_cafe_name, total_processed,
                matched_count, not_found_count, error_count, total_photos, status, now_iso
            ))
            conn.commit()
        finally:
            conn.close()

    def get_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Retrieves current checkpoint status if present."""
        if not os.path.exists(self.output_db_path):
            return None
        conn = self._get_connection(self.output_db_path, read_only=True)
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM checkpoints WHERE id = 1").fetchone()
        conn.close()
        return dict(row) if row else None
