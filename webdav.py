"""TGDrive WebDAV 桥接。

上传：客户端 PUT → 本地临时文件 → 后台 asyncio 任务调用 tg_io.upload_local_file_to_tg
        (自动判定单传 vs 切片)。
下载：get_content() 返回一个类文件对象，该对象内部驱动 tg_io.stream_file_by_id
        (multipart 顺序拼接)。
"""

import os
import io
import asyncio
import threading
import aiosqlite
import logging
import tempfile
from io import BytesIO
from typing import Optional
from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi

import config
from config import DB_PATH, BOT_TOKEN, PROXY, ADMIN_IDS
from database import FileDB, DirDB
from tg_io import upload_local_file_to_tg, upload_stream_to_tg, stream_file_by_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webdav")


def run_async(coro):
    """在当前线程跑一个 coroutine。为了跟 wsgidav（同步）打交道。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 被别人占着了 -- 新起一个
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class _TGStreamReader(io.RawIOBase):
    """把 tg_io.stream_file_by_id（async generator）包装成同步类文件。

    支持 range_start / range_end。
    """

    def __init__(self, file_id_int: int, range_start: int = 0,
                 range_end: Optional[int] = None):
        super().__init__()
        self._file_id = file_id_int
        self._loop = asyncio.new_event_loop()
        self._buf = b""
        self._gen = self._loop.run_until_complete(
            self._open_gen(range_start, range_end))
        self._finished = False

    async def _open_gen(self, lo, hi):
        return stream_file_by_id(self._file_id, range_start=lo,
                                 range_end=hi).__aiter__()

    def _fill(self, want: int) -> bool:
        while len(self._buf) < want and not self._finished:
            try:
                piece = self._loop.run_until_complete(self._gen.__anext__())
                if not piece:
                    continue
                self._buf += piece
            except StopAsyncIteration:
                self._finished = True
        return bool(self._buf)

    def readable(self):
        return True

    def read(self, n=-1):
        if n is None or n < 0:
            # 全读
            while not self._finished:
                try:
                    piece = self._loop.run_until_complete(self._gen.__anext__())
                    if piece:
                        self._buf += piece
                except StopAsyncIteration:
                    self._finished = True
            out, self._buf = self._buf, b""
            return out
        self._fill(n)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        try:
            self._loop.run_until_complete(self._gen.aclose())
        except Exception:
            pass
        try:
            self._loop.close()
        except Exception:
            pass
        super().close()


class TelegramFile(DAVNonCollection):
    def __init__(self, path, environ, file_info):
        super().__init__(path, environ)
        self.file_info = file_info

    def get_content_length(self):
        return self.file_info["file_size"]

    def get_content_type(self):
        return self.file_info["mime_type"] or "application/octet-stream"

    def get_creation_date(self):
        import time
        return time.time()

    def get_last_modified(self):
        import time
        return time.time()

    def support_etag(self):
        return True

    def get_etag(self):
        return f'{self.file_info["id"]}-{self.file_info["file_size"]}'

    def support_ranges(self):
        return True

    def get_content(self):
        """返回同步 file-like，支持 Range。
        wsgidav 会读取 environ、解析 Range 后 seek + read；为了不在本地存
        整个文件，我们部分接管 Range：看 environ['HTTP_RANGE'] 直接起始偏移。
        如果 wsgidav 调用 seek，我们会报错（不支持）；但大多数场景下它会直接迭代。
        """
        env = self.environ or {}
        rng = env.get("HTTP_RANGE") or env.get("http_range") or ""
        lo, hi = 0, None
        if rng.startswith("bytes="):
            try:
                spec = rng[6:].split(",")[0].strip()
                a, _, b = spec.partition("-")
                if a:
                    lo = int(a)
                if b:
                    hi = int(b)
            except ValueError:
                pass
        logger.info("WebDAV download: %s (id=%s, multipart=%s, range=%s-%s)",
                    self.file_info["file_name"], self.file_info["id"],
                    bool(self.file_info.get("is_multipart")), lo, hi)
        return _TGStreamReader(self.file_info["id"], range_start=lo, range_end=hi)

    def delete(self):
        """WebDAV 删除进入回收站，而不是彻底删除索引。"""
        from database import FileDB
        async def do_delete():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                fdb = FileDB(db)
                ok = await fdb.delete_file(self.file_info["id"], deleted_by="webdav")
                if not ok:
                    raise RuntimeError("文件不存在或已删除")
            finally:
                await db.close()
        logger.info("WebDAV move to trash: %s (id=%s)", self.file_info["file_name"], self.file_info["id"])
        run_async(do_delete())


class _StreamingWriter:
    """同步 file-like：WebDAV 客户端 write()、close() 会被调用。

    背后起一个后台线程跑 async upload_stream_to_tg，读端从 queue 取数据。
    """

    def __init__(self, file_name: str, mime_type: str, dest_path: str):
        import queue, threading
        self.file_name = file_name
        self.mime_type = mime_type
        self.dest_path = dest_path
        self._q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=8)
        self._buf = b""
        self._result = None
        self._error = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- WebDAV 会调用的同步接口 ----
    def write(self, data: bytes):
        if self._error:
            raise self._error
        if not data:
            return
        self._q.put(bytes(data))

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._q.put(None)  # EOF
        self._thread.join()
        if self._error:
            raise self._error

    # ---- 后台 ----
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def reader(n: int) -> bytes:
            # 返回 ≤ n 字节；不够就从 queue 拉
            if not self._buf:
                item = await loop.run_in_executor(None, self._q.get)
                if item is None:
                    return b""
                self._buf = item
            out, self._buf = self._buf[:n], self._buf[n:]
            return out

        try:
            self._result = loop.run_until_complete(upload_stream_to_tg(
                reader=reader, file_name=self.file_name,
                mime_type=self.mime_type, dest_path=self.dest_path,
                uploader_id=ADMIN_IDS[0],
            ))
            logger.info("WebDAV streaming upload done: %s mode=%s size=%s",
                        self.file_name, self._result.get("mode"),
                        self._result.get("size"))
        except Exception as e:
            self._error = e
            logger.error("WebDAV streaming upload failed: %s", e)
        finally:
            loop.close()

class TelegramFolder(DAVCollection):
    def __init__(self, path, environ, db_path):
        super().__init__(path, environ)
        self.db_path = db_path  # 网盘内的逻辑路径
        
    def support_recursive_delete(self):
        return False

    def delete(self):
        # 目录本身没有 deleted 标记；暂不支持 WebDAV 删除目录，避免误删整棵树。
        from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
        raise DAVError(HTTP_FORBIDDEN, "目录删除暂不支持；文件删除会进入回收站")
        
    def begin_write(self, content_type=None):
        """WebDAV上传：先写入本地临时文件，关闭后由系统触发后台TG上传"""
        # 由于我们无法预先知道文件名（begin_write在新建文件时发生），
        # 实际的文件名在DAV中体现为这个 resource 被创建。
        # 但在 wsgidav 中，客户端通常是PUT一个新的路径，导致一个新的DAVNonCollection被创建。
        # 文件夹级别的 begin_write 理论上不常用。
        raise NotImplementedError()

    def get_member_names(self):
        names = []
        async def fetch():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                # 获取子目录
                cursor = await db.execute("SELECT name FROM dirs WHERE parent_path=?", (self.db_path,))
                for row in await cursor.fetchall():
                    names.append(row["name"])
                
                # 获取文件
                cursor = await db.execute("SELECT file_name FROM files WHERE path=? AND deleted=0", (self.db_path,))
                for row in await cursor.fetchall():
                    names.append(row["file_name"])
            finally:
                await db.close()
        
        run_async(fetch())
        return names

    def get_member(self, name):
        # 根据名字查找是目录还是文件
        child_path = self.path + name if self.path.endswith("/") else self.path + "/" + name
        logic_path = self.db_path + name if self.db_path.endswith("/") else self.db_path + "/" + name
        
        file_info = None
        is_dir = False
        
        async def fetch():
            nonlocal file_info, is_dir
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                # 查目录
                cursor = await db.execute("SELECT 1 FROM dirs WHERE path=?", (logic_path,))
                if await cursor.fetchone():
                    is_dir = True
                    return
                
                # 查文件
                cursor = await db.execute("SELECT * FROM files WHERE path=? AND file_name=? AND deleted=0", (self.db_path, name))
                row = await cursor.fetchone()
                if row:
                    file_info = dict(row)
            finally:
                await db.close()

        run_async(fetch())
        
        if is_dir:
            return TelegramFolder(child_path, self.environ, logic_path)
        elif file_info:
            return TelegramFile(child_path, self.environ, file_info)
        return None


class TGDriveProvider(DAVProvider):
    def __init__(self):
        super().__init__()
        self._count_get_inst = 0

    def get_resource_inst(self, path, environ):
        self._count_get_inst += 1
        
        if path == "/":
            return TelegramFolder("/", environ, "/")
            
        # 解析路径
        parts = [p for p in path.split("/") if p]
        if not parts:
            return TelegramFolder("/", environ, "/")
            
        # 逐层向下找
        curr = TelegramFolder("/", environ, "/")
        for i, part in enumerate(parts):
            # 对最后一个节点特殊处理：如果是 PUT，允许创建
            if i == len(parts) - 1:
                next_curr = curr.get_member(part)
                if not next_curr:
                    if environ["REQUEST_METHOD"] == "PUT":
                        child_path = curr.path + part if curr.path.endswith("/") else curr.path + "/" + part
                        return TempUploadFile(child_path, environ, curr.db_path, part)
                    return None
                curr = next_curr
            else:
                curr = curr.get_member(part)
                if not curr:
                    return None
            
        return curr
        
    def get_custom_property(self, path, name):
        return None
        
    def set_custom_property(self, path, name, value):
        return None

class TempUploadFile(DAVNonCollection):
    """WebDAV 上传资源：流式切片上传，不落盘。"""

    def __init__(self, path, environ, db_path, file_name):
        super().__init__(path, environ)
        self.db_path = db_path
        self.file_name = file_name
        self._writer: Optional[_StreamingWriter] = None

    def get_content_length(self):
        return 0  # 未知

    def get_content_type(self):
        import mimetypes
        t, _ = mimetypes.guess_type(self.file_name)
        return t or "application/octet-stream"

    def get_creation_date(self):
        import time
        return time.time()

    def get_last_modified(self):
        import time
        return time.time()

    def support_etag(self):
        return False

    def get_etag(self):
        return None

    def support_ranges(self):
        return False

    def get_content(self):
        return BytesIO(b"")

    def begin_write(self, content_type=None):
        """返回一个同步 file-like、边写边发给 Telegram。"""
        import mimetypes
        mime, _ = mimetypes.guess_type(self.file_name)
        mime = mime or content_type or "application/octet-stream"
        self._writer = _StreamingWriter(self.file_name, mime, self.db_path)
        return self._writer

    def end_write(self, with_errors):
        if not self._writer:
            return
        if with_errors:
            logger.warning("WebDAV upload aborted by client: %s", self.file_name)
            try:
                self._writer.close()
            except Exception:
                pass
            return
        try:
            self._writer.close()  # 同步等待后台上传完成
        except Exception as e:
            logger.error("WebDAV end_write error: %s", e)
            raise

def patch_folder_for_upload():
    # 猴子补丁：让 TelegramFolder 支持客户端在其内创建新文件
    def get_member_or_create(self, name):
        res = self.get_member(name)
        if res:
            return res
        # 找不到，我们假设是要新建的上传文件，返回一个临时的对象接收写入
        child_path = self.path + name if self.path.endswith("/") else self.path + "/" + name
        return TempUploadFile(child_path, self.environ, self.db_path, name)
        
    TelegramFolder.get_member_or_create = get_member_or_create

patch_folder_for_upload()


def create_webdav_app() -> WsgiDAVApp:
    """创建 WsgiDAV WSGI 应用。

    可被 webui.py 通过 FastAPI/Starlette 的 WSGIMiddleware 挂载到
    同一个 HTTP 端口（默认路径 /dav），也可继续由本文件独立启动。
    """
    config = {
        "provider_mapping": {
            "/": TGDriveProvider(),
        },
        "simple_dc": {
            "user_mapping": {
                "*": True  # 允许匿名访问，为了安全可以加账号密码
            }
        },
        "logging": {
            "enable": True,
        },
    }
    return WsgiDAVApp(config)


def start_webdav():
    """兼容旧部署：单独在 8081 端口启动 WebDAV。"""
    host = "0.0.0.0"
    port = 8081
    app = create_webdav_app()
    server = wsgi.Server(
        bind_addr=(host, port),
        wsgi_app=app,
    )
    logger.info(f"WebDAV server running on port {port}")
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("WebDAV server stopped")
        server.stop()

if __name__ == "__main__":
    start_webdav()
