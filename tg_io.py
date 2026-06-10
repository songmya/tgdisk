"""Telegram Bot API I/O 工具：流式切片上传、失败重传、Range 流式下载。

为 webui 和 webdav 共用。
"""

from __future__ import annotations

import os
import asyncio
import hashlib
import logging
import io
from typing import AsyncIterator, Awaitable, Callable, Optional

import aiohttp
import aiosqlite

from config import BOT_TOKEN, PROXY, ADMIN_IDS, DB_PATH, MAX_FILE_SIZE, LOCAL_API_BASE, LOCAL_API_MODE, BOT_API_SERVER_DIR, BOT_API_LOCAL_DIR

logger = logging.getLogger(__name__)

# ===== Telegram Bot API 限制 =====
# 官方 Bot API sendDocument 上限约 50MB；默认按 18MB 切，给 multipart/form-data 留余量。
# 自建 telegram-bot-api 可通过环境变量把分片调大，减少分片数与请求数。
def _env_size_mb(name: str, default_mb: int, min_mb: int = 1) -> int:
    raw = os.getenv(name, str(default_mb)).strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 非法，使用默认 %sMB", name, raw, default_mb)
        value = default_mb
    return max(min_mb, value) * 1024 * 1024

CHUNK_SIZE = _env_size_mb("TG_UPLOAD_CHUNK_SIZE_MB", 18)
SINGLE_UPLOAD_THRESHOLD = _env_size_mb(
    "TG_SINGLE_UPLOAD_THRESHOLD_MB",
    max(1, CHUNK_SIZE // (1024 * 1024)),
)
TG_API_BASE = (LOCAL_API_BASE or "https://api.telegram.org").rstrip("/")
TG_FILE_BASE = TG_API_BASE.replace("/bot", "").rstrip("/")


def _tg_proxy() -> Optional[str]:
    """Telegram Bot API 请求使用的代理。

    当 TG_API_BASE 指向自建/本地 Bot API Server 时，bot 程序只需要访问这个
    Bot API Server，本身不应再套 HTTP 代理；否则 127.0.0.1 / LAN 地址可能被
    代理转发导致上传大文件时 Broken pipe / Connection lost。
    """
    if LOCAL_API_BASE:
        return None
    return PROXY or None

# 并发参数
UPLOAD_CONCURRENCY = int(os.getenv("UPLOAD_CONCURRENCY", "4"))
UPLOAD_RETRY = 3
UPLOAD_RETRY_BACKOFF = 1.5  # 秒，乘以 attempt+1

# 自建 Bot API local mode 策略：≤1500MB 单文件上传，>1500MB 按 1500MB 分片。
# Telegram 自建 Bot API 常见单文件上限约 2GB，留安全余量。
LOCAL_SINGLE_UPLOAD_LIMIT = _env_size_mb("TG_LOCAL_SINGLE_UPLOAD_LIMIT_MB", 1500)
LOCAL_PART_SIZE = _env_size_mb("TG_LOCAL_PART_SIZE_MB", 1500)


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
    proxy = _tg_proxy()
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("document", data, filename=filename, content_type=mime_type)
    if caption:
        form.add_field("caption", caption)
    async with session.post(
        f"{TG_API_BASE}/bot{BOT_TOKEN}/sendDocument",
        data=form, proxy=proxy,
        timeout=aiohttp.ClientTimeout(total=600),
    ) as resp:
        return await resp.json()


class _LimitedFileReader(io.IOBase):
    """限制读取范围的同步文件对象，供 aiohttp multipart 流式上传分片。"""

    def __init__(self, path: str, offset: int = 0, size: Optional[int] = None):
        self._file = open(path, "rb")
        self._file.seek(offset)
        self._remaining = size
        self._closed = False

    def readable(self):
        return True

    def read(self, n: int = -1):
        if self._closed:
            return b""
        if self._remaining is not None:
            if self._remaining <= 0:
                return b""
            if n is None or n < 0 or n > self._remaining:
                n = self._remaining
        data = self._file.read(n)
        if self._remaining is not None:
            self._remaining -= len(data)
        return data

    def close(self):
        if not self._closed:
            self._closed = True
            self._file.close()
        super().close()


async def _tg_send_document_file(
    session: aiohttp.ClientSession,
    chat_id: int,
    local_path: str,
    filename: str,
    mime_type: str,
    caption: str = "",
    offset: int = 0,
    size: Optional[int] = None,
) -> dict:
    """sendDocument(local mode): 从本地文件对象流式上传。

    telegram-bot-api 的 HTTP JSON 参数不会把 /path 当作本地文件路径；它会按
    URL/file_id 解析。这里改用 multipart + file object，aiohttp 会从磁盘分块
    读取并发送给本地 Bot API，避免把大文件整体读进 Python 内存。
    offset/size 用于大文件按 1500MB 逻辑分片时只上传文件中的指定范围。
    """
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    if caption:
        form.add_field("caption", caption)
    f = _LimitedFileReader(local_path, offset=offset, size=size)
    try:
        form.add_field("document", f, filename=filename, content_type=mime_type)
        async with session.post(
            f"{TG_API_BASE}/bot{BOT_TOKEN}/sendDocument",
            data=form, proxy=_tg_proxy(),
            timeout=aiohttp.ClientTimeout(total=60 * 60),
        ) as resp:
            return await resp.json()
    finally:
        f.close()


async def _tg_delete_message(session: aiohttp.ClientSession, chat_id: int, message_id: int) -> dict:
    """deleteMessage。注意：Telegram 可能因为时间限制/权限限制拒绝删除。"""
    proxy = _tg_proxy()
    async with session.post(
        f"{TG_API_BASE}/bot{BOT_TOKEN}/deleteMessage",
        json={"chat_id": chat_id, "message_id": message_id},
        proxy=proxy,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        return await resp.json()


async def delete_tg_messages_for_file(file_id_int: int) -> dict:
    """尽力删除一个文件对应的 Telegram 消息。

    Bot API 不能按 file_id 删除 Telegram 服务器文件，只能删除当初 Bot 发送的
    message。multipart 文件会删除主记录消息和所有分片消息。
    """
    targets: list[tuple[int, int, str]] = []
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT id, message_id, chat_id FROM files WHERE id=?",
            (file_id_int,),
        )
        f = await cursor.fetchone()
        if not f:
            return {"ok": False, "deleted": 0, "failed": 0, "errors": ["文件索引不存在"]}
        if f["chat_id"] and f["message_id"]:
            targets.append((int(f["chat_id"]), int(f["message_id"]), "file"))
        cursor = await db.execute(
            "SELECT chunk_index, message_id, chat_id FROM file_chunks WHERE file_id_int=? ORDER BY chunk_index",
            (file_id_int,),
        )
        for c in await cursor.fetchall():
            if c["chat_id"] and c["message_id"]:
                targets.append((int(c["chat_id"]), int(c["message_id"]), f"chunk:{c['chunk_index']}"))
    finally:
        await db.close()

    # 去重：multipart 第 0 片可能和主记录 message 相同。
    seen = set()
    uniq = []
    for chat_id, message_id, kind in targets:
        key = (chat_id, message_id)
        if key not in seen:
            seen.add(key)
            uniq.append((chat_id, message_id, kind))

    deleted = 0
    errors = []
    async with aiohttp.ClientSession() as session:
        for chat_id, message_id, kind in uniq:
            try:
                res = await _tg_delete_message(session, chat_id, message_id)
                if res.get("ok"):
                    deleted += 1
                else:
                    errors.append({
                        "kind": kind,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error": res.get("description", "unknown"),
                    })
            except Exception as e:
                errors.append({
                    "kind": kind,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "error": str(e),
                })
    return {
        "ok": len(errors) == 0,
        "attempted": len(uniq),
        "deleted": deleted,
        "failed": len(errors),
        "errors": errors,
    }


async def _tg_get_file_ref(session: aiohttp.ClientSession, file_id: str) -> Optional[dict]:
    """getFile，兼容官方文件 URL 和 self-hosted local mode 本地路径。"""
    proxy = _tg_proxy()
    async with session.post(
        f"{TG_API_BASE}/bot{BOT_TOKEN}/getFile",
        json={"file_id": file_id}, proxy=proxy,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        data = await resp.json()
    if not data.get("ok"):
        logger.error("getFile failed: %s", data)
        return None

    file_path = data.get("result", {}).get("file_path", "")
    if not file_path:
        logger.error("getFile returned empty file_path: %s", data)
        return None
    if os.path.isabs(file_path):
        return {"kind": "path", "path": file_path}
    return {"kind": "url", "url": f"{TG_FILE_BASE}/file/bot{BOT_TOKEN}/{file_path}"}


def _map_bot_api_local_path(path: str) -> str:
    """把 Bot API Server 返回的本地路径映射到 tgdisk 可读路径。

    local mode 的 getFile 返回 Bot API Server 容器视角路径，通常是
    /var/lib/telegram-bot-api/...。tgdisk 容器需要把同一个 volume 挂到
    BOT_API_LOCAL_DIR；如果两个容器内路径一致，默认无需配置。
    """
    server_dir = BOT_API_SERVER_DIR.rstrip("/")
    local_dir = BOT_API_LOCAL_DIR.rstrip("/")
    if server_dir and (path == server_dir or path.startswith(server_dir + "/")):
        rel = path[len(server_dir):].lstrip("/")
        return os.path.join(local_dir, rel) if rel else local_dir
    return path


def _read_file_range_sync(path: str, start: int, end: int, chunk_size: int = 64 * 1024):
    """同步读取本地文件闭区间 [start, end]，供 async 生成器逐块 yield。"""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


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


# ---------- 覆盖清理 ----------

async def delete_file_and_messages(file_id_int: int) -> tuple[bool, list[str]]:
    """尽力删除 Telegram 消息，并彻底删除本地索引。

    Telegram Bot API 可能因时间或权限限制拒绝删除旧消息；覆盖语义以本地索引
    为准，所以这里记录警告但不因为消息删除失败而阻止新文件覆盖。
    """
    tg_result = await delete_tg_messages_for_file(file_id_int)
    if not tg_result.get("ok"):
        logger.warning(
            "Telegram message cleanup failed for file %s during overwrite: %s",
            file_id_int,
            tg_result.get("errors", []),
        )

    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ok = await FileDB(db).delete_index(file_id_int)
    finally:
        await db.close()
    return ok, []


async def delete_files_by_path_name(path: str, file_name: str) -> tuple[int, list[str]]:
    """删除同目录同名的所有未删除文件，用于 WebDAV 覆盖上传。"""
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        existing_files = await FileDB(db).find_all_by_path_name(path, file_name)
    finally:
        await db.close()

    errors: list[str] = []
    deleted = 0
    for existing in existing_files:
        ok, file_errors = await delete_file_and_messages(int(existing["id"]))
        if ok:
            deleted += 1
        errors.extend(file_errors)
    if errors:
        raise RuntimeError("同名文件覆盖失败，删除旧文件失败：" + "; ".join(errors))
    return deleted, []


# ---------- 流式切片上传 ----------

async def upload_stream_to_tg(
    reader: Callable[[int], Awaitable[bytes]],
    file_name: str,
    mime_type: str,
    dest_path: str,
    uploader_id: Optional[int] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
    overwrite: bool = False,
) -> dict:
    """从一个 async reader(n)->bytes 来源流式读取，自动判断单传 vs 切片。

    单传路径: 第一片读到 <= SINGLE_UPLOAD_THRESHOLD 时，整体 sendDocument。
    切片路径: 每读满 CHUNK_SIZE 立刻丢给后台 worker 上传。
              切片是并发上传（UPLOAD_CONCURRENCY 路），后台失败会标记失败，主流程结束抛错。
    overwrite=True 时，会在上传前清理同目录同名的旧 Telegram 消息和索引，
    让 WebDAV PUT 同名文件表现为直接覆盖。
    """
    if uploader_id is None:
        uploader_id = ADMIN_IDS[0]

    if overwrite:
        await delete_files_by_path_name(dest_path, file_name)

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
    # 此时 first 已经超过 SINGLE_UPLOAD_THRESHOLD。
    # 我们把 first 重新切成 CHUNK_SIZE 的 chunk_index=0，剩余字节预留到下一次累计。
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


# ---------- local Bot API 低内存文件流上传 ----------

async def _upload_local_path_to_tg(
    local_path: str, file_name: str, mime_type: str,
    dest_path: str, uploader_id: int,
) -> dict:
    """本地 Bot API local mode: 从文件对象流式单文件上传。"""
    size = os.path.getsize(local_path)
    if MAX_FILE_SIZE > 0 and size > MAX_FILE_SIZE * 1024 * 1024:
        raise ValueError(f"文件 {file_name} 超过 MAX_FILE_SIZE={MAX_FILE_SIZE}MB")

    file_type = guess_file_type(mime_type)
    async with aiohttp.ClientSession() as s:
        cap = f"{dest_path} (local stream)" if dest_path != "/" else "来自 TGDrive 的上传"
        res = await _tg_send_document_file(s, uploader_id, local_path, file_name, mime_type, cap)
    if not res.get("ok"):
        raise RuntimeError(f"sendDocument(local stream) 失败: {res.get('description', 'unknown')}")

    msg = res["result"]
    doc = msg.get("document") or {}
    if not doc.get("file_id"):
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
            file_size=doc.get("file_size") or size,
            mime_type=mime_type,
            file_type=file_type,
            path=dest_path,
            uploader_id=uploader_id,
            message_id=msg.get("message_id", 0),
            chat_id=msg.get("chat", {}).get("id", 0),
        )
    finally:
        await db.close()
    return {"ok": True, "mode": "local_stream", "file_id": row_id,
            "file_name": file_name, "size": size, "path": dest_path}


async def _upload_local_multipart_to_tg(
    local_path: str, file_name: str, mime_type: str,
    dest_path: str, uploader_id: int,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """本地 Bot API local mode: >1500MB 文件按 1500MB 分片，低内存上传。"""
    size = os.path.getsize(local_path)
    if MAX_FILE_SIZE > 0 and size > MAX_FILE_SIZE * 1024 * 1024:
        raise ValueError(f"文件 {file_name} 超过 MAX_FILE_SIZE={MAX_FILE_SIZE}MB")

    file_type = guess_file_type(mime_type)
    chunk_count = (size + LOCAL_PART_SIZE - 1) // LOCAL_PART_SIZE
    sha = hashlib.sha256()

    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        fdb = FileDB(db)
        file_int_id = await fdb.add_multipart_file(
            file_name=file_name, file_size=0, mime_type=mime_type,
            file_type=file_type, path=dest_path, uploader_id=uploader_id,
            chunk_count=chunk_count, total_sha256="",
        )
    finally:
        await db.close()

    total_done = 0
    try:
        async with aiohttp.ClientSession() as s:
            for idx in range(chunk_count):
                offset = idx * LOCAL_PART_SIZE
                part_size = min(LOCAL_PART_SIZE, size - offset)
                part_name = f"{file_name}.part{idx+1:04d}"
                chunk_sha = hashlib.sha256()
                # 只为校验计算流式 hash，不把分片读进内存。
                with open(local_path, "rb") as f:
                    f.seek(offset)
                    remaining = part_size
                    while remaining > 0:
                        data = f.read(min(1024 * 1024, remaining))
                        if not data:
                            break
                        chunk_sha.update(data)
                        sha.update(data)
                        remaining -= len(data)
                chunk_hex = chunk_sha.hexdigest()
                cap = (f"【分片 {idx+1}/{chunk_count}】{file_name}\n"
                       f"file_int_id={file_int_id} sha={chunk_hex[:12]}")
                res = await _tg_send_document_file(
                    s, uploader_id, local_path, part_name, "application/octet-stream",
                    cap, offset=offset, size=part_size,
                )
                if not res.get("ok"):
                    raise RuntimeError(f"sendDocument(local part {idx+1}) 失败: {res.get('description', 'unknown')}")
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
                        chunk_size=part_size,
                        chunk_sha256=chunk_hex,
                        message_id=msg.get("message_id", 0),
                        chat_id=msg.get("chat", {}).get("id", 0),
                    )
                    if idx == 0:
                        await db2.execute(
                            "UPDATE files SET file_id=?, file_unique_id=?, message_id=?, chat_id=? WHERE id=?",
                            (doc.get("file_id", ""), doc.get("file_unique_id", ""),
                             msg.get("message_id", 0), msg.get("chat", {}).get("id", 0), file_int_id),
                        )
                        await db2.commit()
                finally:
                    await db2.close()
                total_done += part_size
                if progress_cb:
                    progress_cb(total_done, size)

        db3 = await aiosqlite.connect(DB_PATH)
        try:
            await db3.execute(
                "UPDATE files SET file_size=?, chunk_count=?, total_sha256=? WHERE id=?",
                (size, chunk_count, sha.hexdigest(), file_int_id),
            )
            await db3.commit()
        finally:
            await db3.close()
    except Exception:
        db4 = await aiosqlite.connect(DB_PATH)
        try:
            await db4.execute("UPDATE files SET deleted=1 WHERE id=?", (file_int_id,))
            await db4.commit()
        finally:
            await db4.close()
        raise

    return {"ok": True, "mode": "local_multipart", "file_id": file_int_id,
            "file_name": file_name, "size": size, "path": dest_path,
            "chunk_count": chunk_count, "sha256": sha.hexdigest()}


# ---------- 兼容入口：从本地文件上传（webdav / 旧 webui 调用） ----------

async def upload_local_file_to_tg(
    local_path: str, file_name: str, mime_type: str,
    dest_path: str, uploader_id: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    overwrite: bool = False,
) -> dict:
    """从本地文件上传。

    LOCAL_API_MODE=true 且配置 LOCAL_API_BASE 时，优先用本地文件对象流式
    上传，避免 Python 进程按大分片把文件读入内存。否则回退到兼容的
    流式切片上传。
    """
    size = os.path.getsize(local_path)
    if uploader_id is None:
        uploader_id = ADMIN_IDS[0]

    if overwrite:
        await delete_files_by_path_name(dest_path, file_name)

    if LOCAL_API_BASE and LOCAL_API_MODE:
        if progress_cb:
            progress_cb(0, size)
        if size <= LOCAL_SINGLE_UPLOAD_LIMIT:
            result = await _upload_local_path_to_tg(
                local_path, file_name, mime_type, dest_path, uploader_id
            )
            if progress_cb:
                progress_cb(size, size)
            return result
        return await _upload_local_multipart_to_tg(
            local_path, file_name, mime_type, dest_path, uploader_id, progress_cb=progress_cb
        )

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
            overwrite=False,
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
    proxy = _tg_proxy()
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

            ref = await _tg_get_file_ref(s, c["tg_file_id"])
            if not ref:
                return

            if ref["kind"] == "path":
                # self-hosted Bot API local mode: getFile 返回 Bot API 服务器本地路径。
                # tgdisk 与 botapi 同机/同容器路径可见时，直接按 Range 读取本地文件。
                original_path = ref["path"]
                path = _map_bot_api_local_path(original_path)
                if not os.path.exists(path):
                    logger.error(
                        "local getFile path does not exist: %s (mapped from %s). "
                        "Mount Bot API data dir into tgdisk and set BOT_API_SERVER_DIR/BOT_API_LOCAL_DIR if needed.",
                        path, original_path,
                    )
                    return
                for piece in _read_file_range_sync(path, inner_lo, inner_hi):
                    yield piece
                continue

            # 官方/非 local mode: Telegram 文件下载支持 Range 头。
            headers = {}
            if inner_lo > 0 or inner_hi < csize - 1:
                headers["Range"] = f"bytes={inner_lo}-{inner_hi}"
            async with s.get(ref["url"], proxy=proxy, headers=headers,
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
