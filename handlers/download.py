"""TGDrive - 文件下载和删除处理"""

import logging
from aiogram import Router, types
from aiogram.filters import Command

from config import ADMIN_IDS
from database import get_db, UserDB, FileDB
from utils import format_size, get_file_type

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


@router.message(Command("get"))
async def cmd_get(message: types.Message):
    """下载文件"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法：/get 文件ID")
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

        # 多分片文件：Bot 不能直接推送 >50MB，引导用户走 Web
        if f.get("is_multipart"):
            await message.answer(
                f"📦 *{f['file_name']}*\n"
                f"该文件为分片文件（{f.get('chunk_count', '?')} 片，总大小 "
                f"{format_size(f['file_size'])}）。\n"
                f"超过 Bot API 50MB 推送上限，请通过 Web UI 下载：\n"
                f"`/api/proxy-download/{file_id_int}`",
                parse_mode="Markdown",
            )
            return

        # 根据文件类型选择发送方式
        tg_file_id = f["file_id"]
        file_type = f["file_type"]

        await message.answer("⏳ 正在发送文件...")

        try:
            if file_type == "photo":
                await message.answer_photo(tg_file_id, caption=f"📄 {f['file_name']}")
            elif file_type == "video":
                await message.answer_video(tg_file_id, caption=f"📄 {f['file_name']}")
            elif file_type == "audio":
                await message.answer_audio(tg_file_id, caption=f"📄 {f['file_name']}")
            elif file_type == "voice":
                await message.answer_voice(tg_file_id)
            elif file_type == "animation":
                await message.answer_animation(tg_file_id, caption=f"📄 {f['file_name']}")
            elif file_type == "sticker":
                await message.answer_sticker(tg_file_id)
            else:
                await message.answer_document(tg_file_id, caption=f"📄 {f['file_name']}")

            logger.info(f"文件下载: {f['file_name']} (ID:{file_id_int})")
        except Exception as e:
            logger.error(f"发送文件失败: {e}")
            await message.answer(
                f"❌ 发送文件失败\n"
                f"文件可能已从 Telegram 服务器过期或被删除\n"
                f"错误：{str(e)[:100]}"
            )
    finally:
        await db.close()


@router.message(Command("del"))
async def cmd_delete(message: types.Message):
    """删除文件"""
    # 只有管理员可以删除
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ 只有管理员可以删除文件")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("用法：/del 文件ID")
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
            await message.answer(f"❌ 文件 ID {file_id_int} 不存在或已删除")
            return

        success = await file_db.delete_file(file_id_int)
        if success:
            await message.answer(
                f"✅ 已删除索引：{f['file_name']} (ID:{file_id_int})\n"
                f"⚠️ 注意：文件可能仍存在于 Telegram 服务器"
            )
            logger.info(f"文件删除: {f['file_name']} (ID:{file_id_int})")
        else:
            await message.answer(f"❌ 删除失败")
    finally:
        await db.close()
