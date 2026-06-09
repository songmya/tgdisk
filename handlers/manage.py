"""TGDrive - 目录和标签管理"""

import logging
from aiogram import Router, types
from aiogram.filters import Command

from config import ADMIN_IDS
from database import get_db, UserDB, FileDB, DirDB
from utils import normalize_path

router = Router()
logger = logging.getLogger(__name__)


async def _check_permission(message: types.Message) -> bool:
    if message.from_user.id in ADMIN_IDS:
        return True
    db = await get_db()
    try:
        user_db = UserDB(db)
        return await user_db.is_allowed(message.from_user.id, ADMIN_IDS)
    finally:
        await db.close()


@router.message(Command("mkdir"))
async def cmd_mkdir(message: types.Message):
    """创建目录"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法：/mkdir 目录名\n或：/mkdir /parent/dirname")
        return

    raw = args[1].strip()
    db = await get_db()
    try:
        dir_db = DirDB(db)

        # 支持绝对路径和相对路径
        if "/" in raw:
            # 绝对路径：/books/python
            parts = [p for p in raw.split("/") if p]
            name = parts[-1]
            parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
            parent = normalize_path(parent)
        else:
            name = raw
            parent = "/"

        # 确保父目录存在
        if parent != "/" and not await dir_db.dir_exists(parent):
            await message.answer(f"❌ 父目录不存在：{parent}")
            return

        result = await dir_db.create_dir(name, parent)
        if result:
            await message.answer(f"✅ 目录已创建：{result}")
            logger.info(f"目录创建: {result}")
        else:
            await message.answer(f"❌ 目录已存在：{normalize_path(parent)}/{name}")
    finally:
        await db.close()


@router.message(Command("mv"))
async def cmd_mv(message: types.Message):
    """移动文件"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("用法：/mv 文件ID 目标路径\n示例：/mv 123 /books")
        return

    try:
        file_id_int = int(args[1])
    except ValueError:
        await message.answer("❌ 文件 ID 必须是数字")
        return

    target_path = normalize_path(args[2])

    db = await get_db()
    try:
        file_db = FileDB(db)
        dir_db = DirDB(db)

        f = await file_db.get_file(file_id_int)
        if not f:
            await message.answer(f"❌ 文件 ID {file_id_int} 不存在")
            return

        # 验证目标目录
        if target_path != "/" and not await dir_db.dir_exists(target_path):
            await message.answer(f"❌ 目标目录不存在：{target_path}\n先 /mkdir 创建")
            return

        success = await file_db.move_file(file_id_int, target_path)
        if success:
            await message.answer(
                f"✅ 文件已移动\n"
                f"📄 {f['file_name']}\n"
                f"{f['path']} → {target_path}"
            )
            logger.info(f"文件移动: {f['file_name']} {f['path']} -> {target_path}")
        else:
            await message.answer("❌ 移动失败")
    finally:
        await db.close()


@router.message(Command("tag"))
async def cmd_tag(message: types.Message):
    """给文件打标签"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("用法：/tag 文件ID 标签1,标签2\n示例：/tag 123 教程,python")
        return

    try:
        file_id_int = int(args[1])
    except ValueError:
        await message.answer("❌ 文件 ID 必须是数字")
        return

    tags = args[2].strip()

    db = await get_db()
    try:
        file_db = FileDB(db)
        f = await file_db.get_file(file_id_int)
        if not f:
            await message.answer(f"❌ 文件 ID {file_id_int} 不存在")
            return

        success = await file_db.tag_file(file_id_int, tags)
        if success:
            await message.answer(f"✅ 标签已更新：{f['file_name']} → [{tags}]")
        else:
            await message.answer("❌ 打标签失败")
    finally:
        await db.close()
