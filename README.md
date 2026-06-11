# TGDrive / TGDisk

基于 Telegram Bot 的轻量网盘服务。文件实际存储在 Telegram，TGDrive 在本地 SQLite 中保存文件索引、目录结构、分片信息和上传任务状态，并提供三种访问方式：

- **Telegram Bot**：直接把文件发给 Bot，使用命令管理文件。
- **WebUI / HTTP API**：浏览器上传、下载、搜索、回收站、上传进度与续传。
- **WebDAV**：把 Telegram 网盘挂载到系统文件管理器、Infuse、播放器或支持 WebDAV 的客户端。

项目重点优化了大文件场景：支持 WebUI 缓存上传、WebDAV 上传、分片存储、Range 下载、断点续传，以及自建 Telegram Bot API local mode 下的低内存文件流上传。

---

## 目录

- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [快速部署](#快速部署)
- [方式一：使用自建 Telegram Bot API（推荐）](#方式一使用自建-telegram-bot-api推荐)
- [方式二：使用官方 Telegram Bot API](#方式二使用官方-telegram-bot-api)
- [最简配置说明](#最简配置说明)
- [访问方式](#访问方式)
- [Telegram Bot 命令](#telegram-bot-命令)
- [WebUI 使用说明](#webui-使用说明)
- [WebDAV 使用说明](#webdav-使用说明)
- [大文件与低内存上传](#大文件与低内存上传)
- [断点续传](#断点续传)
- [Range 下载与视频在线播放](#range-下载与视频在线播放)
- [回收站与彻底删除](#回收站与彻底删除)
- [孤儿分片清理](#孤儿分片清理)
- [备份与迁移](#备份与迁移)
- [常见问题](#常见问题)
- [安全建议](#安全建议)
- [开发运行](#开发运行)

---

## 功能特性

### 文件管理

- 上传任意类型文件。
- 目录管理：创建目录、按目录浏览、移动文件。
- 文件搜索：按文件名和标签搜索。
- 文件详情、统计信息、标签。
- WebUI / WebDAV 删除默认进入回收站。
- 回收站支持恢复与彻底删除索引。

### 多入口访问

- Telegram Bot 命令行式管理。
- WebUI：默认端口 `8080`，Docker Compose 默认映射为宿主机 `6354`。
- WebDAV：挂载在 WebUI 同端口的 `/dav` 路径。
- HTTP API：供脚本或前端调用。

### 大文件支持

- 官方 Bot API 下默认按较小分片上传，规避官方 `sendDocument` 限制。
- 自建 `telegram-bot-api --local` 下支持本地文件对象流式上传。
- WebUI 上传先缓存到服务器，再后台上传 Telegram，可显示双阶段进度。
- WebDAV PUT 先写入临时文件，再交给上传逻辑处理。
- 超过单文件阈值的大文件自动拆成 multipart 分片。

### 下载能力

- 单文件与 multipart 文件统一下载。
- 支持 HTTP `Range`，可用于视频在线播放、断点下载和 WebDAV 客户端随机读取。
- 自建 Bot API local mode 下载时可直接读取共享的 Bot API 本地文件目录，减少外网回源。

---

## 工作原理

TGDrive 不把文件内容长期保存在本地，它把文件或分片发送到 Telegram：

1. 用户通过 Bot、WebUI 或 WebDAV 上传文件。
2. TGDrive 调用 Telegram Bot API `sendDocument`。
3. Telegram 返回 `file_id`、`file_unique_id`、消息 ID 等元数据。
4. TGDrive 把元数据写入本地 SQLite：默认 `data/tgdrive.sqlite3`。
5. 下载时，TGDrive 根据 SQLite 中的 `file_id` 调用 `getFile`，再从 Telegram 或本地 Bot API 缓存目录流式读取文件内容。

对于大文件，TGDrive 会把文件拆成多个 Telegram 文档分片，并在下载时按顺序拼接；Range 请求只读取命中的分片区间。

> 注意：Telegram Bot API 不能“按 file_id 删除 Telegram 服务器上的文件”。TGDrive 只能尽力删除当初 Bot 发送的消息，并删除本地索引。

---

## 快速部署

下面只给两套**最简可用**配置：

1. **使用自建 Telegram Bot API**：推荐，大文件体验更好，支持 local mode 低内存上传。
2. **使用官方 Telegram Bot API**：配置更少，但文件大小限制更明显，国内机器通常需要代理。

通用准备：

```bash
git clone https://github.com/songmya/tgdisk.git
cd tgdisk
mkdir -p data
```

`BOT_TOKEN` 从 [@BotFather](https://t.me/BotFather) 获取；`ADMIN_IDS` 可以给 [@userinfobot](https://t.me/userinfobot) 发消息获取 Telegram 数字 ID。

---

## 方式一：使用自建 Telegram Bot API（推荐）

适合：大文件、WebDAV、在线播放、长期自托管。

优点：

- 可启用 `telegram-bot-api --local`。
- TGDrive 与 Bot API 共享本地目录，下载可直接读 local 文件路径。
- WebUI / WebDAV 上传先临时落盘，再通过文件对象流式交给本地 Bot API，内存占用低。
- 大文件可按 1500MB 左右分片，分片数量远少于官方 Bot API 模式。

### 最简 `.env`

所有可调配置都放在 `.env`，`docker-compose.yml` 只负责服务结构、端口和挂载。

```env
# Telegram Bot
BOT_TOKEN=123456:your_bot_token
ADMIN_IDS=123456789

# 自建 telegram-bot-api 必填，到 https://my.telegram.org/apps 获取
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_LOCAL=1

# tgdisk 使用 compose 内的 botapi 服务
LOCAL_API_BASE=http://botapi:8081
LOCAL_API_MODE=true
BOT_API_SERVER_DIR=/var/lib/telegram-bot-api
BOT_API_LOCAL_DIR=/var/lib/telegram-bot-api

# local mode 推荐值：低内存，少分片
TG_LOCAL_SINGLE_UPLOAD_LIMIT_MB=1500
TG_LOCAL_PART_SIZE_MB=1500
UPLOAD_CONCURRENCY=1

# WebUI 上传缓存
UPLOAD_CACHE_ENABLED=true
UPLOAD_CACHE_DIR=data/cache
UPLOAD_CACHE_KEEP_AFTER_DONE=false
RESUME_ALLOWED_DIRS=/app/data/cache,/tmp,/var/lib/telegram-bot-api

# WebUI / WebDAV 安全配置，强烈建议填写
WEBUI_TOKEN=change_me_to_a_long_random_token
WEBDAV_USERNAME=tgdisk
WEBDAV_PASSWORD=change_me_to_a_strong_password
```

### 最简 `docker-compose.yml`

```yaml
services:
  tgdisk:
    build: .
    image: tgdisk:latest
    container_name: tgdisk
    restart: unless-stopped
    env_file: .env
    depends_on:
      - botapi
    ports:
      - "6354:8080"
    volumes:
      - ./data:/app/data
      - ./data/botapi:/var/lib/telegram-bot-api:ro

  botapi:
    image: aiogram/telegram-bot-api:latest
    container_name: tgdisk-botapi
    restart: unless-stopped
    env_file: .env
    command:
      - --http-port=8081
      - --dir=/var/lib/telegram-bot-api
      - --local
    volumes:
      - ./data/botapi:/var/lib/telegram-bot-api
```

启动：

```bash
docker compose up -d
```

访问：

```text
WebUI:  http://服务器IP:6354/?token=你的WEBUI_TOKEN
WebDAV: http://服务器IP:6354/dav
```

---

## 方式二：使用官方 Telegram Bot API

适合：只想最简单跑起来、不想维护自建 Bot API、文件较小或能接受较多小分片。

特点：

- 不需要 `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`。
- 不需要 `botapi` 容器。
- 需要能访问 `api.telegram.org`；国内机器通常要配置 `PROXY`。
- 官方 Bot API 上传限制更严格，建议使用 18MB 左右分片。

### 最简 `.env`

所有可调配置都放在 `.env`。

```env
# Telegram Bot
BOT_TOKEN=123456:your_bot_token
ADMIN_IDS=123456789

# 明确使用官方 Bot API
LOCAL_API_BASE=
LOCAL_API_MODE=false

# 如果服务器不能直连 Telegram，填写代理；能直连则删除或留空
PROXY=http://user:pass@host:port
# PROXY=socks5://user:pass@host:port

# 官方 Bot API 建议保持小分片
TG_UPLOAD_CHUNK_SIZE_MB=18
TG_SINGLE_UPLOAD_THRESHOLD_MB=18
UPLOAD_CONCURRENCY=4

# WebUI 上传缓存
UPLOAD_CACHE_ENABLED=true
UPLOAD_CACHE_DIR=data/cache
UPLOAD_CACHE_KEEP_AFTER_DONE=false

# WebUI / WebDAV 安全配置，强烈建议填写
WEBUI_TOKEN=change_me_to_a_long_random_token
WEBDAV_USERNAME=tgdisk
WEBDAV_PASSWORD=change_me_to_a_strong_password
```

### 最简 `docker-compose.yml`

```yaml
services:
  tgdisk:
    build: .
    image: tgdisk:latest
    container_name: tgdisk
    restart: unless-stopped
    env_file: .env
    ports:
      - "6354:8080"
    volumes:
      - ./data:/app/data
```

启动：

```bash
docker compose up -d
```

访问：

```text
WebUI:  http://服务器IP:6354/?token=你的WEBUI_TOKEN
WebDAV: http://服务器IP:6354/dav
```

---

## 最简配置说明

真正必须理解的变量只有这些：

| 变量 | 什么时候需要 | 说明 |
|---|---|---|
| `BOT_TOKEN` | 两种方式都需要 | Telegram Bot Token。 |
| `ADMIN_IDS` | 两种方式都需要 | 允许使用 Bot 的管理员 Telegram 数字 ID。 |
| `TELEGRAM_API_ID` | 仅自建 Bot API | 自建 `telegram-bot-api` 登录 Telegram API 用。 |
| `TELEGRAM_API_HASH` | 仅自建 Bot API | 自建 `telegram-bot-api` 登录 Telegram API 用。 |
| `PROXY` | 仅官方 Bot API 且不能直连 Telegram 时 | HTTP 或 SOCKS5 代理。 |
| `WEBUI_TOKEN` | 强烈建议 | WebUI/API 访问令牌。 |
| `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` | 强烈建议 | WebDAV 登录账号密码。 |

其他变量都有默认值，先不用管。等你确实遇到缓存容量、分片大小、续传目录、CORS 等需求时，再回头调整 `.env.example` 里的高级选项。

## 访问方式

### WebUI

```text
http://服务器IP:6354/
```

WebUI 与 API 默认跑在容器内 `8080`，compose 映射到宿主机 `6354`。

### WebDAV

```text
http://服务器IP:6354/dav
```

WebDAV 挂载在同一个 FastAPI 服务下，不再默认单独开放 `8081`。

### Telegram Bot

直接在 Telegram 中给 Bot 发送命令或文件。只有 `ADMIN_IDS` 中的用户拥有管理员权限。

---

## Telegram Bot 命令

| 命令 | 说明 | 示例 |
|---|---|---|
| `/start` | 开始使用。 | `/start` |
| `/help` | 查看帮助。 | `/help` |
| `/ls [路径]` | 列出目录内容。 | `/ls /books` |
| `/search 关键词` | 搜索文件名或标签。 | `/search python` |
| `/get ID` | 获取下载链接或文件。 | `/get 123` |
| `/del ID` | 删除文件索引。 | `/del 123` |
| `/info ID` | 查看文件详情。 | `/info 123` |
| `/mkdir 名称` | 创建目录。 | `/mkdir books` |
| `/mv ID 路径` | 移动文件到目录。 | `/mv 123 /books` |
| `/tag ID 标签` | 设置标签。 | `/tag 123 教程` |
| `/stats` | 查看统计。 | `/stats` |

也可以直接把文件发送给 Bot，TGDrive 会上传并记录索引。

---

## WebUI 使用说明

WebUI 支持：

- 浏览当前目录文件。
- 创建目录。
- 搜索文件。
- 上传文件。
- 查看上传任务状态。
- 下载文件。
- 删除到回收站。
- 查看回收站、恢复、彻底删除。

### 上传流程

默认 `UPLOAD_CACHE_ENABLED=true`，WebUI 上传分两阶段：

1. 浏览器把文件按 `BROWSER_CHUNK_SIZE` 写到服务器缓存目录。
2. 后端后台任务从缓存文件上传到 Telegram。

这样可以显示：

- 浏览器 → 服务器 的进度。
- 服务器 → Telegram 的进度。

上传任务可通过这些 API 查看：

```text
GET /api/uploads
GET /api/uploads/{session_id}
GET /api/upload-cache
```

如果关闭 `UPLOAD_CACHE_ENABLED`，`POST /api/upload` 会回退为旧的直接流式上传模式，接口会等待 Telegram 上传完成后才返回。

---

## WebDAV 使用说明

WebDAV 地址：

```text
http://服务器IP:6354/dav
```

支持能力：

- `PROPFIND` 浏览目录。
- `MKCOL` 创建目录。
- `PUT` 上传文件。
- 对同名文件 `PUT` 会覆盖旧索引。
- `GET` 下载文件。
- `Range GET` 随机读取。
- `DELETE` 文件或目录会进入回收站。

### 常见客户端

- Windows：映射网络驱动器，或使用 RaiDrive、Cyberduck。
- macOS：Finder → 前往 → 连接服务器。
- Linux：`davfs2`、GNOME Files、KDE Dolphin。
- iOS / Android：支持 WebDAV 的文件管理器或播放器。

### 挂载示例

Linux `davfs2`：

```bash
sudo apt-get install davfs2
sudo mkdir -p /mnt/tgdisk
sudo mount -t davfs http://服务器IP:6354/dav /mnt/tgdisk
```

如果启用了 WebDAV 账号密码，按客户端提示输入 `WEBDAV_USERNAME` 和 `WEBDAV_PASSWORD`。

---

## 大文件与低内存上传

### 推荐模式：自建 Bot API local mode

Docker Compose 默认启动 `aiogram/telegram-bot-api` 并启用：

```yaml
command:
  - --http-port=8081
  - --dir=/var/lib/telegram-bot-api
  - --local
```

`tgdisk` 容器默认配置：

```env
LOCAL_API_BASE=http://botapi:8081
LOCAL_API_MODE=true
BOT_API_SERVER_DIR=/var/lib/telegram-bot-api
BOT_API_LOCAL_DIR=/var/lib/telegram-bot-api
```

此模式下：

- WebUI：浏览器上传到 `UPLOAD_CACHE_DIR`，后台通过本地文件对象流式发送给 Bot API。
- WebDAV：客户端 PUT 到临时文件，关闭后通过本地文件对象流式发送给 Bot API。
- Python 进程不需要把 1.5GB 分片整体读进内存。
- 小于等于 `TG_LOCAL_SINGLE_UPLOAD_LIMIT_MB` 的文件走 `local_stream` 单文件上传。
- 更大的文件走 `local_multipart`，按 `TG_LOCAL_PART_SIZE_MB` 拆分。

### 官方 Bot API / 非 local mode

如果不使用自建 Bot API：

```env
LOCAL_API_BASE=
LOCAL_API_MODE=false
TG_UPLOAD_CHUNK_SIZE_MB=18
TG_SINGLE_UPLOAD_THRESHOLD_MB=18
UPLOAD_CONCURRENCY=4
```

此时 TGDrive 会按 `TG_UPLOAD_CHUNK_SIZE_MB` 分片，把每个分片作为 `sendDocument` 上传。内存峰值大致为：

```text
UPLOAD_CONCURRENCY × TG_UPLOAD_CHUNK_SIZE_MB
```

官方 Bot API 对上传大小限制较严格，建议保持 18MB 左右，并配置可访问 Telegram 的代理。

---

## 断点续传

multipart 上传失败时，TGDrive 会尽量保留主文件记录和已成功分片，失败分片记录为 `failed`，便于后续续传。

### 查看缺失分片

```bash
curl -H "Authorization: Bearer $WEBUI_TOKEN" \
  "http://服务器IP:6354/api/upload-status/文件ID"
```

返回示例：

```json
{
  "file_id": 123,
  "file_name": "movie.mkv",
  "chunk_count": 3,
  "missing": [2],
  "complete": false
}
```

### 从服务器本地源文件续传

```bash
curl -X POST -H "Authorization: Bearer $WEBUI_TOKEN" \
  "http://服务器IP:6354/api/resume-upload/文件ID?source_path=/app/data/cache/source-file.part"
```

安全限制：

- `source_path` 必须是 TGDrive 服务器本地可读路径。
- 默认只允许 `UPLOAD_CACHE_DIR` 和系统临时目录。
- 可通过 `RESUME_ALLOWED_DIRS` 增加允许目录。

> 如果上传成功后 `UPLOAD_CACHE_KEEP_AFTER_DONE=false`，缓存文件会被删除；失败任务会按 TTL 保留一段时间，便于续传。

---

## Range 下载与视频在线播放

TGDrive 的下载代理支持 Range：

```text
GET /api/proxy-download/{file_id}
Range: bytes=0-1048575
```

行为：

- 返回 `206 Partial Content`。
- 带 `Accept-Ranges: bytes`。
- multipart 文件只读取命中的分片和范围。
- WebDAV 文件对象也支持 seek / Range 读取。

这对视频在线播放、音频拖动进度、断点下载非常重要。

下载示例：

```bash
curl -L -H "Authorization: Bearer $WEBUI_TOKEN" \
  -o file.bin \
  "http://服务器IP:6354/api/proxy-download/123"
```

Range 示例：

```bash
curl -H "Authorization: Bearer $WEBUI_TOKEN" \
  -H "Range: bytes=0-1048575" \
  -o first-1m.bin \
  "http://服务器IP:6354/api/proxy-download/123"
```

---

## 回收站与彻底删除

WebUI 和 WebDAV 删除文件时，默认是软删除：

- `files.deleted=1`
- 记录删除时间与来源
- 文件从普通列表中隐藏
- 仍可从回收站恢复

### 回收站 API

```text
GET    /api/trash
POST   /api/trash/{file_id}/restore
DELETE /api/trash/{file_id}?delete_tg=true
```

彻底删除时，TGDrive 会：

1. 尽力调用 Telegram `deleteMessage` 删除原始上传消息和分片消息。
2. 删除本地 SQLite 索引。

限制：

- Bot API 不能按 `file_id` 删除 Telegram 服务器文件。
- `deleteMessage` 可能受 Telegram 时间限制、权限限制影响。
- 因此彻底删除不能 100% 保证 Telegram 服务器侧文件立即不可恢复。

---

## 孤儿分片清理

如果 multipart 上传中断，可能留下“未集齐所有分片”的文件记录。可以定期清理。

### Dry-run 查看

```bash
python scripts/cleanup_orphans.py --hours 24
```

### 实际清理

```bash
python scripts/cleanup_orphans.py --hours 24 --apply
```

### HTTP API

```bash
curl -X POST -H "Authorization: Bearer $WEBUI_TOKEN" \
  "http://服务器IP:6354/api/cleanup-orphans?hours=24&apply=false"
```

实际清理：

```bash
curl -X POST -H "Authorization: Bearer $WEBUI_TOKEN" \
  "http://服务器IP:6354/api/cleanup-orphans?hours=24&apply=true"
```

### Cron 示例

```cron
0 5 * * * cd /path/to/tgdisk && /usr/bin/python3 scripts/cleanup_orphans.py --apply >> data/cleanup.log 2>&1
```

---

## 备份与迁移

最重要的是 SQLite 数据库：

```text
data/tgdrive.sqlite3
```

建议备份：

```bash
mkdir -p backups
sqlite3 data/tgdrive.sqlite3 ".backup 'backups/tgdrive-$(date +%F).sqlite3'"
```

Docker 部署时建议备份整个 `data/` 目录：

```bash
tar -czf tgdisk-data-$(date +%F).tar.gz data/
```

迁移注意事项：

- 不要随意更换 `BOT_TOKEN`，Telegram `file_id` 通常不跨 Bot 通用。
- 如果使用 Bot API local mode 下载，迁移后要保证 `BOT_API_SERVER_DIR` / `BOT_API_LOCAL_DIR` 映射正确。
- WebUI 缓存目录不是长期数据，但失败上传续传可能依赖其中的临时源文件。

---

## 常见问题

### 1. 为什么需要自建 Bot API Server？

官方 Bot API 上传限制较小，大文件需要切很多小片。自建 `telegram-bot-api --local` 通常能更好地处理大文件，并允许 local mode 返回服务器本地文件路径，下载和上传都更适合自托管场景。

### 2. `LOCAL_API_MODE=true` 是否完全不占内存？

不是完全不占，但不会把大文件或 1.5GB 分片整体读入 Python 内存。它使用文件对象流式传给本地 Bot API，内存主要来自 HTTP 框架、系统缓冲和小块读写。

### 3. WebDAV 上传为什么要先落临时文件？

WebDAV 客户端是同步写入模型。当前实现先写入 `UPLOAD_CACHE_DIR` 下的临时文件，再调用统一的本地文件上传逻辑。这样更稳定，也能配合 local Bot API 做低内存文件流上传。

### 4. 删除文件后 Telegram 上是否真的删除？

不一定。TGDrive 可以删除本地索引，并尽力删除 Bot 当初发送的消息；但 Bot API 不能按 `file_id` 删除 Telegram 服务器文件，旧消息删除也可能失败。

### 5. WebUI 打开 API 返回 401？

如果设置了 `WEBUI_TOKEN`，需要以下任一方式携带 token：

- 首次访问 `/?token=你的WEBUI_TOKEN` 写入 Cookie。
- 请求头：`Authorization: Bearer 你的WEBUI_TOKEN`。
- 查询参数：`?token=你的WEBUI_TOKEN`。

### 6. 容器里代理写 `127.0.0.1:7890` 不生效？

容器内的 `127.0.0.1` 是容器自己，不是宿主机。请改用宿主机 LAN IP，或者使用 host 网络模式。

### 7. WebDAV 客户端新建文件夹失败？

标准做法是发送 `MKCOL`。项目也兼容部分客户端的空 `PUT /name/` 和先 `PROPFIND` 的行为。如果仍失败，换一个 WebDAV 客户端试试，或查看 `docker compose logs -f tgdisk`。

---

## 安全建议

- 生产环境务必设置 `WEBUI_TOKEN`。
- 生产环境务必设置 `WEBDAV_USERNAME` 和 `WEBDAV_PASSWORD`。
- 不要把 `.env` 提交到 Git。
- 不建议存储证件、密钥、密码库等特别敏感的文件。
- 定期备份 `data/tgdrive.sqlite3`。
- 如果暴露到公网，建议放在 HTTPS 反向代理后面。
- 对 `/api/resume-upload` 谨慎配置 `RESUME_ALLOWED_DIRS`，避免允许读取过宽的本地目录。

---

## 开发运行

### 本地 Python 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env
python bot.py
```

另开一个终端运行 WebUI / WebDAV：

```bash
python webui.py 8080
```

WebDAV 默认挂载在：

```text
http://127.0.0.1:8080/dav
```

### Docker 构建

```bash
docker build -t tgdisk:latest .
docker compose up -d
```

### 简单检查

```bash
python -m py_compile bot.py config.py database.py tg_io.py upload_cache.py webdav.py webui.py handlers/*.py scripts/*.py
python scripts/smoke_refactor.py
```

---

## 许可

当前仓库未声明开源许可证。公开使用或二次分发前，请根据你的需求补充 `LICENSE` 文件。
