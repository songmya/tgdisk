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
                FOREIGN KEY (uploader_id) REFERENCES users(telegram_id)
            );

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

    async def delete_file(self, file_id_int: int) -> bool:
        """软删除文件"""
        cursor = await self.db.execute(
            "UPDATE files SET deleted = 1 WHERE id = ? AND deleted = 0", (file_id_int,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

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
