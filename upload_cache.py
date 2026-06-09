"""TGDrive upload cache and background Telegram upload sessions."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import HTTPException, UploadFile

from config import DB_PATH, ADMIN_IDS
from tg_io import upload_local_file_to_tg, CHUNK_SIZE

UPLOAD_CACHE_ENABLED = os.getenv("UPLOAD_CACHE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
UPLOAD_CACHE_DIR = Path(os.getenv("UPLOAD_CACHE_DIR", "data/cache"))
UPLOAD_CACHE_MAX_SIZE_MB = int(os.getenv("UPLOAD_CACHE_MAX_SIZE_MB", "10240"))
UPLOAD_CACHE_MAX_FILE_SIZE_MB = int(os.getenv("UPLOAD_CACHE_MAX_FILE_SIZE_MB", "0"))
UPLOAD_CACHE_TTL_HOURS = int(os.getenv("UPLOAD_CACHE_TTL_HOURS", "24"))
UPLOAD_CACHE_KEEP_AFTER_DONE = os.getenv("UPLOAD_CACHE_KEEP_AFTER_DONE", "false").lower() in ("1", "true", "yes", "on")

WRITE_CHUNK_SIZE = 1024 * 1024


def _now() -> int:
    return int(time.time())


def _expires_at() -> int:
    return _now() + max(1, UPLOAD_CACHE_TTL_HOURS) * 3600


async def _db() -> aiosqlite.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_upload_cache() -> None:
    UPLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = await _db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_sessions (
                id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                mime_type TEXT DEFAULT 'application/octet-stream',
                dest_path TEXT DEFAULT '/',
                total_size INTEGER DEFAULT 0,
                received_size INTEGER DEFAULT 0,
                cache_path TEXT NOT NULL,
                status TEXT DEFAULT 'created',
                stage TEXT DEFAULT 'created',
                telegram_read_bytes INTEGER DEFAULT 0,
                telegram_total_bytes INTEGER DEFAULT 0,
                telegram_total_chunks INTEGER DEFAULT 0,
                tg_file_id INTEGER DEFAULT 0,
                result_json TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_upload_sessions_status ON upload_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_upload_sessions_expires ON upload_sessions(expires_at);
            """
        )
        await db.commit()
    finally:
        await db.close()
    await cleanup_upload_cache()


async def cleanup_upload_cache() -> dict:
    """Remove expired cache files and old completed rows.

    For active uploading/receiving sessions we only delete them when expired; this is a
    safety valve for crashed processes, not a scheduler replacement.
    """
    await init_table_only()
    now = _now()
    removed_files = 0
    removed_rows = 0
    db = await _db()
    try:
        cursor = await db.execute(
            "SELECT id, cache_path, status FROM upload_sessions WHERE expires_at <= ?",
            (now,),
        )
        expired = [dict(r) for r in await cursor.fetchall()]
        for row in expired:
            try:
                Path(row["cache_path"]).unlink(missing_ok=True)
                removed_files += 1
            except Exception:
                pass
            await db.execute("DELETE FROM upload_sessions WHERE id=?", (row["id"],))
            removed_rows += 1
        await db.commit()
    finally:
        await db.close()

    # Size pressure: delete oldest finished/failed/canceled caches first.
    max_bytes = UPLOAD_CACHE_MAX_SIZE_MB * 1024 * 1024
    if max_bytes > 0:
        total = await cache_size_bytes()
        if total > max_bytes:
            db = await _db()
            try:
                cursor = await db.execute(
                    """SELECT id, cache_path FROM upload_sessions
                       WHERE status IN ('done','failed','canceled')
                       ORDER BY updated_at ASC"""
                )
                for row in await cursor.fetchall():
                    if total <= max_bytes:
                        break
                    p = Path(row["cache_path"])
                    size = p.stat().st_size if p.exists() else 0
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                    total -= size
                    await db.execute("DELETE FROM upload_sessions WHERE id=?", (row["id"],))
                    removed_rows += 1
                await db.commit()
            finally:
                await db.close()
    return {"removed_files": removed_files, "removed_rows": removed_rows, "cache_size": await cache_size_bytes()}


