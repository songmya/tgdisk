"""TGDrive - 基础命令处理"""

import logging
from aiogram import Router, types
from aiogram.filters import CommandStart, Command

from config import ADMIN_IDS
from database import get_db, UserDB, FileDB, DirDB
from utils import format_size

router = Router()
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """开始命令"""
    db = await get_db()
    try:
        user_db = UserDB(db)
        role = "admin" if message.from_user.id in ADMIN_IDS else "user"
        await user_db.ensure_user(
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
            role
        )

        text = (
            "☁️ *TGDrive - Telegram 网盘*\n\n"
            "直接发送文件给我即可上传！\n\n"
            "📋 常用命令：\n"
            "/ls \\[路径] - 浏览文件\n"
            "/search 关键词 - 搜索文件\n"
            "/get ID - 下载文件\n"
            "/del ID - 删除文件\n"
            "/mkdir 名称 - 创建目录\n"
            "/mv ID 路径 - 移动文件\n"
            "/tag ID 标签 - 打标签\n"
            "/info ID - 文件详情\n"
            "/stats - 统计信息\n"
            "/help - 帮助"
        )
        await message.answer(text, parse_mode="Markdown")
    finally:
        await db.close()


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 *TGDrive 使用帮助*\n\n"
        "📤 *上传文件*\n"
        "直接发送文件/图片/视频/音频给 Bot\n\n"
        "📋 *浏览文件*\n"
        "/ls - 列出根目录\n"
        "/ls /books - 列出指定目录\n\n"
        "🔍 *搜索文件*\n"
        "/search python - 按文件名或标签搜索\n\n"
        "📥 *下载文件*\n"
        "/get 123 - 下载指定 ID 的文件\n\n"
        "🗑️ *删除文件*\n"
        "/del 123 - 删除指定 ID 的文件索引\n\n"
        "📁 *目录管理*\n"
        "/mkdir books - 创建目录\n"
        "/mv 123 /books - 移动文件到目录\n\n"
        "🏷️ *标签*\n"
        "/tag 123 教程,python - 给文件打标签\n\n"
        "ℹ️ *其他*\n"
        "/info 123 - 查看文件详情\n"
        "/stats - 查看统计信息"
    )
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """统计信息"""
    db = await get_db()
    try:
        file_db = FileDB(db)
        stats = await file_db.stats()
        count = stats["count"]
        total_size = stats["total_size"]

        db2 = await get_db()
        try:
            cursor = await db2.execute("SELECT COUNT(*) FROM dirs")
            row = await cursor.fetchone()
            dir_count = row[0]
        finally:
            await db2.close()

        text = (
            f"📊 *TGDrive 统计*\n\n"
            f"📄 文件数：{count}\n"
            f"📁 目录数：{dir_count}\n"
            f"💾 总大小：{format_size(total_size)}"
        )
        await message.answer(text, parse_mode="Markdown")
    finally:
        await db.close()
