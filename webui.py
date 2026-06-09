"""TGDrive Web UI - FastAPI 后端"""

import os
import aiosqlite
from pathlib import Path
from fastapi import FastAPI, Query, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import config
from config import DB_PATH, BOT_TOKEN, PROXY, ADMIN_IDS, MAX_FILE_SIZE

app = FastAPI(title="TGDrive Web")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
async def api_upload(file: UploadFile, path: str = "/"):
    """网页端上传文件"""
    import aiohttp
    import tempfile
    
    # 验证文件名
    filename = file.filename or "upload.bin"
    mime_type = file.content_type or "application/octet-stream"
    
    # 保存到临时文件
    fd, tmp_path = tempfile.mkstemp()
    try:
        size = 0
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1MB 块
                f.write(chunk)
                size += len(chunk)
                
        if size > MAX_FILE_SIZE * 1024 * 1024:
            raise HTTPException(400, f"文件过大，最大允许 {MAX_FILE_SIZE}MB")
            
        if size > 50 * 1024 * 1024:
            # Bot API 官方限制直接上传不能超过 50MB
            # 除非自建 API Server
            raise HTTPException(400, "Web直传目前受Bot API限制，请勿超过50MB。大于50MB请在Telegram中发给Bot。")

        # 使用 form-data 上传到 Telegram
        proxy = PROXY or None
        async with aiohttp.ClientSession() as s:
            with open(tmp_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", str(ADMIN_IDS[0])) # 默认传给第一个管理员
                data.add_field("document", f, filename=filename, content_type=mime_type)
                
                # caption 用于让 bot 识别路径
                if path != "/":
                    data.add_field("caption", f"{path} (Web上传)")
                else:
                    data.add_field("caption", "来自 Web 的上传")

                async with s.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data=data,
                    proxy=proxy,
                    timeout=300
                ) as resp:
                    res = await resp.json()
                    
        if not res.get("ok"):
            raise HTTPException(400, f"上传到 Telegram 失败: {res.get('description', '')}")
            
        msg = res.get("result", {})
        doc = msg.get("document", {})
        if not doc:
            raise HTTPException(400, "未收到文档对象")
            
        file_id = doc.get("file_id")
        file_unique_id = doc.get("file_unique_id")
        tg_size = doc.get("file_size", size)
        
        # 写入数据库 (Bot polling 会收到，但由于我们用 bot 自己发给自己，可能会被忽略，或者重复记录，所以最好主动写入)
        from database import FileDB
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            file_db = FileDB(db)
            
            # 确定类型
            file_type = "document"
            if mime_type.startswith("image/"): file_type = "photo"
            elif mime_type.startswith("video/"): file_type = "video"
            elif mime_type.startswith("audio/"): file_type = "audio"
            
            # 插入
            await file_db.add_file(
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_name=filename,
                file_size=tg_size,
                mime_type=mime_type,
                file_type=file_type,
                path=path,
                uploader_id=ADMIN_IDS[0], # web 默认 admin 0
                message_id=msg.get("message_id", 0),
                chat_id=msg.get("chat", {}).get("id", 0)
            )
        finally:
            await db.close()
            
        return {"ok": True, "file_name": filename, "size": tg_size, "path": path}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        os.close(fd)
        os.remove(tmp_path)


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
        success = await file_db.delete_file(file_id)
        if not success:
            raise HTTPException(404, "文件不存在或已删除")
        return {"ok": True}
    finally:
        await db.close()
@app.get("/api/proxy-download/{file_id}")
@app.get("/api/proxy-download/{file_id}")
async def proxy_download(file_id: int):
    """通过后端代理下载文件（适合国内网络）"""
    import aiohttp
    from starlette.responses import StreamingResponse

    row = await query_db(
        "SELECT file_id, file_name, file_size, mime_type FROM files WHERE id=? AND deleted=0",
        (file_id,),
    )
    if not row:
        raise HTTPException(404, "文件不存在")

    f = row[0]
    proxy = PROXY or None
    
    # We must return a StreamingResponse that yields chunks.
    # To do this safely with aiohttp session, we wrap it in an async generator.
    async def stream_telegram_file():
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                json={"file_id": f["file_id"]},
                proxy=proxy,
            ) as resp:
                data = await resp.json()
            if not data.get("ok"):
                yield b""
                return

            tg_path = data["result"]["file_path"]
            tg_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_path}"

            async with s.get(tg_url, proxy=proxy) as tg_resp:
                async for chunk in tg_resp.content.iter_chunked(64 * 1024):
                    yield chunk

    return StreamingResponse(
        stream_telegram_file(),
        headers={
            "Content-Disposition": f'attachment; filename="{f["file_name"]}"',
            "Content-Type": f["mime_type"] or "application/octet-stream",
            "Content-Length": str(f["file_size"]),
        },
    )


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    uvicorn.run(app, host="0.0.0.0", port=port)
