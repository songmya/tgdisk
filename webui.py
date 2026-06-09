"""TGDrive Web UI - FastAPI 后端"""

import os
import hashlib
import asyncio
import aiosqlite
from pathlib import Path
from fastapi import FastAPI, Query, HTTPException, UploadFile, File, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.wsgi import WSGIMiddleware
import uvicorn

import config
from database import init_db
from config import DB_PATH, BOT_TOKEN, PROXY, ADMIN_IDS, MAX_FILE_SIZE
from tg_io import (upload_local_file_to_tg, upload_stream_to_tg,
                   stream_file_by_id, resume_chunks, cleanup_orphans,
                   SINGLE_UPLOAD_THRESHOLD, CHUNK_SIZE)
from upload_cache import (
    UPLOAD_CACHE_ENABLED,
    init_upload_cache,
    cache_info,
    cleanup_upload_cache,
    create_upload_session,
    receive_upload_data,
    write_upload_chunk,
    mark_upload_paused,
    commit_cached_upload,
    process_cached_upload,
    get_upload_session,
    list_upload_sessions,
    cancel_upload_session,
    delete_upload_session,
)

app = FastAPI(title="TGDrive Web")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup_upload_cache():
    await init_db()
    await init_upload_cache()

WWW = Path(__file__).parent / "www"
WWW.mkdir(exist_ok=True)


def get_bot() -> str:
    """获取 Bot 用户名用于展示"""
    return "TGDrive"


async def query_db(sql: str, params: tuple = ()):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()




# WebDAV 与 WebUI 共用同一个端口：WebDAV 挂载在 /dav
try:
    from webdav import create_webdav_app
    app.mount("/dav", WSGIMiddleware(create_webdav_app()))
except Exception as e:
    import logging
    logging.getLogger(__name__).warning("WebDAV mount disabled: %s", e)



@app.get("/api/webdav")
async def api_webdav_info():
    return {
        "enabled": True,
        "path": "/dav",
        "url": "/dav",
        "note": "WebDAV is mounted on the same WebUI port under /dav",
    }

@app.get("/", response_class=HTMLResponse)
async def index():
    idx = WWW / "index.html"
    if not idx.exists():
        return HTMLResponse("<h1>UI 文件未找到，请先部署前端</h1>")
    return HTMLResponse(idx.read_text(encoding="utf-8"))


@app.get("/api/stats")
async def api_stats():
    rows = await query_db(
        "SELECT COUNT(*) as count, COALESCE(SUM(file_size),0) as total_size FROM files WHERE deleted=0"
    )
    dirs = await query_db("SELECT COUNT(*) as count FROM dirs")
    types = await query_db(
        """SELECT file_type, COUNT(*) as count
           FROM files WHERE deleted=0 GROUP BY file_type ORDER BY count DESC"""
    )
    return {
        "file_count": rows[0]["count"],
        "total_size": rows[0]["total_size"],
        "dir_count": dirs[0]["count"],
        "types": types,
    }


def format_size(s: int) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if s < 1024:
            return f"{s:.1f} {u}"
        s /= 1024
    return f"{s:.1f} TB"


