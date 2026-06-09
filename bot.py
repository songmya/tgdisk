"""TGDrive - Telegram Bot 网盘主入口"""

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

import config
from config import BOT_TOKEN, PROXY, LOG_LEVEL, LOCAL_API_BASE, LOCAL_API_MODE
from database import init_db

# 日志
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_bot() -> tuple[Bot, Dispatcher]:
    """创建 Bot 和 Dispatcher"""

    # 自建 Bot API Server
    api_server = None
    if LOCAL_API_BASE:
        api_server = TelegramAPIServer.from_base(LOCAL_API_BASE, is_local=LOCAL_API_MODE)
        logger.info(f"使用自建 Bot API Server: {LOCAL_API_BASE} (local_mode={LOCAL_API_MODE})")

    # 代理设置（连接自建服务器通常不需要代理）
    session = None
    use_proxy = PROXY and not LOCAL_API_BASE
    if use_proxy:
        logger.info(f"使用代理: {PROXY}")
        if PROXY.startswith("socks5://") or PROXY.startswith("socks4://"):
            # SOCKS 代理 - 通过环境变量让 python-socks 处理
            import os
            os.environ["ALL_PROXY"] = PROXY
            logger.info(f"SOCKS 代理已通过环境变量配置")
            session = AiohttpSession(proxy=PROXY, api=api_server) if api_server else AiohttpSession(proxy=PROXY)
        else:
            # HTTP/HTTPS 代理 - aiohttp 原生支持
            session = AiohttpSession(proxy=PROXY, api=api_server) if api_server else AiohttpSession(proxy=PROXY)
            logger.info(f"HTTP 代理已配置: {PROXY}")
    elif api_server:
        # 只用自建服务器，不走代理
        session = AiohttpSession(api=api_server)

    # 创建 Bot
    if session:
        bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    else:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))

    dp = Dispatcher()

    # 注册 handlers
    from handlers.basic import router as basic_router
    from handlers.upload import router as upload_router
    from handlers.list import router as list_router
    from handlers.download import router as download_router
    from handlers.manage import router as manage_router

    dp.include_router(basic_router)
    dp.include_router(upload_router)
    dp.include_router(list_router)
    dp.include_router(download_router)
    dp.include_router(manage_router)

    return bot, dp


async def main():
    """主函数"""
    # 校验配置
    try:
        config.validate()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("TGDrive 启动中...")

    # 初始化数据库
    await init_db()
    logger.info("数据库初始化完成")

    # 创建 Bot
    bot, dp = create_bot()

    # 注册 Bot 命令
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="ls", description="浏览文件"),
            BotCommand(command="search", description="搜索文件"),
            BotCommand(command="get", description="下载文件"),
            BotCommand(command="del", description="删除文件"),
            BotCommand(command="info", description="文件详情"),
            BotCommand(command="mkdir", description="创建目录"),
            BotCommand(command="mv", description="移动文件"),
            BotCommand(command="tag", description="打标签"),
            BotCommand(command="stats", description="统计信息"),
            BotCommand(command="help", description="帮助"),
        ])
    except Exception as e:
        logger.warning(f"注册命令失败（不影响使用）: {e}")

    # 启动
    me = await bot.me()
    logger.info(f"Bot 已启动: @{me.username} (ID: {me.id})")
    logger.info(f"管理员: {config.ADMIN_IDS}")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("TGDrive 已停止")


if __name__ == "__main__":
    asyncio.run(main())
