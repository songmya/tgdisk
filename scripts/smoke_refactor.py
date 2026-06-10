#!/usr/bin/env python3
"""Focused smoke tests for the refactor safety fixes."""

import asyncio
import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before importing project modules that read config at import time.
TMP = tempfile.TemporaryDirectory()
ROOT = Path(TMP.name)
os.environ["DB_PATH"] = str(ROOT / "test.sqlite3")
os.environ["BOT_TOKEN"] = "123:test"
os.environ["ADMIN_IDS"] = "1"
os.environ["UPLOAD_CACHE_DIR"] = str(ROOT / "cache")
os.environ["RESUME_ALLOWED_DIRS"] = str(ROOT / "cache")
os.environ["WEBUI_TOKEN"] = "secret"

import aiosqlite
from fastapi import HTTPException
from fastapi.testclient import TestClient

from database import init_db, FileDB, UserDB, get_db
import tg_io
from tg_io import stream_file_by_id
from webui import app, _safe_resume_path


async def prepare_partial_multipart() -> int:
    await init_db()
    db = await get_db()
    try:
        await UserDB(db).ensure_user(1, "", "", "admin")
        fdb = FileDB(db)
        fid = await fdb.add_multipart_file(
            file_name="broken.bin",
            file_size=9,
            mime_type="application/octet-stream",
            file_type="document",
            path="/",
            uploader_id=1,
            chunk_count=3,
            total_sha256="",
        )
        await fdb.add_chunk(fid, 0, "tg0", "u0", 3, "sha0")
        await db.execute(
            "INSERT INTO file_chunks (file_id_int, chunk_index, tg_file_id, tg_file_unique_id, chunk_size, chunk_sha256, status) VALUES (?, ?, '', '', ?, ?, 'failed')",
            (fid, 1, 3, "sha1"),
        )
        await fdb.add_chunk(fid, 2, "tg2", "u2", 3, "sha2")
        await db.commit()
        return fid
    finally:
        await db.close()


async def test_missing_chunks_and_stream_guard():
    fid = await prepare_partial_multipart()
    db = await get_db()
    try:
        fdb = FileDB(db)
        missing = await fdb.get_missing_chunks(fid)
        assert missing == [1], missing
        integrity = await fdb.multipart_integrity(fid)
        assert integrity["complete"] is False
        assert integrity["missing"] == [1]
    finally:
        await db.close()

    try:
        _ = [chunk async for chunk in stream_file_by_id(fid)]
    except RuntimeError as e:
        assert "分片不完整" in str(e)
    else:
        raise AssertionError("stream_file_by_id should fail loud for incomplete multipart")


async def test_failed_stream_upload_keeps_resume_metadata():
    old_chunk = tg_io.CHUNK_SIZE
    old_single = tg_io.SINGLE_UPLOAD_THRESHOLD
    old_conc = tg_io.UPLOAD_CONCURRENCY
    old_send = tg_io._send_with_retry
    tg_io.CHUNK_SIZE = 3
    tg_io.SINGLE_UPLOAD_THRESHOLD = 2
    tg_io.UPLOAD_CONCURRENCY = 1

    calls = []

    async def fake_send(session, chat_id, filename, mime_type, data, caption):
        idx = len(calls)
        calls.append((filename, bytes(data)))
        if idx == 1:
            raise RuntimeError("forced chunk failure")
        return {
            "ok": True,
            "result": {
                "message_id": 100 + idx,
                "chat": {"id": chat_id},
                "document": {"file_id": f"tg{idx}", "file_unique_id": f"u{idx}", "file_size": len(data)},
            },
        }

    async def reader_factory(payload: bytes):
        pos = 0

        async def reader(n: int) -> bytes:
            nonlocal pos
            out = payload[pos:pos+n]
            pos += len(out)
            return out

        return reader

    tg_io._send_with_retry = fake_send
    try:
        reader = await reader_factory(b"abcdef")
        try:
            await tg_io.upload_stream_to_tg(reader, "x.bin", "application/octet-stream", "/", uploader_id=1)
        except RuntimeError as e:
            assert "分片上传失败" in str(e)
        else:
            raise AssertionError("upload_stream_to_tg should fail")

        db = await get_db()
        try:
            cur = await db.execute("SELECT id, file_size, chunk_count FROM files WHERE file_name='x.bin' ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
            assert row is not None
            assert row["file_size"] == 6, dict(row)
            assert row["chunk_count"] == 2, dict(row)
            missing = await FileDB(db).get_missing_chunks(row["id"])
            assert missing == [1], missing
        finally:
            await db.close()
    finally:
        tg_io.CHUNK_SIZE = old_chunk
        tg_io.SINGLE_UPLOAD_THRESHOLD = old_single
        tg_io.UPLOAD_CONCURRENCY = old_conc
        tg_io._send_with_retry = old_send


def test_resume_path_guard():
    allowed = ROOT / "cache" / "source.bin"
    allowed.parent.mkdir(parents=True, exist_ok=True)
    allowed.write_bytes(b"abc")
    assert _safe_resume_path(str(allowed)) == str(allowed.resolve())

    blocked = ROOT / "blocked.bin"
    blocked.write_bytes(b"secret")
    try:
        _safe_resume_path(str(blocked))
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("_safe_resume_path should reject paths outside allowlist")


def test_web_auth():
    client = TestClient(app)
    r = client.get("/api/stats")
    assert r.status_code == 401, r.text
    r = client.get("/api/stats", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200, r.text


def main():
    try:
        asyncio.run(test_missing_chunks_and_stream_guard())
        asyncio.run(test_failed_stream_upload_keeps_resume_metadata())
        test_resume_path_guard()
        test_web_auth()
        print("smoke_refactor: OK")
    finally:
        TMP.cleanup()


if __name__ == "__main__":
    main()
