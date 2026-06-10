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
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN, HTTP_CONFLICT
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi

import config
from config import DB_PATH, BOT_TOKEN, PROXY, ADMIN_IDS
from database import FileDB, DirDB
from tg_io import upload_local_file_to_tg, upload_stream_to_tg, stream_file_by_id
from utils import normalize_path

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
    """把 tg_io.stream_file_by_id（async generator）包装成同步 seekable 文件。

    WsgiDAV 在处理普通 GET / Range GET 时会调用 seek()/tell()。这里的 seek
    会重新打开底层 Telegram 流，从新的绝对偏移开始读取，避免本地缓存整文件。
    """

    def __init__(self, file_id_int: int, file_size: int):
        super().__init__()
        self._file_id = file_id_int
        self._file_size = int(file_size or 0)
        self._loop = asyncio.new_event_loop()
        self._buf = b""
        self._pos = 0
        self._gen = None
        self._finished = False
        self._open_at(0)

    async def _open_gen(self, lo):
        return stream_file_by_id(self._file_id, range_start=lo).__aiter__()

    def _close_gen(self):
        if self._gen is None:
            return
        try:
            self._loop.run_until_complete(self._gen.aclose())
        except Exception:
            pass
        self._gen = None

    def _open_at(self, pos: int):
        self._close_gen()
        self._buf = b""
        self._pos = max(0, min(int(pos), self._file_size))
        self._finished = self._pos >= self._file_size
        if not self._finished:
            self._gen = self._loop.run_until_complete(self._open_gen(self._pos))

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

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = self._pos + offset
        elif whence == os.SEEK_END:
            new_pos = self._file_size + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        self._open_at(new_pos)
        return self._pos

    def read(self, n=-1):
        if self.closed:
            return b""
        if n is None or n < 0:
            chunks = []
            if self._buf:
                chunks.append(self._buf)
                self._pos += len(self._buf)
                self._buf = b""
            while not self._finished:
                try:
                    piece = self._loop.run_until_complete(self._gen.__anext__())
                    if piece:
                        chunks.append(piece)
                        self._pos += len(piece)
                except StopAsyncIteration:
                    self._finished = True
            return b"".join(chunks)

        self._fill(n)
        out, self._buf = self._buf[:n], self._buf[n:]
        self._pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        self._close_gen()
        try:
            self._loop.close()
        except Exception:
            pass
        super().close()


