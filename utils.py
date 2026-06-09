"""工具函数"""

import os


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"


def get_file_type(mime_type: str | None, file_name: str) -> str:
    """获取文件类型图标"""
    if mime_type:
        if mime_type.startswith("image/"):
            return "🖼️"
        if mime_type.startswith("video/"):
            return "🎬"
        if mime_type.startswith("audio/"):
            return "🎵"
        if mime_type == "application/pdf":
            return "📄"
        if mime_type in ("application/zip", "application/x-rar-compressed",
                         "application/x-7z-compressed", "application/gzip",
                         "application/x-tar"):
            return "📦"
        if mime_type.startswith("text/"):
            return "📝"
    # 按扩展名判断
    ext = os.path.splitext(file_name)[1].lower()
    ext_map = {
        ".pdf": "📄", ".epub": "📖", ".mobi": "📖", ".azw3": "📖",
        ".doc": "📝", ".docx": "📝", ".xls": "📊", ".xlsx": "📊",
        ".ppt": "📊", ".pptx": "📊",
        ".zip": "📦", ".rar": "📦", ".7z": "📦", ".tar": "📦", ".gz": "📦",
        ".mp4": "🎬", ".mkv": "🎬", ".avi": "🎬", ".mov": "🎬",
        ".mp3": "🎵", ".flac": "🎵", ".wav": "🎵", ".ogg": "🎵",
        ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️", ".webp": "🖼️",
        ".py": "🐍", ".js": "📝", ".go": "📝", ".rs": "📝", ".java": "📝",
        ".sh": "⚙️", ".bat": "⚙️",
    }
    return ext_map.get(ext, "📎")


def normalize_path(path: str) -> str:
    """规范化路径"""
    if not path:
        return "/"
    path = path.replace("\\", "/")
    # 去除多余斜杠
    parts = [p for p in path.split("/") if p]
    result = "/" + "/".join(parts)
    return result if result != "" else "/"
