"""Telegram Bot API I/O 工具：流式切片上传、失败重传、Range 流式下载。

为 webui 和 webdav 共用。
"""

from __future__ import annotations

import os
import asyncio
import hashlib
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

import aiohttp
import aiosqlite

from config import BOT_TOKEN, PROXY, ADMIN_IDS, DB_PATH, MAX_FILE_SIZE

logger = logging.getLogger(__name__)

# ===== Telegram Bot API 限制 =====
# sendDocument: 50MB；getFile: 20MB。统一按 18MB 切，留余量给 form 编码与协议头。
CHUNK_SIZE = 18 * 1024 * 1024
SINGLE_UPLOAD_THRESHOLD = 18 * 1024 * 1024

# 并发参数
UPLOAD_CONCURRENCY = int(os.getenv("UPLOAD_CONCURRENCY", "4"))
UPLOAD_RETRY = 3
UPLOAD_RETRY_BACKOFF = 1.5  # 秒，乘以 attempt+1


# ---------- 基础调用 ----------

async def _tg_send_document(
    session: aiohttp.ClientSession,
    chat_id: int,
    filename: str,
    mime_type: str,
    data: bytes,
    caption: str = "",
) -> dict:
    """sendDocument。返回原始 JSON。"""
    proxy = PROXY or None
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("document", data, filename=filename, content_type=mime_type)
    if caption:
        form.add_field("caption", caption)
    async with session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data=form, proxy=proxy,
        timeout=aiohttp.ClientTimeout(total=600),
    ) as resp:
        return await resp.json()


async def _tg_get_file_url(session: aiohttp.ClientSession, file_id: str) -> Optional[str]:
    proxy = PROXY or None
    async with session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
        json={"file_id": file_id}, proxy=proxy,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        data = await resp.json()
    if not data.get("ok"):
        logger.error("getFile failed: %s", data)
        return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{data['result']['file_path']}"


async def _send_with_retry(
    session: aiohttp.ClientSession,
    chat_id: int,
    filename: str,
    mime_type: str,
    data: bytes,
    caption: str,
) -> dict:
    """带重试的 sendDocument，失败抛 RuntimeError。"""
    last_err = None
    for attempt in range(UPLOAD_RETRY):
        try:
            res = await _tg_send_document(session, chat_id, filename, mime_type, data, caption)
            if res.get("ok"):
                return res
            last_err = res.get("description", "unknown")
        except Exception as e:
            last_err = str(e)
        await asyncio.sleep(UPLOAD_RETRY_BACKOFF * (attempt + 1))
    raise RuntimeError(f"sendDocument 失败: {last_err}")


# ---------- 类型推断 ----------

def guess_file_type(mime_type: str) -> str:
    if mime_type.startswith("image/"): return "photo"
    if mime_type.startswith("video/"): return "video"
    if mime_type.startswith("audio/"): return "audio"
    return "document"


async def _ensure_uploader_exists(uploader_id: int):
    """保证 users 表中有该上传者记录，避免外键失败。"""
    from database import UserDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        udb = UserDB(db)
        role = "admin" if uploader_id in ADMIN_IDS else "user"
        await udb.ensure_user(uploader_id, "", "", role)
    finally:
        await db.close()


# ---------- 单文件上传（小文件） ----------

async def _upload_single_bytes(
    payload: bytes, file_name: str, mime_type: str,
    dest_path: str, uploader_id: int,
) -> dict:
    file_type = guess_file_type(mime_type)
    async with aiohttp.ClientSession() as s:
        cap = f"{dest_path} (单传)" if dest_path != "/" else "来自 TGDrive 的上传"
        res = await _send_with_retry(s, uploader_id, file_name, mime_type, payload, cap)
    msg = res["result"]
    doc = msg.get("document") or {}
    if not doc.get("file_id"):
        # 兼容 photo/video 类型场景（一般我们都用 sendDocument，所以应当总有 document）
        raise RuntimeError("Telegram 未返回 document 对象")

    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        fdb = FileDB(db)
        row_id = await fdb.add_file(
            file_id=doc["file_id"],
            file_unique_id=doc.get("file_unique_id", ""),
            file_name=file_name,
            file_size=doc.get("file_size", len(payload)),
            mime_type=mime_type,
            file_type=file_type,
            path=dest_path,
            uploader_id=uploader_id,
            message_id=msg.get("message_id", 0),
            chat_id=msg.get("chat", {}).get("id", 0),
        )
    finally:
        await db.close()
    return {"ok": True, "mode": "single", "file_id": row_id,
            "file_name": file_name, "size": len(payload), "path": dest_path}