class TelegramFile(DAVNonCollection):
    def __init__(self, path, environ, file_info):
        super().__init__(path, environ)
        self.file_info = file_info
        self._writer: Optional[_StreamingWriter] = None

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
        """返回同步 seekable file-like，WsgiDAV 负责 Range seek/read。"""
        logger.info("WebDAV download: %s (id=%s, multipart=%s)",
                    self.file_info["file_name"], self.file_info["id"],
                    bool(self.file_info.get("is_multipart")))
        return _TGStreamReader(self.file_info["id"], self.file_info["file_size"])

    def begin_write(self, content_type=None):
        """WebDAV PUT 到已有文件时，直接覆盖同名旧文件。"""
        import mimetypes
        mime, _ = mimetypes.guess_type(self.file_info["file_name"])
        mime = mime or content_type or "application/octet-stream"
        self._writer = _StreamingWriter(
            self.file_info["file_name"],
            mime,
            self.file_info["path"],
            overwrite=True,
        )
        return self._writer

    def end_write(self, with_errors):
        if not self._writer:
            return
        if with_errors:
            logger.warning("WebDAV overwrite aborted by client: %s", self.file_info["file_name"])
            try:
                self._writer.close()
            except Exception:
                pass
            return
        try:
            self._writer.close()
        except Exception as e:
            logger.error("WebDAV overwrite end_write error: %s", e)
            raise

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
    """同步 file-like：WebDAV PUT 先落临时文件，再交给 tg_io 上传。

    在 LOCAL_API_MODE=true 时，upload_local_file_to_tg 会把临时文件路径直接交给
    本地 Bot API Server，避免把大文件按分片读进 Python 内存。
    """

    def __init__(self, file_name: str, mime_type: str, dest_path: str, overwrite: bool = False):
        self.file_name = file_name
        self.mime_type = mime_type
        self.dest_path = dest_path
        self.overwrite = overwrite
        self._error = None
        self._closed = False
        suffix = os.path.splitext(file_name)[1] or ".upload"
        tmp = tempfile.NamedTemporaryFile(prefix="tgdisk-webdav-", suffix=suffix, delete=False)
        self._tmp_path = tmp.name
        self._file = tmp

    # ---- WebDAV 会调用的同步接口 ----
    def write(self, data: bytes):
        if self._error:
            raise self._error
        if not data:
            return
        self._file.write(data)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
            self._file.close()
            self._result = run_async(upload_local_file_to_tg(
                local_path=self._tmp_path,
                file_name=self.file_name,
                mime_type=self.mime_type,
                dest_path=self.dest_path,
                uploader_id=ADMIN_IDS[0],
                overwrite=self.overwrite,
            ))
            logger.info("WebDAV temp-file upload done: %s mode=%s size=%s",
                        self.file_name, self._result.get("mode"),
                        self._result.get("size"))
        except Exception as e:
            self._error = e
            logger.error("WebDAV temp-file upload failed: %s", e)
            raise
        finally:
            try:
                os.unlink(self._tmp_path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("WebDAV temp cleanup failed %s: %s", self._tmp_path, e)

class TelegramFolder(DAVCollection):
    def __init__(self, path, environ, db_path):
        super().__init__(path, environ)
        self.db_path = db_path  # 网盘内的逻辑路径
        
    def support_recursive_delete(self):
        return True

    def delete(self):
        """WebDAV 删除目录：递归删除目录记录，目录内文件进入回收站。"""
        if self.db_path == "/":
            raise DAVError(HTTP_FORBIDDEN, "根目录不能删除")

        async def do_delete_dir():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                dir_db = DirDB(db)
                files, dirs = await dir_db.delete_dir_recursive(self.db_path, deleted_by="webdav")
                if dirs == 0:
                    raise DAVError(HTTP_CONFLICT, "目录不存在")
                return files, dirs
            finally:
                await db.close()

        files, dirs = run_async(do_delete_dir())
        logger.info("WebDAV delete dir: %s (files=%s dirs=%s)", self.db_path, files, dirs)

    def create_collection(self, name):
        """WebDAV MKCOL：在当前目录下创建子目录。"""
        assert self.is_collection
        name = (name or "").strip().strip("/")
        if not name or "/" in name or name in (".", ".."):
            raise DAVError(HTTP_CONFLICT, "非法目录名")

        async def do_create():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                dir_db = DirDB(db)
                parent_path = normalize_path(self.db_path)
                if not await dir_db.dir_exists(parent_path):
                    raise DAVError(HTTP_CONFLICT, "父目录不存在")
                created_path = await dir_db.create_dir(name, parent_path)
                if not created_path:
                    raise DAVError(HTTP_FORBIDDEN, "目录已存在")
                return created_path
            finally:
                await db.close()

        created_path = run_async(do_create())
        logger.info("WebDAV mkdir: %s", created_path)
        return TelegramFolder(
            self.path + name if self.path.endswith("/") else self.path + "/" + name,
            self.environ,
            created_path,
        )
        
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
        request_method = environ.get("REQUEST_METHOD", "")
        
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
                    if request_method == "PUT":
                        child_path = curr.path + part if curr.path.endswith("/") else curr.path + "/" + part
                        return TempUploadFile(child_path, environ, curr.db_path, part)
                    # MKCOL 必须让 WsgiDAV 看到目标 URL 尚未映射，随后由父目录
                    # create_collection() 创建；这里不能返回临时文件对象。
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
        self._writer = _StreamingWriter(self.file_name, mime, self.db_path, overwrite=True)
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
