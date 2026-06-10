#!/usr/bin/env python3
"""Smoke test WebDAV folder creation compatibility paths."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(ROOT / "test.sqlite3")
os.environ["BOT_TOKEN"] = "123:test"
os.environ["ADMIN_IDS"] = "1"
os.environ["UPLOAD_CACHE_DIR"] = str(ROOT / "cache")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite
from fastapi.testclient import TestClient
from database import init_db
from webui import app


async def rows():
    db = await aiosqlite.connect(str(ROOT / "test.sqlite3"))
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute("SELECT name, path, parent_path FROM dirs ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


def main():
    asyncio.run(init_db())
    client = TestClient(app)

    r = client.request("MKCOL", "/dav/mkcol-dir")
    assert r.status_code == 201, (r.status_code, r.text[:200])

    # Some clients use empty PUT with trailing slash as a folder-create fallback.
    r = client.put("/dav/put-dir/", content=b"")
    assert r.status_code == 201, (r.status_code, r.text[:200])

    # gitdisk-compatible fallback: some clients PROPFIND a missing folder target
    # before MKCOL, then abort on 404. Likely folder paths are lazily created.
    r = client.request("PROPFIND", "/dav/lazy-dir", headers={"Depth": "0"})
    assert r.status_code == 207, (r.status_code, r.text[:200])

    # Obvious file paths must not be auto-created as folders.
    r = client.request("PROPFIND", "/dav/not-a-folder.txt", headers={"Depth": "0"})
    assert r.status_code == 404, (r.status_code, r.text[:200])

    got = asyncio.run(rows())
    assert {r["path"] for r in got} == {"/mkcol-dir", "/put-dir", "/lazy-dir"}, got
    print("smoke_webdav_mkdir: OK")


if __name__ == "__main__":
    main()
