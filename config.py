"""TGDrive 配置模块"""

import os
from dotenv import load_dotenv

load_dotenv()

# Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 管理员 ID 列表
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# 代理
PROXY = os.getenv("PROXY") or os.getenv("ALL_PROXY") or os.getenv("all_proxy") or ""

# 自建 Bot API Server（留空则使用官方 https://api.telegram.org）
# 例：http://127.0.0.1:8081  或  https://tg-api.example.com
LOCAL_API_BASE = os.getenv("LOCAL_API_BASE", "").rstrip("/")
# 是否使用本地模式（local mode）。本地模式下 Bot API Server 与 Bot 部署在同机，
# 文件路径返回为绝对路径而不是下载链接，单文件可达 2GB。
LOCAL_API_MODE = os.getenv("LOCAL_API_MODE", "false").lower() in ("1", "true", "yes", "on")

# 数据库
DB_PATH = os.getenv("DB_PATH", "data/tgdrive.sqlite3")

# 文件大小限制（MB）。0 = 不限（只受 Telegram 服务器和本地磁盘约束）
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "0"))

# 分页
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))

# 日志
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate():
    """校验必要配置"""
    errors = []
    if not BOT_TOKEN:
        errors.append("BOT_TOKEN 未设置")
    if not ADMIN_IDS:
        errors.append("ADMIN_IDS 未设置")
    if errors:
        raise ValueError("配置错误：\n" + "\n".join(f"  - {e}" for e in errors))
