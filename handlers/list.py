"""TGDrive - 文件列表和搜索处理"""

import logging
from aiogram import Router, types
from aiogram.filters import Command

from config import ADMIN_IDS, PAGE_SIZE
from database import get_db, UserDB, FileDB, DirDB
from utils import format_size, get_file_type, normalize_path

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


@router.message(Command("ls"))
async def cmd_ls(message: types.Message):
    """列出文件"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    # 解析路径参数
    args = message.text.split(maxsplit=1)
    path = normalize_path(args[1]) if len(args) > 1 else "/"

    db = await get_db()
    try:
        file_db = FileDB(db)
        dir_db = DirDB(db)

        # 获取子目录
        dirs = await dir_db.list_dirs(path)
        # 获取文件
        files = await file_db.list_files(path, limit=PAGE_SIZE)
        total = await file_db.count_files(path)

        if not dirs and not files:
            await message.answer(f"📂 目录 {path} 为空")
            return

        lines = [f"📂 *{path}*"]
        if path != "/":
            lines.append("⬆️ ..")

        # 目录
        for d in dirs:
            lines.append(f"📁 {d['name']}/")

        # 文件
        for f in files:
            icon = get_file_type(f["mime_type"], f["file_name"])
            size = format_size(f["file_size"])
            lines.append(f"{icon} `{f['id']}` - {f['file_name']} ({size})")

        if total > PAGE_SIZE:
            lines.append(f"\n_共 {total} 个文件，仅显示前 {PAGE_SIZE} 个_")

        text = "\n".join(lines)
        await message.answer(text, parse_mode="Markdown")
    finally:
        await db.close()


@router.message(Command("search"))
async def cmd_search(message: types.Message):
    """搜索文件"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法：/search 关键词")
        return

    keyword = args[1].strip()
    db = await get_db()
    try:
        file_db = FileDB(db)
        results = await file_db.search_files(keyword, limit=PAGE_SIZE)

        if not results:
            await message.answer(f"🔍 没有找到匹配 \"{keyword}\" 的文件")
            return

        lines = [f"🔍 搜索结果：*{keyword}*"]
        for f in results:
            icon = get_file_type(f["mime_type"], f["file_name"])
            size = format_size(f["file_size"])
            tags = f" [{f['tags']}]" if f["tags"] else ""
            lines.append(
                f"{icon} `{f['id']}` - {f['file_name']} ({size}){tags}"
            )

        text = "\n".join(lines)
        await message.answer(text, parse_mode="Markdown")
    finally:
        await db.close()


@router.message(Command("info"))
async def cmd_info(message: types.Message):
    """文件详情"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法：/info 文件ID")
        return

    try:
        file_id_int = int(args[1].strip())
    except ValueError:
        await message.answer("❌ 文件 ID 必须是数字")
        return

    db = await get_db()
    try:
        file_db = FileDB(db)
        f = await file_db.get_file(file_id_int)
        if not f:
            await message.answer(f"❌ 文件 ID {file_id_int} 不存在")
            return

        icon = get_file_type(f["mime_type"], f["file_name"])
        text = (
            f"{icon} *文件详情*\n\n"
            f"🆔 ID：`{f['id']}`\n"
            f"📄 文件名：{f['file_name']}\n"
            f"📏 大小：{format_size(f['file_size'])}\n"
            f"🏷️ MIME：{f['mime_type'] or 'unknown'}\n"
            f"📂 路径：{f['path']}\n"
            f"📌 标签：{f['tags'] or '无'}\n"
            f"👤 上传者：`{f['uploader_id']}`\n"
            f"🕐 时间：{f['created_at']}"
        )
        await message.answer(text, parse_mode="Markdown")
    finally:
        await db.close()
