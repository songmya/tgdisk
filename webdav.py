"""TGDrive WebDAV 桥接"""

import os
import io
import asyncio
import aiosqlite
import logging
from io import BytesIO
from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot import wsgi
from wsgidav.fs_dav_provider import FilesystemProvider

import config
from config import DB_PATH, BOT_TOKEN, PROXY
from database import FileDB, DirDB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webdav")


# 因为 WsgiDAV 是同步框架，而我们的数据库和下载是异步的，需要一个事件循环
# 这里我们创建一个简单的同步包装
def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TelegramFile(DAVNonCollection):
    def __init__(self, path, environ, file_info):
        super().__init__(path, environ)
        self.file_info = file_info

    def get_content_length(self):
        return self.file_info["file_size"]

    def get_content_type(self):
        return self.file_info["mime_type"] or "application/octet-stream"

    def get_creation_date(self):
        # 简化处理，实际应该解析时间字符串
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
        return False  # 流式代理比较难做完美 Range，暂时设为 False

    def get_content(self):
        """同步下载文件并返回字节流。
        注意：对于大文件，这样会将其全部读入内存。
        对于 WebDAV 这是个妥协方案，更好的做法是自己实现流式生成器，
        但结合异步请求和同步的 WSGI 比较复杂。"""
        import requests
        
        # 1. 拿文件路径
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
        proxies = {"http": PROXY, "https": PROXY} if PROXY else None
        
        try:
            res = requests.post(api_url, json={"file_id": self.file_info["file_id"]}, proxies=proxies, timeout=10).json()
            if not res.get("ok"):
                logger.error(f"Failed to get file path: {res}")
                return BytesIO(b"")
                
            tg_path = res["result"]["file_path"]
            dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_path}"
            
            # 2. 下载内容 (对于小文件可行，大文件可能会爆内存)
            # 在实际中 WebDAV 这里最好用生成器 yield
            logger.info(f"WebDAV Downloading: {self.file_info['file_name']}")
            r = requests.get(dl_url, proxies=proxies, stream=True)
            return r.raw # 返回一个类文件对象给 WsgiDAV
            
        except Exception as e:
            logger.error(f"Download error: {e}")
            return BytesIO(b"")


class WebDAVUploadTask:
    def __init__(self, db_path, file_name, temp_file_path):
        self.db_path = db_path
        self.file_name = file_name
        self.temp_file_path = temp_file_path
        
    async def execute(self):
        import aiohttp
        from database import FileDB
        import mimetypes
        
        logger.info(f"WebDAV background upload started: {self.file_name} -> {self.db_path}")
        try:
            file_size = os.path.getsize(self.temp_file_path)
            mime_type, _ = mimetypes.guess_type(self.file_name)
            mime_type = mime_type or "application/octet-stream"
            
            # 确定类型
            file_type = "document"
            if mime_type.startswith("image/"): file_type = "photo"
            elif mime_type.startswith("video/"): file_type = "video"
            elif mime_type.startswith("audio/"): file_type = "audio"
            
            # 使用代理上传给 TG
            proxy = PROXY or None
            
            # 发送大文件可能超时
            timeout = aiohttp.ClientTimeout(total=3600)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                with open(self.temp_file_path, "rb") as f:
                    data = aiohttp.FormData()
                    from config import ADMIN_IDS
                    # 传给管理员0
                    data.add_field("chat_id", str(ADMIN_IDS[0]))
                    data.add_field("document", f, filename=self.file_name, content_type=mime_type)
                    data.add_field("caption", f"{self.db_path} (WebDAV上传)")
                    
                    logger.info(f"Uploading {self.file_name} to Telegram via API...")
                    async with s.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                        data=data,
                        proxy=proxy
                    ) as resp:
                        res = await resp.json()
                        
            if not res.get("ok"):
                logger.error(f"WebDAV Telegram API upload failed: {res}")
                return
                
            msg = res.get("result", {})
            doc = msg.get("document", {})
            if not doc:
                logger.error("WebDAV API response has no document")
                return
                
            file_id = doc.get("file_id")
            file_unique_id = doc.get("file_unique_id")
            tg_size = doc.get("file_size", file_size)
            
            # 写入 DB
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                file_db = FileDB(db)
                await file_db.add_file(
                    file_id=file_id,
                    file_unique_id=file_unique_id,
                    file_name=self.file_name,
                    file_size=tg_size,
                    mime_type=mime_type,
                    file_type=file_type,
                    path=self.db_path,
                    uploader_id=ADMIN_IDS[0],
                    message_id=msg.get("message_id", 0),
                    chat_id=msg.get("chat", {}).get("id", 0)
                )
                logger.info(f"WebDAV upload success & recorded: {self.file_name}")
            finally:
                await db.close()
                
        except Exception as e:
            logger.error(f"WebDAV upload exception: {e}")
        finally:
            try:
                os.remove(self.temp_file_path)
            except:
                pass

class TelegramFolder(DAVCollection):
    def __init__(self, path, environ, db_path):
        super().__init__(path, environ)
        self.db_path = db_path  # 网盘内的逻辑路径
        
    def support_recursive_delete(self):
        return False
        
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
    def __init__(self, path, environ, db_path, file_name):
        super().__init__(path, environ)
        self.db_path = db_path
        self.file_name = file_name
        import tempfile
        fd, self.temp_path = tempfile.mkstemp(prefix="tgdrive_")
        os.close(fd)
        
    def get_content_length(self):
        return os.path.getsize(self.temp_path)
        
    def get_content_type(self):
        import mimetypes
        t, _ = mimetypes.guess_type(self.file_name)
        return t or "application/octet-stream"
        
    def get_creation_date(self):
        return os.path.getmtime(self.temp_path)
        
    def get_last_modified(self):
        return os.path.getmtime(self.temp_path)
        
    def support_etag(self):
        return False
        
    def get_etag(self):
        return None
        
    def support_ranges(self):
        return False
        
    def get_content(self):
        return open(self.temp_path, "rb")
        
    def begin_write(self, content_type=None):
        """客户端上传时调用，返回一个 file-like object 供写入"""
        return open(self.temp_path, "wb")
        
    def end_write(self, with_errors):
        """写入完成后调用，触发后台上传"""
        if with_errors:
            try:
                os.remove(self.temp_path)
            except:
                pass
            return
            
        # 触发后台异步任务上传到 TG
        task = WebDAVUploadTask(self.db_path, self.file_name, self.temp_path)
        # 因为 wsgidav 跑在同步线程，这里直接用 asyncio 创建一个新的 loop 执行上传任务
        import threading
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(task.execute())
            loop.close()
            
        t = threading.Thread(target=_run)
        t.start()

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


def start_webdav():
    config = {
        "host": "0.0.0.0",
        "port": 8081,
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
    
    app = WsgiDAVApp(config)
    server = wsgi.Server(
        bind_addr=(config["host"], config["port"]),
        wsgi_app=app,
    )
    logger.info(f"WebDAV server running on port {config['port']}")
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("WebDAV server stopped")
        server.stop()

if __name__ == "__main__":
    start_webdav()