@app.get("/api/files")
async def api_files(
    path: str = Query("/"),
    search: str = Query(""),
    page: int = Query(1),
    limit: int = Query(50),
):
    offset = (page - 1) * limit
    if search:
        rows = await query_db(
            """SELECT * FROM files
               WHERE (file_name LIKE ? OR tags LIKE ?) AND deleted=0
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (f"%{search}%", f"%{search}%", limit, offset),
        )
        total_row = await query_db(
            """SELECT COUNT(*) as count FROM files
               WHERE (file_name LIKE ? OR tags LIKE ?) AND deleted=0""",
            (f"%{search}%", f"%{search}%"),
        )
    else:
        rows = await query_db(
            """SELECT * FROM files WHERE path=? AND deleted=0
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (path, limit, offset),
        )
        total_row = await query_db(
            "SELECT COUNT(*) as count FROM files WHERE path=? AND deleted=0",
            (path,),
        )

    total = total_row[0]["count"] if total_row else 0

    files = []
    for r in rows:
        files.append({
            "id": r["id"],
            "file_name": r["file_name"],
            "file_size": r["file_size"],
            "size_fmt": format_size(r["file_size"]),
            "mime_type": r["mime_type"],
            "file_type": r["file_type"],
            "path": r["path"],
            "tags": r["tags"],
            "created_at": r["created_at"],
        })

    return {
        "files": files,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, -(-total // limit)),  # ceil division
    }


@app.get("/api/dirs")
async def api_dirs(path: str = Query("/")):
    subdirs = await query_db(
        "SELECT * FROM dirs WHERE parent_path=? ORDER BY name", (path,)
    )
    # breadcrumb
    parts = []
    if path != "/":
        parts = path.strip("/").split("/")
    bread = [{"name": "根目录", "path": "/"}]
    acc = ""
    for p in parts:
        acc += "/" + p
        bread.append({"name": p, "path": acc})
    return {"dirs": subdirs, "breadcrumb": bread, "current": path}



@app.post("/api/upload")
async def api_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), path: str = "/"):
    """网页端上传：先写入可配置缓存文件，再后台上传 Telegram。

    返回 upload session，前端可轮询 /api/uploads/{session_id} 获取
    Telegram 分片上传进度。缓存由 UPLOAD_CACHE_* 环境变量控制。
    """
    if not UPLOAD_CACHE_ENABLED:
        # 兼容模式：缓存关闭时回退到旧的流式上传，接口等待 Telegram 完成。
        filename = file.filename or "upload.bin"
        mime_type = file.content_type or "application/octet-stream"

        async def reader(n: int) -> bytes:
            return await file.read(n)

        try:
            return await upload_stream_to_tg(
                reader=reader, file_name=filename, mime_type=mime_type,
                dest_path=path, uploader_id=ADMIN_IDS[0],
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(500, str(e))

    filename = file.filename or "upload.bin"
    mime_type = file.content_type or "application/octet-stream"
    declared_size = 0
    try:
        declared_size = int(file.headers.get("content-length") or 0)
    except Exception:
        declared_size = 0

    session = await create_upload_session(
        file_name=filename, file_size=declared_size,
        mime_type=mime_type, dest_path=path,
    )
    session = await receive_upload_data(session["id"], file)
    background_tasks.add_task(process_cached_upload, session["id"])
    session["accepted"] = True
    return session


@app.post("/api/uploads/init")
async def api_upload_init(
    file_name: str = Query(...),
    file_size: int = Query(...),
    mime_type: str = Query("application/octet-stream"),
    path: str = Query("/"),
):
    """初始化浏览器分块上传任务。"""
    session = await create_upload_session(
        file_name=file_name, file_size=file_size, mime_type=mime_type,
        dest_path=path, upload_mode="chunked", chunk_size=0,
    )
    session["chunk_size"] = session.get("chunk_size") or 0
    return session


@app.post("/api/uploads/{session_id}/chunk")
async def api_upload_chunk(session_id: str, request: Request, offset: int = Query(...)):
    """写入一个浏览器分片。客户端必须按 offset 顺序上传。"""
    data = await request.body()
    return await write_upload_chunk(session_id, offset, data)


@app.post("/api/uploads/{session_id}/pause")
async def api_pause_upload_session(session_id: str):
    return await mark_upload_paused(session_id)


@app.post("/api/uploads/{session_id}/commit")
async def api_commit_upload_session(session_id: str, background_tasks: BackgroundTasks):
    session = await commit_cached_upload(session_id)
    background_tasks.add_task(process_cached_upload, session_id)
    session["accepted"] = True
    return session


@app.get("/api/upload-cache")
async def api_upload_cache_info():
    return await cache_info()


@app.post("/api/upload-cache/cleanup")
async def api_upload_cache_cleanup():
    return await cleanup_upload_cache()


@app.get("/api/uploads")
async def api_uploads(limit: int = Query(50)):
    return {"uploads": await list_upload_sessions(limit=limit)}


@app.get("/api/uploads/{session_id}")
async def api_upload_session(session_id: str):
    return await get_upload_session(session_id)


@app.post("/api/uploads/{session_id}/cancel")
async def api_cancel_upload_session(session_id: str):
    return await cancel_upload_session(session_id)


@app.delete("/api/uploads/{session_id}")
async def api_delete_upload_session(session_id: str):
    return await delete_upload_session(session_id)


@app.post("/api/mkdir")
async def api_mkdir(name: str = Query(...), parent: str = Query("/")):
    """网页端创建目录"""
    from database import DirDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        dir_db = DirDB(db)
        if parent != "/" and not await dir_db.dir_exists(parent):
            raise HTTPException(400, "父目录不存在")
            
        path = await dir_db.create_dir(name, parent)
        if not path:
            raise HTTPException(400, "目录已存在")
        return {"ok": True, "path": path}
    finally:
        await db.close()

@app.post("/api/delete/{file_id}")
async def api_delete(file_id: int):
    """网页端删除索引"""
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        file_db = FileDB(db)
        success = await file_db.delete_file(file_id, deleted_by="webui")
        if not success:
            raise HTTPException(404, "文件不存在或已删除")
        return {"ok": True}
    finally:
        await db.close()
@app.get("/api/trash")
async def api_trash(page: int = Query(1), limit: int = Query(50)):
    """回收站列表。"""
    from database import FileDB
    offset = (page - 1) * limit
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        file_db = FileDB(db)
        rows = await file_db.list_deleted_files(limit=limit, offset=offset)
        total = await file_db.count_deleted_files()
    finally:
        await db.close()
    files = []
    for r in rows:
        files.append({
            "id": r["id"],
            "file_name": r["file_name"],
            "file_size": r["file_size"],
            "size_fmt": format_size(r["file_size"]),
            "mime_type": r["mime_type"],
            "file_type": r["file_type"],
            "path": r["path"],
            "tags": r["tags"],
            "created_at": r["created_at"],
            "deleted_at": r.get("deleted_at", ""),
            "deleted_by": r.get("deleted_by", ""),
        })
    return {"files": files, "total": total, "page": page, "limit": limit, "pages": max(1, -(-total // limit))}


@app.post("/api/trash/{file_id}/restore")
async def api_restore_file(file_id: int):
    """从回收站恢复文件。"""
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        file_db = FileDB(db)
        success = await file_db.restore_file(file_id)
        if not success:
            raise HTTPException(404, "文件不存在或不在回收站")
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/trash/{file_id}")
async def api_purge_file(file_id: int):
    """从回收站彻底删除索引。"""
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        file_db = FileDB(db)
        success = await file_db.purge_file(file_id)
        if not success:
            raise HTTPException(404, "文件不存在或不在回收站")
        return {"ok": True}
    finally:
        await db.close()


@app.get("/api/proxy-download/{file_id}")
async def proxy_download(file_id: int, request: Request):
    """后端代理下载，支持 Range 请求。单文件 / multipart 自动识别。"""
    from starlette.responses import StreamingResponse, Response

    row = await query_db(
        "SELECT id, file_name, file_size, mime_type, is_multipart, chunk_count "
        "FROM files WHERE id=? AND deleted=0",
        (file_id,),
    )
    if not row:
        raise HTTPException(404, "文件不存在")

    f = row[0]
    total = f["file_size"]
    range_header = request.headers.get("range") or request.headers.get("Range")

    headers = {
        "Content-Disposition": f'attachment; filename="{f["file_name"]}"',
        "Content-Type": f["mime_type"] or "application/octet-stream",
        "Accept-Ranges": "bytes",
    }
    if f.get("is_multipart"):
        headers["X-TGDrive-Multipart"] = str(f["chunk_count"])

    # 解析 Range
    start, end = 0, total - 1
    status = 200
    if range_header and range_header.startswith("bytes="):
        try:
            spec = range_header[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            if a:
                start = int(a)
            if b:
                end = int(b)
            if start < 0 or end >= total or start > end:
                return Response(status_code=416,
                                headers={"Content-Range": f"bytes */{total}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        except ValueError:
            pass

    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        stream_file_by_id(file_id, range_start=start, range_end=end),
        headers=headers, status_code=status,
    )


@app.get("/api/upload-status/{file_id}")
async def api_upload_status(file_id: int):
    """查看一个 multipart 上传的进度与缺失分片。"""
    from database import FileDB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT id, file_name, file_size, chunk_count, is_multipart, deleted "
            "FROM files WHERE id=?", (file_id,))
        row = await cursor.fetchone()
        if not row or not row["is_multipart"]:
            raise HTTPException(404, "文件不存在或不是 multipart")
        fdb = FileDB(db)
        missing = await fdb.get_missing_chunks(file_id)
    finally:
        await db.close()
    return {
        "file_id": file_id,
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "chunk_count": row["chunk_count"],
        "missing": missing,
        "complete": len(missing) == 0,
        "deleted": bool(row["deleted"]),
    }


@app.post("/api/resume-upload/{file_id}")
async def api_resume_upload(file_id: int, source_path: str = Query(...)):
    """从本地 source_path 文件重传 file_id 的缺失分片。
    注意：source_path 必须是服务器本地可读路径（临时上传后未订零的供重传用）。
    仅限本地/运维使用。"""
    from database import FileDB
    if not os.path.isfile(source_path):
        raise HTTPException(400, f"source_path 不存在: {source_path}")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        fdb = FileDB(db)
        missing = await fdb.get_missing_chunks(file_id)
    finally:
        await db.close()
    if not missing:
        return {"ok": True, "already_complete": True}
    try:
        return await resume_chunks(file_id, missing, source_path)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/cleanup-orphans")
async def api_cleanup_orphans(hours: int = Query(24), apply: bool = Query(False)):
    """查找 / 清理 hours 小时前未集齐分片的 multipart 文件。默认 dry_run。"""
    return await cleanup_orphans(hours=hours, dry_run=not apply)


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    uvicorn.run(app, host="0.0.0.0", port=port)
