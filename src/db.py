"""Database layer for Mapify: reads source cafes and persists scraped photo data."""

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import struct
import time
from typing import Optional, List, Dict, Any
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

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def extract_place_identifiers(url: str) -> Dict[str, Optional[str]]:
    """
    Extracts canonical Google Maps identifiers from URL:
    - place_id: Universal Google Place ID ('ChIJ...')
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

    # 2. Match direct place_id query param if present
    m_pid = re.search(r"[?&](?:query_place_id|place_id)=([a-zA-Z0-9_\-]+)", url)
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
        conn.row_factory = sqlite3.Row
        return conn

    def _init_output_db(self) -> None:
        """Create output tables and indices for venues and photos."""
        conn = self._get_connection(self.output_db_path)
        cur = conn.cursor()

        # Table for matched/scraped places
        cur.execute("""
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
            status TEXT,                -- 'matched', 'not_found', 'error'
            name_score REAL,
            distance_meters REAL,
            photos_count INTEGER DEFAULT 0,
            search_url TEXT,
            google_maps_url TEXT,
            scraped_at TEXT
        );
        """)

        # Migration: ensure cid, fid, kgmid columns exist if upgrading existing db
        existing_cols = {c["name"] for c in cur.execute("PRAGMA table_info(places)").fetchall()}
        for col in ["cid", "fid", "kgmid"]:
            if col not in existing_cols:
                cur.execute(f"ALTER TABLE places ADD COLUMN {col} TEXT;")

        # Table for extracted photo URLs
        cur.execute("""
        CREATE TABLE IF NOT EXISTS place_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cafe_id INTEGER NOT NULL,
            photo_url TEXT NOT NULL,
            high_res_url TEXT NOT NULL,
            source_type TEXT DEFAULT 'gallery',  -- 'request', 'rpc', 'dom'
            author_name TEXT,
            created_at TEXT,
            UNIQUE(cafe_id, photo_url),
            FOREIGN KEY (cafe_id) REFERENCES places(cafe_id)
        );
        """)

        # Indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_cafe ON place_photos(cafe_id);")

        conn.commit()
        conn.close()

    def get_already_processed_ids(self) -> set[int]:
        """Returns set of cafe IDs already processed (either matched or not_found)."""
        conn = self._get_connection(self.output_db_path, read_only=True)
        cur = conn.cursor()
        rows = cur.execute("SELECT cafe_id FROM places").fetchall()
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
        Supports sharding across multiple workers/notebooks.
        """
        processed_ids = self.get_already_processed_ids()
        conn = self._get_connection(self.source_db_path, read_only=True)
        cur = conn.cursor()

        query = """
        SELECT id, name, latitude, longitude, city, address, phone, instagram, website
        FROM cafes_cafe
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY id ASC
        """
        rows = cur.execute(query).fetchall()
        conn.close()

        pending = []
        for r in rows:
            cid = r["id"]
            if cid in processed_ids:
                continue

            # ID Range filter
            if start_id is not None and cid < start_id:
                continue
            if end_id is not None and cid > end_id:
                continue

            # Sharding filter (e.g. shard 1 of 4: cid % 4 == 0)
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

    def save_place_and_photos(
        self,
        cafe: CafeRecord,
        status: str,
        title: Optional[str] = None,
        place_id: Optional[str] = None,
        cid: Optional[str] = None,
        fid: Optional[str] = None,
        kgmid: Optional[str] = None,
        scraped_lat: Optional[float] = None,
        scraped_lng: Optional[float] = None,
        address: Optional[str] = None,
        name_score: float = 0.0,
        distance_meters: Optional[float] = None,
        search_url: str = "",
        google_maps_url: Optional[str] = None,
        photos: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Atomically saves a place record and all its associated photos with complete Google IDs."""
        photos = photos or []

        # Auto-extract identifiers from google_maps_url if not explicitly provided
        if google_maps_url and not place_id:
            extracted = extract_place_identifiers(google_maps_url)
            place_id = place_id or extracted.get("place_id")
            cid = cid or extracted.get("cid")
            fid = fid or extracted.get("fid")
            kgmid = kgmid or extracted.get("kgmid")

        conn = self._get_connection(self.output_db_path)
        cur = conn.cursor()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        cur.execute("""
        INSERT OR REPLACE INTO places (
            cafe_id, place_id, cid, fid, kgmid, title, searched_name, searched_lat, searched_lng,
            scraped_lat, scraped_lng, address, status, name_score, distance_meters,
            photos_count, search_url, google_maps_url, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cafe.id, place_id, cid, fid, kgmid, title or cafe.name, cafe.name, cafe.latitude, cafe.longitude,
            scraped_lat, scraped_lng, address, status, round(name_score, 2),
            round(distance_meters, 1) if distance_meters is not None else None,
            len(photos), search_url, google_maps_url, now_iso
        ))

        for p in photos:
            cur.execute("""
            INSERT OR IGNORE INTO place_photos (
                cafe_id, photo_url, high_res_url, source_type, author_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cafe.id,
                p["photo_url"],
                p.get("high_res_url", p["photo_url"]),
                p.get("source_type", "gallery"),
                p.get("author_name"),
                now_iso
            ))

        conn.commit()
        conn.close()
