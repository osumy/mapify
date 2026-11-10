"""Script to monitor Kaggle kernels, download completed shard databases, and merge into master."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

KAGGLE_USERNAME = "amiraliaraghi"
API_TOKEN = "KGAT_836740cc96f6d44b36bc678c98af8889"
PYTHON_BIN = sys.executable

def set_kaggle_env():
    os.environ["KAGGLE_API_TOKEN"] = API_TOKEN
    os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME

def run_cmd(cmd_list: list[str]) -> tuple[int, str]:
    set_kaggle_env()
    proc = subprocess.run(cmd_list, capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout + "\n" + proc.stderr

def check_status(kernel_id: str) -> str:
    code, out = run_cmd([PYTHON_BIN, "-m", "kaggle", "kernels", "status", kernel_id])
    for line in out.splitlines():
        if "has status" in line:
            return line.split("has status")[-1].strip().strip('"')
    return out.strip()

def download_output(kernel_id: str, dest_dir: Path) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)
    code, out = run_cmd([PYTHON_BIN, "-m", "kaggle", "kernels", "output", kernel_id, "-p", str(dest_dir)])
    print(out.strip())
    # Check if cafe_photos.db was downloaded
    db_file = dest_dir / "cafe_photos.db"
    return db_file.exists()

def main():
    parser = argparse.ArgumentParser(description="Monitor and download Mapify metadata shards")
    parser.add_argument("--action", choices=["status", "download", "push_remaining"], default="status")
    args = parser.parse_args()

    project_root = Path(__file__).parent
    output_base = project_root / "kaggle_output_shards"
    output_base.mkdir(parents=True, exist_ok=True)

    total_shards = 8
    statuses = {}

    print("=" * 60)
    print("📊 KAGGLE SHARDS STATUS")
    print("=" * 60)

    for i in range(1, total_shards + 1):
        kid = f"{KAGGLE_USERNAME}/mapify-meta-shard{i}"
        st = check_status(kid)
        statuses[i] = st
        print(f"  Shard {i} ({kid}): {st}")

    if args.action == "push_remaining":
        # Check if slots are available (less than 5 running)
        running_count = sum(1 for s in statuses.values() if "RUNNING" in s or "QUEUED" in s)
        print(f"\nCurrently running/queued CPU kernels: {running_count}/5")
        if running_count < 5:
            for i in range(6, total_shards + 1):
                if statuses[i] not in ("KernelWorkerStatus.RUNNING", "KernelWorkerStatus.COMPLETE"):
                    print(f"Slot available! Launching Shard {i}...")
                    subprocess.run([PYTHON_BIN, "kaggle_staging/kaggle_manager.py", "--shard", str(i), "--action", "push_one"])
                    running_count += 1
                    if running_count >= 5:
                        break

    elif args.action == "download":
        downloaded_dbs = []
        for i in range(1, total_shards + 1):
            renamed_db = output_base / f"scraped_meta_shard_{i}.db"
            if renamed_db.exists() and renamed_db.stat().st_size > 100000:
                print(f"⏩ Shard {i} already downloaded locally: {renamed_db}")
                downloaded_dbs.append(str(renamed_db))
                continue

            kid = f"{KAGGLE_USERNAME}/mapify-meta-shard{i}"
            st = statuses.get(i, "")
            if "COMPLETE" in st:
                target_dir = output_base / f"shard_{i}"
                print(f"Downloading completed output for Shard {i} to {target_dir}...")
                success = download_output(kid, target_dir)
                if success:
                    # Rename to distinct shard db
                    src_db = target_dir / "cafe_photos.db"
                    import shutil
                    shutil.copy2(src_db, renamed_db)
                    downloaded_dbs.append(str(renamed_db))
                    print(f"✅ Shard {i} ready: {renamed_db}")
            else:
                print(f"⏳ Shard {i} is not complete yet (Status: {st})")

        if downloaded_dbs:
            print("\nMerging available shards into data/cafe_photos_master.db...")
            from merge_shards import merge_databases
            merge_databases(downloaded_dbs, "data/cafe_photos_master.db")

if __name__ == "__main__":
    main()
