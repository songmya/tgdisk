"""TGDrive - 文件上传处理"""

import logging
from aiogram import Router, types, F
from aiogram.filters import Command

from config import ADMIN_IDS, MAX_FILE_SIZE
from database import get_db, UserDB, FileDB
from utils import format_size, get_file_type

router = Router()
logger = logging.getLogger(__name__)


async def _check_permission(message: types.Message) -> bool:
    """检查用户权限"""
    if message.from_user.id in ADMIN_IDS:
        return True
    db = await get_db()
    try:
        user_db = UserDB(db)
        return await user_db.is_allowed(message.from_user.id, ADMIN_IDS)
    finally:
        await db.close()


@router.message(F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker)
async def handle_file(message: types.Message):
    """处理文件上传"""
    if not await _check_permission(message):
        await message.answer("❌ 你没有使用权限")
        return

    # 提取文件信息
    file_id = ""
    file_unique_id = ""
    file_name = ""
    file_size = 0
    mime_type = ""
    file_type = ""

    if message.document:
        doc = message.document
        file_id = doc.file_id
        file_unique_id = doc.file_unique_id
        file_name = doc.file_name or "unnamed"
        file_size = doc.file_size or 0
        mime_type = doc.mime_type or ""
        file_type = "document"
    elif message.photo:
        # photo 是列表，取最大尺寸
        photo = message.photo[-1]
        file_id = photo.file_id
        file_unique_id = photo.file_unique_id
        file_name = f"photo_{photo.file_unique_id}.jpg"
        file_size = photo.file_size or 0
        mime_type = "image/jpeg"
        file_type = "photo"
    elif message.video:
        vid = message.video
        file_id = vid.file_id
        file_unique_id = vid.file_unique_id
        file_name = vid.file_name or f"video_{vid.file_unique_id}.mp4"
        file_size = vid.file_size or 0
        mime_type = vid.mime_type or "video/mp4"
        file_type = "video"
    elif message.audio:
        aud = message.audio
        file_id = aud.file_id
        file_unique_id = aud.file_unique_id
        file_name = aud.file_name or f"audio_{aud.file_unique_id}.mp3"
        file_size = aud.file_size or 0
        mime_type = aud.mime_type or "audio/mpeg"
        file_type = "audio"
    elif message.voice:
        v = message.voice
        file_id = v.file_id
        file_unique_id = v.file_unique_id
        file_name = f"voice_{v.file_unique_id}.ogg"
        file_size = v.file_size or 0
        mime_type = v.mime_type or "audio/ogg"
        file_type = "voice"
    elif message.animation:
        anim = message.animation
        file_id = anim.file_id
        file_unique_id = anim.file_unique_id
        file_name = anim.file_name or f"animation_{anim.file_unique_id}.mp4"
        file_size = anim.file_size or 0
        mime_type = anim.mime_type or "video/mp4"
        file_type = "animation"
    elif message.sticker:
        s = message.sticker
        file_id = s.file_id
        file_unique_id = s.file_unique_id
        file_name = f"sticker_{s.file_unique_id}.webp"
        file_size = s.file_size or 0
        mime_type = "image/webp"
        file_type = "sticker"
    else:
        await message.answer("❌ 不支持的文件类型")
        return

    # 检查文件大小
    max_bytes = MAX_FILE_SIZE * 1024 * 1024
    if file_size > max_bytes:
        await message.answer(
            f"❌ 文件过大：{format_size(file_size)}\n"
            f"最大限制：{MAX_FILE_SIZE} MB"
        )
        return

    # 解析上传路径（从 caption 中提取，格式：/path/filename）
    upload_path = "/"
    caption = message.caption or ""
    if caption.startswith("/"):
        parts = caption.strip().split()
        if parts:
            from utils import normalize_path
            upload_path = normalize_path(parts[0])
            # 如果路径不是 /，验证目录是否存在
            if upload_path != "/":
                from database import DirDB
                db_check = await get_db()
                try:
                    dir_db = DirDB(db_check)
                    if not await dir_db.dir_exists(upload_path):
                        await message.answer(f"❌ 目录不存在：{upload_path}\n先 /mkdir 创建")
                        return
                finally:
                    await db_check.close()

    # 保存到数据库
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

        file_db = FileDB(db)
        file_int_id = await file_db.add_file(
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_name=file_name,
            file_size=file_size,
            mime_type=mime_type,
            file_type=file_type,
            path=upload_path,
            uploader_id=message.from_user.id,
            message_id=message.message_id,
            chat_id=message.chat.id,
        )

        icon = get_file_type(mime_type, file_name)
        text = (
            f"✅ 文件已保存\n\n"
            f"{icon} *{file_name}*\n"
            f"🆔 ID：`{file_int_id}`\n"
            f"📏 大小：{format_size(file_size)}\n"
            f"📁 路径：{upload_path}\n"
            f"🏷️ 类型：{mime_type or file_type}"
        )
        await message.answer(text, parse_mode="Markdown")
        logger.info(f"文件上传: {file_name} ({format_size(file_size)}) -> ID:{file_int_id}")
    finally:
        await db.close()