# ---------- 流式切片上传 ----------

async def upload_stream_to_tg(
    reader: Callable[[int], Awaitable[bytes]],
    file_name: str,
    mime_type: str,
    dest_path: str,
    uploader_id: Optional[int] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> dict:
    """从一个 async reader(n)->bytes 来源流式读取，自动判断单传 vs 切片。

    单传路径: 第一片读到 <= SINGLE_UPLOAD_THRESHOLD 时，整体 sendDocument。
    切片路径: 每读满 CHUNK_SIZE 立刻丢给后台 worker 上传。
              切片是并发上传（UPLOAD_CONCURRENCY 路），后台失败会标记失败，主流程结束抛错。
    """
    if uploader_id is None:
        uploader_id = ADMIN_IDS[0]

    # 先尝试凑足 SINGLE_UPLOAD_THRESHOLD+1 字节来判断走单传还是切片
    first = b""
    while len(first) <= SINGLE_UPLOAD_THRESHOLD:
        more = await reader(SINGLE_UPLOAD_THRESHOLD + 1 - len(first))
        if not more:
            break
        first += more
    if not first:
        raise ValueError("文件为空")

    await _ensure_uploader_exists(uploader_id)

    # ===== 单传路径 =====
    if len(first) <= SINGLE_UPLOAD_THRESHOLD:
        if progress_cb: progress_cb(len(first))
        if MAX_FILE_SIZE > 0 and len(first) > MAX_FILE_SIZE * 1024 * 1024:
            raise ValueError(f"文件 {file_name} 超过 MAX_FILE_SIZE={MAX_FILE_SIZE}MB")
        return await _upload_single_bytes(
            first, file_name, mime_type, dest_path, uploader_id)

    # ===== 切片路径 =====
    # 此时 first 已经超过 SINGLE_UPLOAD_THRESHOLD（实际是 18MB+1 字节）
    # 我们把 first 重新切成 18MB 的 chunk_index=0，剩余字节预留到下一次累计
    pending = first
    file_type = guess_file_type(mime_type)

    # 先创建主记录（chunk_count 后面更新）
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        fdb = FileDB(db)
        file_int_id = await fdb.add_multipart_file(
            file_name=file_name, file_size=0, mime_type=mime_type,
            file_type=file_type, path=dest_path, uploader_id=uploader_id,
            chunk_count=0, total_sha256="",
        )
    finally:
        await db.close()

    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    pending_tasks: list[asyncio.Task] = []
    failures: list[BaseException] = []
    sha = hashlib.sha256()
    total_size = 0
    chunk_index = 0

    async def upload_one(idx: int, chunk_bytes: bytes, chunk_count_at_send: int):
        chunk_sha = hashlib.sha256(chunk_bytes).hexdigest()
        part_name = f"{file_name}.part{idx+1:04d}"
        cap = (f"【分片 {idx+1}】{file_name}\n"
               f"file_int_id={file_int_id} sha={chunk_sha[:12]}")
        async with sem:
            try:
                async with aiohttp.ClientSession() as s:
                    res = await _send_with_retry(
                        s, uploader_id, part_name,
                        "application/octet-stream", chunk_bytes, cap,
                    )
            except Exception as e:
                # 落入 file_chunks 但状态为 failed（占位，便于断点续传 API 看到）
                db2 = await aiosqlite.connect(DB_PATH)
                try:
                    await db2.execute(
                        "INSERT OR REPLACE INTO file_chunks "
                        "(file_id_int, chunk_index, tg_file_id, tg_file_unique_id, "
                        " chunk_size, chunk_sha256, status, message_id, chat_id) "
                        "VALUES (?, ?, '', '', ?, ?, 'failed', 0, 0)",
                        (file_int_id, idx, len(chunk_bytes), chunk_sha))
                    await db2.commit()
                finally:
                    await db2.close()
                failures.append(e)
                return

        msg = res["result"]
        doc = msg.get("document", {})
        db2 = await aiosqlite.connect(DB_PATH)
        db2.row_factory = aiosqlite.Row
        try:
            fdb2 = FileDB(db2)
            await fdb2.add_chunk(
                file_id_int=file_int_id, chunk_index=idx,
                tg_file_id=doc.get("file_id", ""),
                tg_file_unique_id=doc.get("file_unique_id", ""),
                chunk_size=len(chunk_bytes),
                chunk_sha256=chunk_sha,
                message_id=msg.get("message_id", 0),
                chat_id=msg.get("chat", {}).get("id", 0),
            )
            if idx == 0:
                await db2.execute(
                    "UPDATE files SET file_id=?, file_unique_id=?, "
                    "message_id=?, chat_id=? WHERE id=?",
                    (doc.get("file_id", ""), doc.get("file_unique_id", ""),
                     msg.get("message_id", 0),
                     msg.get("chat", {}).get("id", 0), file_int_id),
                )
                await db2.commit()
        finally:
            await db2.close()

    # 循环读取并切片
    try:
        eof = False
        while True:
            # 凑齐至少 CHUNK_SIZE（或读到 EOF）
            while len(pending) < CHUNK_SIZE and not eof:
                more = await reader(CHUNK_SIZE - len(pending))
                if not more:
                    eof = True
                    break
                pending += more

            if not pending:
                break

            if len(pending) >= CHUNK_SIZE:
                chunk = pending[:CHUNK_SIZE]
                pending = pending[CHUNK_SIZE:]
            else:  # 末尾不满一片
                chunk = pending
                pending = b""

            sha.update(chunk)
            total_size += len(chunk)
            if MAX_FILE_SIZE > 0 and total_size > MAX_FILE_SIZE * 1024 * 1024:
                raise ValueError(
                    f"上传超出 MAX_FILE_SIZE={MAX_FILE_SIZE}MB（已读 {total_size}）")
            if progress_cb:
                progress_cb(total_size)

            t = asyncio.create_task(upload_one(chunk_index, chunk, chunk_index + 1))
            pending_tasks.append(t)
            chunk_index += 1

            # 并发背压：任务队列过长时先 await 一部分，避免内存堆积
            if len(pending_tasks) >= UPLOAD_CONCURRENCY * 2:
                done, _ = await asyncio.wait(
                    pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    pending_tasks.remove(d)

            if eof and not pending:
                break

        await asyncio.gather(*pending_tasks)
    except Exception:
        for t in pending_tasks:
            try: await t
            except Exception: pass
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("UPDATE files SET deleted=1 WHERE id=?", (file_int_id,))
            await db.commit()
        finally:
            await db.close()
        raise

    if failures:
        # 标记主记录"上传未完成"；保留分片信息以便断点续传（不删 deleted=0）
        raise RuntimeError(
            f"{len(failures)} 个分片上传失败；可调用 /api/resume-upload/{file_int_id} 续传")

    chunk_count = chunk_index
    # 更新主记录的总大小、分片数、整体 sha256
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE files SET file_size=?, chunk_count=?, total_sha256=? WHERE id=?",
            (total_size, chunk_count, sha.hexdigest(), file_int_id))
        await db.commit()
    finally:
        await db.close()

    return {
        "ok": True, "mode": "multipart", "file_id": file_int_id,
        "file_name": file_name, "size": total_size, "path": dest_path,
        "chunk_count": chunk_count, "sha256": sha.hexdigest(),
    }


# ---------- 兼容入口：从本地文件上传（webdav / 旧 webui 调用） ----------

async def upload_local_file_to_tg(
    local_path: str, file_name: str, mime_type: str,
    dest_path: str, uploader_id: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """从本地文件上传，封装成调用 upload_stream_to_tg。"""
    size = os.path.getsize(local_path)

    # 把同步文件改成 async reader
    f = open(local_path, "rb")
    loop = asyncio.get_event_loop()

    async def reader(n: int) -> bytes:
        return await loop.run_in_executor(None, f.read, n)

    cb_inner = None
    if progress_cb:
        def cb_inner(done):  # type: ignore[no-redef]
            progress_cb(done, size)

    try:
        return await upload_stream_to_tg(
            reader=reader, file_name=file_name, mime_type=mime_type,
            dest_path=dest_path, uploader_id=uploader_id,
            progress_cb=cb_inner,
        )
    finally:
        f.close()


# ---------- 断点续传：单独重传指定分片 ----------

async def resume_chunks(file_id_int: int, missing: list[int],
                         source_path: str) -> dict:
    """根据 missing chunk 索引，从本地源文件读取对应区域并重传。"""
    if not missing:
        return {"ok": True, "resent": 0}

    from database import FileDB
    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def resend_one(idx: int):
        with open(source_path, "rb") as f:
            f.seek(idx * CHUNK_SIZE)
            data = f.read(CHUNK_SIZE)
        if not data:
            return
        chunk_sha = hashlib.sha256(data).hexdigest()
        part_name = f"chunk-{file_id_int}.part{idx+1:04d}"
        cap = f"【续传 chunk_index={idx}】file_int_id={file_id_int} sha={chunk_sha[:12]}"
        async with sem:
            async with aiohttp.ClientSession() as s:
                res = await _send_with_retry(
                    s, ADMIN_IDS[0], part_name,
                    "application/octet-stream", data, cap)
        msg = res["result"]; doc = msg.get("document", {})
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute(
                "DELETE FROM file_chunks WHERE file_id_int=? AND chunk_index=?",
                (file_id_int, idx))
            fdb = FileDB(db)
            await fdb.add_chunk(
                file_id_int=file_id_int, chunk_index=idx,
                tg_file_id=doc.get("file_id", ""),
                tg_file_unique_id=doc.get("file_unique_id", ""),
                chunk_size=len(data), chunk_sha256=chunk_sha,
                message_id=msg.get("message_id", 0),
                chat_id=msg.get("chat", {}).get("id", 0))
        finally:
            await db.close()

    await asyncio.gather(*[resend_one(i) for i in missing])
    return {"ok": True, "resent": len(missing)}


# ---------- 下载：流式拼接 ----------

async def stream_file_by_id(
    file_id_int: int,
    range_start: int = 0,
    range_end: Optional[int] = None,
) -> AsyncIterator[bytes]:
    """根据 files.id 流式产出文件字节。

    - 自动识别单文件 / multipart。
    - 支持 [range_start, range_end] (闭区间) 切片，多分片场景下只下载需要的分片。
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT id, file_id, file_size, is_multipart, chunk_count "
            "FROM files WHERE id=? AND deleted=0", (file_id_int,))
        row = await cursor.fetchone()
        if not row:
            return
        f = dict(row)
        if f.get("is_multipart"):
            cursor = await db.execute(
                "SELECT chunk_index, tg_file_id, chunk_size FROM file_chunks "
                "WHERE file_id_int=? AND status='ok' "
                "ORDER BY chunk_index ASC", (file_id_int,))
            chunks = [dict(r) for r in await cursor.fetchall()]
        else:
            chunks = [{"chunk_index": 0, "tg_file_id": f["file_id"],
                       "chunk_size": f["file_size"]}]
    finally:
        await db.close()

    if not chunks:
        return

    if range_end is None:
        range_end = f["file_size"] - 1
    bytes_total = f["file_size"]
    range_start = max(0, range_start)
    range_end = min(range_end, bytes_total - 1)
    if range_start > range_end:
        return

    # 计算每个分片在整体文件中的偏移区间
    proxy = PROXY or None
    offset = 0
    async with aiohttp.ClientSession() as s:
        for c in chunks:
            csize = c["chunk_size"]
            c_start = offset
            c_end = offset + csize - 1
            offset += csize
            if c_end < range_start:
                continue
            if c_start > range_end:
                break
            # 这片需要：起止裁剪
            inner_lo = max(0, range_start - c_start)
            inner_hi = min(csize - 1, range_end - c_start)

            url = await _tg_get_file_url(s, c["tg_file_id"])
            if not url:
                return
            # Telegram 文件下载支持 Range 头
            headers = {}
            if inner_lo > 0 or inner_hi < csize - 1:
                headers["Range"] = f"bytes={inner_lo}-{inner_hi}"
            async with s.get(url, proxy=proxy, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=600)) as resp:
                async for piece in resp.content.iter_chunked(64 * 1024):
                    yield piece


# ---------- 孤儿清理 ----------

async def cleanup_orphans(hours: int = 24, dry_run: bool = True) -> dict:
    """把 hours 小时之前未集齐分片的 multipart 文件软删除。hours<0 仅供测试。"""
    modifier = f'{-abs(hours)} hours' if hours >= 0 else f'+{abs(hours)} hours'
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT f.id, f.file_name, f.chunk_count, f.created_at,
                      COUNT(CASE WHEN c.status='ok' THEN 1 END) AS got
               FROM files f LEFT JOIN file_chunks c ON c.file_id_int=f.id
               WHERE f.is_multipart=1 AND f.deleted=0
                 AND datetime(f.created_at) <= datetime('now','localtime',?)
               GROUP BY f.id
               HAVING got < f.chunk_count""",
            (modifier,))
        rows = [dict(r) for r in await cursor.fetchall()]

        if not dry_run:
            for r in rows:
                await db.execute("UPDATE files SET deleted=1 WHERE id=?", (r["id"],))
            await db.commit()
    finally:
        await db.close()
    return {"orphans": rows, "count": len(rows), "dry_run": dry_run}
