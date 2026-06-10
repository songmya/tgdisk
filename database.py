"""TGDrive 数据库模块"""

import aiosqlite
from pathlib import Path
from config import DB_PATH


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """初始化数据库表"""
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                role TEXT DEFAULT 'user' CHECK(role IN ('admin', 'user', 'banned')),
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                last_active TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_name TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                mime_type TEXT,
                file_type TEXT DEFAULT 'document',
                path TEXT DEFAULT '/',
                tags TEXT DEFAULT '',
                uploader_id INTEGER NOT NULL,
                message_id INTEGER,
                chat_id INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                deleted INTEGER DEFAULT 0,
                deleted_at TEXT DEFAULT '',
                deleted_by TEXT DEFAULT '',
                is_multipart INTEGER DEFAULT 0,
                chunk_count INTEGER DEFAULT 0,
                total_sha256 TEXT DEFAULT '',
                FOREIGN KEY (uploader_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS file_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id_int INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                tg_file_id TEXT NOT NULL,
                tg_file_unique_id TEXT,
                chunk_size INTEGER NOT NULL,
                chunk_sha256 TEXT DEFAULT '',
                status TEXT DEFAULT 'ok',
                message_id INTEGER,
                chat_id INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(file_id_int, chunk_index),
                FOREIGN KEY (file_id_int) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file ON file_chunks(file_id_int, chunk_index);

            CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
            CREATE INDEX IF NOT EXISTS idx_files_name ON files(file_name);
            CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(deleted);
            CREATE INDEX IF NOT EXISTS idx_files_uploader ON files(uploader_id);
            CREATE INDEX IF NOT EXISTS idx_files_tags ON files(tags);

            CREATE TABLE IF NOT EXISTS dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                parent_path TEXT DEFAULT '/',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_dirs_path ON dirs(path);
            CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(parent_path);
        """)

        # 老库升级：为已存在的 files 表补上 multipart 列（幂等）
        async def _ensure_column(table: str, col: str, ddl: str):
            cursor = await db.execute(f"PRAGMA table_info({table})")
            cols = [row[1] for row in await cursor.fetchall()]
            if col not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        await _ensure_column("files", "deleted_at", "deleted_at TEXT DEFAULT ''")
        await _ensure_column("files", "deleted_by", "deleted_by TEXT DEFAULT ''")
        await _ensure_column("files", "is_multipart", "is_multipart INTEGER DEFAULT 0")
        await _ensure_column("files", "chunk_count", "chunk_count INTEGER DEFAULT 0")
        await _ensure_column("files", "total_sha256", "total_sha256 TEXT DEFAULT ''")
        await _ensure_column("file_chunks", "status", "status TEXT DEFAULT 'ok'")

        await db.commit()
    finally:
        await db.close()


class FileDB:
    """文件数据库操作"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def add_file(self, file_id: str, file_unique_id: str, file_name: str,
                       file_size: int, mime_type: str, file_type: str,
                       path: str, uploader_id: int,
                       message_id: int = 0, chat_id: int = 0, tags: str = "") -> int:
        """添加文件记录"""
        await self.db.execute(
            """INSERT INTO files (file_id, file_unique_id, file_name, file_size,
               mime_type, file_type, path, tags, uploader_id, message_id, chat_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id, file_unique_id, file_name, file_size, mime_type, file_type,
             path, tags, uploader_id, message_id, chat_id)
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return row[0]

    async def get_file(self, file_id_int: int) -> dict | None:
        """按 ID 获取文件"""
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE id = ? AND deleted = 0", (file_id_int,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_files(self, path: str = "/", limit: int = 20, offset: int = 0) -> list[dict]:
        """列出目录下的文件"""
        cursor = await self.db.execute(
            """SELECT * FROM files WHERE path = ? AND deleted = 0
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (path, limit, offset)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def find_all_by_path_name(self, path: str, file_name: str) -> list[dict]:
        """查找同目录同名的所有未删除文件记录。"""
        cursor = await self.db.execute(
            """SELECT * FROM files WHERE path = ? AND file_name = ? AND deleted = 0
               ORDER BY id DESC""",
            (path, file_name),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_files(self, path: str = "/") -> int:
        """统计目录下文件数"""
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM files WHERE path = ? AND deleted = 0", (path,)
        )
        row = await cursor.fetchone()
        return row[0]

    async def search_files(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索文件"""
        cursor = await self.db.execute(
            """SELECT * FROM files
               WHERE (file_name LIKE ? OR tags LIKE ?) AND deleted = 0
               ORDER BY created_at DESC LIMIT ?""",
            (f"%{keyword}%", f"%{keyword}%", limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_file(self, file_id_int: int, deleted_by: str = "webui") -> bool:
        """软删除文件：进入回收站。"""
        cursor = await self.db.execute(
            """UPDATE files
               SET deleted = 1, deleted_at = datetime('now', 'localtime'), deleted_by = ?
               WHERE id = ? AND deleted = 0""",
            (deleted_by, file_id_int),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def restore_file(self, file_id_int: int) -> bool:
        """从回收站恢复文件。"""
        cursor = await self.db.execute(
            """UPDATE files
               SET deleted = 0, deleted_at = '', deleted_by = ''
               WHERE id = ? AND deleted = 1""",
            (file_id_int,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def purge_file(self, file_id_int: int) -> bool:
        """从数据库中彻底删除索引；Telegram 服务器上的文件无法保证删除。"""
        cursor = await self.db.execute(
            "DELETE FROM files WHERE id = ? AND deleted = 1", (file_id_int,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_index(self, file_id_int: int) -> bool:
        """彻底删除文件索引，不要求文件已在回收站。"""
        cursor = await self.db.execute(
            "DELETE FROM files WHERE id = ?", (file_id_int,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def list_deleted_files(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """列出回收站文件。"""
        cursor = await self.db.execute(
            """SELECT * FROM files WHERE deleted = 1
               ORDER BY COALESCE(NULLIF(deleted_at,''), created_at) DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_deleted_files(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM files WHERE deleted = 1")
        row = await cursor.fetchone()
        return row[0]

    async def move_file(self, file_id_int: int, new_path: str) -> bool:
        """移动文件到新路径"""
        cursor = await self.db.execute(
            "UPDATE files SET path = ? WHERE id = ? AND deleted = 0",
            (new_path, file_id_int)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def tag_file(self, file_id_int: int, tags: str) -> bool:
        """给文件打标签"""
        cursor = await self.db.execute(
            "UPDATE files SET tags = ? WHERE id = ? AND deleted = 0",
            (tags, file_id_int)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def stats(self) -> dict:
        """统计信息"""
        cursor = await self.db.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(file_size), 0) as total_size "
            "FROM files WHERE deleted = 0"
        )
        row = await cursor.fetchone()
        return dict(row)

    async def add_multipart_file(self, file_name: str, file_size: int, mime_type: str,
                                  file_type: str, path: str, uploader_id: int,
                                  chunk_count: int, total_sha256: str,
                                  tags: str = "") -> int:
        """创建多分片主文件记录（file_id 为第一个分片的 file_id，后续 update）"""
        await self.db.execute(
            """INSERT INTO files (file_id, file_unique_id, file_name, file_size,
               mime_type, file_type, path, tags, uploader_id, message_id, chat_id,
               is_multipart, chunk_count, total_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            ("", "", file_name, file_size, mime_type, file_type, path, tags,
             uploader_id, 0, 0, chunk_count, total_sha256)
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return row[0]

    async def add_chunk(self, file_id_int: int, chunk_index: int,
                        tg_file_id: str, tg_file_unique_id: str,
                        chunk_size: int, chunk_sha256: str,
                        message_id: int = 0, chat_id: int = 0) -> int:
        """插入一个分片记录"""
        await self.db.execute(
            """INSERT INTO file_chunks (file_id_int, chunk_index, tg_file_id,
               tg_file_unique_id, chunk_size, chunk_sha256, message_id, chat_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id_int, chunk_index, tg_file_id, tg_file_unique_id,
             chunk_size, chunk_sha256, message_id, chat_id)
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return row[0]

    async def get_chunks(self, file_id_int: int) -> list[dict]:
        """按顺序获取一个多分片文件的所有分片"""
        cursor = await self.db.execute(
            """SELECT * FROM file_chunks WHERE file_id_int = ?
               ORDER BY chunk_index ASC""",
            (file_id_int,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_missing_chunks(self, file_id_int: int) -> list[int]:
        """返回一个多分片文件中缺失 / 失败的 chunk_index 列表。"""
        cursor = await self.db.execute(
            "SELECT chunk_count FROM files WHERE id=? AND is_multipart=1",
            (file_id_int,))
        row = await cursor.fetchone()
        if not row:
            return []
        total = row[0]
        cursor = await self.db.execute(
            "SELECT chunk_index FROM file_chunks WHERE file_id_int=? AND status='ok'",
            (file_id_int,))
        present = {r[0] for r in await cursor.fetchall()}
        return [i for i in range(total) if i not in present]

    async def mark_chunk_status(self, file_id_int: int, chunk_index: int, status: str):
        await self.db.execute(
            "UPDATE file_chunks SET status=? WHERE file_id_int=? AND chunk_index=?",
            (status, file_id_int, chunk_index))
        await self.db.commit()

    async def list_orphan_multipart(self, hours: int = 24) -> list[dict]:
        """列出超过 hours 小时仍未集齐分片的 multipart 记录。hours<0 仅供测试。"""
        modifier = f'{-abs(hours)} hours' if hours >= 0 else f'+{abs(hours)} hours'
        cursor = await self.db.execute(
            """SELECT f.id, f.file_name, f.file_size, f.chunk_count, f.created_at,
                      COUNT(CASE WHEN c.status='ok' THEN 1 END) AS got
               FROM files f LEFT JOIN file_chunks c ON c.file_id_int=f.id
               WHERE f.is_multipart=1 AND f.deleted=0
                 AND datetime(f.created_at) <= datetime('now','localtime', ?)
               GROUP BY f.id
               HAVING got < f.chunk_count""",
            (modifier,))
        return [dict(r) for r in await cursor.fetchall()]


class DirDB:
    """目录数据库操作"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create_dir(self, name: str, parent_path: str = "/") -> str | None:
        """创建目录"""
        path = f"{parent_path.rstrip('/')}/{name}"
        try:
            await self.db.execute(
                "INSERT INTO dirs (name, path, parent_path) VALUES (?, ?, ?)",
                (name, path, parent_path)
            )
            await self.db.commit()
            return path
        except aiosqlite.IntegrityError:
            return None  # 已存在

    async def list_dirs(self, parent_path: str = "/") -> list[dict]:
        """列出子目录"""
        cursor = await self.db.execute(
            "SELECT * FROM dirs WHERE parent_path = ? ORDER BY name",
            (parent_path,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def dir_exists(self, path: str) -> bool:
        """目录是否存在"""
        if path == "/":
            return True
        cursor = await self.db.execute(
            "SELECT 1 FROM dirs WHERE path = ?", (path,)
        )
        return await cursor.fetchone() is not None

    async def delete_dir_recursive(self, path: str, deleted_by: str = "webdav") -> tuple[int, int]:
        """递归删除目录记录，并把目录内文件移入回收站。返回(文件数,目录数)。"""
        path = path.rstrip("/") or "/"
        if path == "/":
            raise ValueError("根目录不能删除")
        if not await self.dir_exists(path):
            return (0, 0)
        like = path + "/%"
        cursor = await self.db.execute(
            """UPDATE files
               SET deleted=1, deleted_at=datetime('now','localtime'), deleted_by=?
               WHERE deleted=0 AND (path=? OR path LIKE ?)""",
            (deleted_by, path, like),
        )
        file_count = cursor.rowcount if cursor.rowcount is not None else 0
        cursor = await self.db.execute(
            "DELETE FROM dirs WHERE path=? OR path LIKE ?",
            (path, like),
        )
        dir_count = cursor.rowcount if cursor.rowcount is not None else 0
        await self.db.commit()
        return (file_count, dir_count)


class UserDB:
    """用户数据库操作"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def ensure_user(self, telegram_id: int, username: str = "",
                          first_name: str = "", role: str = "user") -> dict:
        """确保用户存在，不存在则创建"""
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row:
            await self.db.execute(
                """UPDATE users SET username = ?, first_name = ?,
                   last_active = datetime('now', 'localtime')
                   WHERE telegram_id = ?""",
                (username, first_name, telegram_id)
            )
            await self.db.commit()
            return dict(row)

        await self.db.execute(
            """INSERT INTO users (telegram_id, username, first_name, role)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, username, first_name, role)
        )
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return dict(row)

    async def get_user(self, telegram_id: int) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def is_allowed(self, telegram_id: int, admin_ids: list[int]) -> bool:
        """检查用户是否有权限"""
        # 管理员始终有权限
        if telegram_id in admin_ids:
            return True
        user = await self.get_user(telegram_id)
        if not user:
            return False
        return user["role"] not in ("banned",)