async def init_table_only() -> None:
    UPLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = await _db()
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS upload_sessions (
                id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                mime_type TEXT DEFAULT 'application/octet-stream',
                dest_path TEXT DEFAULT '/',
                total_size INTEGER DEFAULT 0,
                received_size INTEGER DEFAULT 0,
                cache_path TEXT NOT NULL,
                status TEXT DEFAULT 'created',
                stage TEXT DEFAULT 'created',
                telegram_read_bytes INTEGER DEFAULT 0,
                telegram_total_bytes INTEGER DEFAULT 0,
                telegram_total_chunks INTEGER DEFAULT 0,
                tg_file_id INTEGER DEFAULT 0,
                result_json TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )"""
        )
        await db.commit()
    finally:
        await db.close()


async def cache_size_bytes() -> int:
    total = 0
    if not UPLOAD_CACHE_DIR.exists():
        return 0
    for p in UPLOAD_CACHE_DIR.glob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


async def cache_info() -> dict:
    await init_table_only()
    db = await _db()
    try:
        cursor = await db.execute("SELECT status, COUNT(*) AS count FROM upload_sessions GROUP BY status")
        by_status = {row["status"]: row["count"] for row in await cursor.fetchall()}
    finally:
        await db.close()
    return {
        "enabled": UPLOAD_CACHE_ENABLED,
        "dir": str(UPLOAD_CACHE_DIR),
        "max_size_mb": UPLOAD_CACHE_MAX_SIZE_MB,
        "max_file_size_mb": UPLOAD_CACHE_MAX_FILE_SIZE_MB,
        "ttl_hours": UPLOAD_CACHE_TTL_HOURS,
        "keep_after_done": UPLOAD_CACHE_KEEP_AFTER_DONE,
        "size_bytes": await cache_size_bytes(),
        "by_status": by_status,
    }


async def create_upload_session(file_name: str, file_size: int = 0, mime_type: str = "application/octet-stream", dest_path: str = "/") -> dict:
    if not UPLOAD_CACHE_ENABLED:
        raise HTTPException(400, "上传缓存未启用")
    await init_upload_cache()
    if UPLOAD_CACHE_MAX_FILE_SIZE_MB > 0 and file_size > UPLOAD_CACHE_MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 UPLOAD_CACHE_MAX_FILE_SIZE_MB={UPLOAD_CACHE_MAX_FILE_SIZE_MB}MB")
    max_bytes = UPLOAD_CACHE_MAX_SIZE_MB * 1024 * 1024
    if max_bytes > 0 and file_size > max_bytes:
        raise HTTPException(413, f"文件超过 UPLOAD_CACHE_MAX_SIZE_MB={UPLOAD_CACHE_MAX_SIZE_MB}MB")
    if max_bytes > 0 and await cache_size_bytes() + max(0, file_size) > max_bytes:
        await cleanup_upload_cache()
        if await cache_size_bytes() + max(0, file_size) > max_bytes:
            raise HTTPException(507, "上传缓存空间不足，请稍后再试或调大 UPLOAD_CACHE_MAX_SIZE_MB")

    sid = uuid.uuid4().hex
    safe_name = file_name.replace("/", "_").replace("\\", "_") or "upload.bin"
    cache_path = UPLOAD_CACHE_DIR / f"{sid}_{safe_name}.part"
    ts = _now()
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE if file_size else 0
    db = await _db()
    try:
        await db.execute(
            """INSERT INTO upload_sessions
               (id, file_name, mime_type, dest_path, total_size, cache_path, status, stage,
                telegram_total_bytes, telegram_total_chunks, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 'created', 'created', ?, ?, ?, ?, ?)""",
            (sid, safe_name, mime_type or "application/octet-stream", dest_path or "/", file_size,
             str(cache_path), file_size, total_chunks, ts, ts, _expires_at()),
        )
        await db.commit()
    finally:
        await db.close()
    return await get_upload_session(sid)


async def get_upload_session(session_id: str) -> dict:
    await init_table_only()
    db = await _db()
    try:
        cursor = await db.execute("SELECT * FROM upload_sessions WHERE id=?", (session_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "上传任务不存在")
    data = dict(row)
    if data.get("result_json"):
        try:
            data["result"] = json.loads(data["result_json"])
        except Exception:
            data["result"] = None
    else:
        data["result"] = None
    data["server_progress"] = (data["received_size"] / data["total_size"] * 100) if data["total_size"] else 0
    data["telegram_progress"] = (data["telegram_read_bytes"] / data["telegram_total_bytes"] * 100) if data["telegram_total_bytes"] else 0
    return data


async def list_upload_sessions(limit: int = 50) -> list[dict]:
    await init_table_only()
    db = await _db()
    try:
        cursor = await db.execute("SELECT id FROM upload_sessions ORDER BY created_at DESC LIMIT ?", (limit,))
        ids = [row["id"] for row in await cursor.fetchall()]
    finally:
        await db.close()
    return [await get_upload_session(i) for i in ids]


async def receive_upload_data(session_id: str, file: UploadFile) -> dict:
    s = await get_upload_session(session_id)
    if s["status"] not in ("created", "failed", "canceled"):
        raise HTTPException(409, f"当前状态不允许上传数据: {s['status']}")
    path = Path(s["cache_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _now()
    db = await _db()
    try:
        await db.execute(
            "UPDATE upload_sessions SET status='receiving', stage='receiving', received_size=0, error='', updated_at=?, expires_at=? WHERE id=?",
            (ts, _expires_at(), session_id),
        )
        await db.commit()
    finally:
        await db.close()

    received = 0
    try:
        with path.open("wb") as out:
            while True:
                chunk = await file.read(WRITE_CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if UPLOAD_CACHE_MAX_FILE_SIZE_MB > 0 and received > UPLOAD_CACHE_MAX_FILE_SIZE_MB * 1024 * 1024:
                    raise HTTPException(413, f"文件超过 UPLOAD_CACHE_MAX_FILE_SIZE_MB={UPLOAD_CACHE_MAX_FILE_SIZE_MB}MB")
                out.write(chunk)
                if received % (8 * WRITE_CHUNK_SIZE) == 0:
                    await _update(session_id, received_size=received, updated_at=_now())
        total_chunks = (received + CHUNK_SIZE - 1) // CHUNK_SIZE if received else 0
        await _update(session_id, received_size=received, total_size=received, telegram_total_bytes=received,
                      telegram_total_chunks=total_chunks, status="cached", stage="cached", updated_at=_now(), expires_at=_expires_at())
        return await get_upload_session(session_id)
    except Exception as e:
        if isinstance(e, HTTPException):
            msg = str(e.detail)
        else:
            msg = str(e)
        await _update(session_id, status="failed", stage="receiving", error=msg, updated_at=_now())
        raise


async def cancel_upload_session(session_id: str) -> dict:
    s = await get_upload_session(session_id)
    if s["status"] in ("done",):
        return s
    await _update(session_id, status="canceled", stage="canceled", error="已取消", updated_at=_now())
    return await get_upload_session(session_id)


async def delete_upload_session(session_id: str) -> dict:
    s = await get_upload_session(session_id)
    try:
        Path(s["cache_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    db = await _db()
    try:
        await db.execute("DELETE FROM upload_sessions WHERE id=?", (session_id,))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


async def process_cached_upload(session_id: str) -> None:
    s = await get_upload_session(session_id)
    if s["status"] == "canceled":
        return
    path = Path(s["cache_path"])
    if not path.exists():
        await _update(session_id, status="failed", stage="uploading", error="缓存文件不存在", updated_at=_now())
        return
    await _update(session_id, status="uploading", stage="telegram", telegram_read_bytes=0, error="", updated_at=_now())

    def progress(done: int, total: int) -> None:
        # Called from sync file reader executor path; schedule a DB update safely.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_update(session_id, telegram_read_bytes=done, telegram_total_bytes=total, updated_at=_now()))
        except RuntimeError:
            pass

    try:
        result = await upload_local_file_to_tg(
            local_path=str(path),
            file_name=s["file_name"],
            mime_type=s["mime_type"] or "application/octet-stream",
            dest_path=s["dest_path"] or "/",
            uploader_id=ADMIN_IDS[0],
            progress_cb=progress,
        )
        await _update(session_id, status="done", stage="done", telegram_read_bytes=s["total_size"],
                      tg_file_id=int(result.get("file_id") or 0), result_json=json.dumps(result, ensure_ascii=False),
                      updated_at=_now())
        if not UPLOAD_CACHE_KEEP_AFTER_DONE:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        await _update(session_id, status="failed", stage="telegram", error=str(e), updated_at=_now(), expires_at=_expires_at())


async def _update(session_id: str, **fields) -> None:
    if not fields:
        return
    keys = []
    vals = []
    for k, v in fields.items():
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(session_id)
    db = await _db()
    try:
        await db.execute(f"UPDATE upload_sessions SET {', '.join(keys)} WHERE id=?", vals)
        await db.commit()
    finally:
        await db.close()
