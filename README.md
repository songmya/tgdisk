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
- [快速部署：Docker Compose 推荐](#快速部署docker-compose-推荐)
- [配置说明](#配置说明)
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

## 快速部署：Docker Compose 推荐

Docker Compose 会同时启动：

- `tgdisk`：Bot、WebUI、WebDAV。
- `botapi`：自建 Telegram Bot API Server，启用 `--local`。

### 1. 克隆项目

```bash
git clone https://github.com/songmya/tgdisk.git
cd tgdisk
```

### 2. 准备配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```env
BOT_TOKEN=123456:your_bot_token
ADMIN_IDS=你的Telegram数字ID
TELEGRAM_API_ID=你的Telegram_API_ID
TELEGRAM_API_HASH=你的Telegram_API_HASH
```

获取方式：

- `BOT_TOKEN`：找 Telegram 的 [@BotFather](https://t.me/BotFather) 创建 Bot。
- `ADMIN_IDS`：给 [@userinfobot](https://t.me/userinfobot) 发消息获取数字 ID。
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`：到 <https://my.telegram.org/apps> 创建应用获取。自建 Bot API Server 需要这两项。

生产环境建议同时设置：

```env
WEBUI_TOKEN=换成一个长随机字符串
WEBDAV_USERNAME=你的WebDAV用户名
WEBDAV_PASSWORD=你的WebDAV密码
```

### 3. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f tgdisk
docker compose logs -f botapi
```

### 4. 打开 WebUI

默认 compose 映射：

```text
http://服务器IP:6354/
```

如果设置了 `WEBUI_TOKEN`，第一次访问可带 token：

```text
http://服务器IP:6354/?token=你的WEBUI_TOKEN
```

页面会把 token 写入 `tgdisk_token` Cookie，后续 API 调用会自动携带。

### 5. WebDAV 地址

```text
http://服务器IP:6354/dav
```

如果配置了 `WEBDAV_USERNAME` / `WEBDAV_PASSWORD`，客户端会使用 Basic 或 Digest Auth 登录。

---

## 配置说明

所有配置都在 `.env` 中，参考 `.env.example`。

### 必填配置

| 变量 | 说明 |
|---|---|
| `BOT_TOKEN` | Telegram Bot Token。 |
| `ADMIN_IDS` | 管理员 Telegram 数字 ID，多个用逗号分隔。 |

Docker Compose 中如果启用自建 Bot API，还需要：

| 变量 | 说明 |
|---|---|
| `TELEGRAM_API_ID` | Telegram API ID。 |
| `TELEGRAM_API_HASH` | Telegram API Hash。 |

### 网络与代理

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PROXY` | 空 | 访问官方 `api.telegram.org` 时使用的 HTTP/SOCKS 代理。 |
| `ALL_PROXY` | 空 | `PROXY` 未设置时会读取。 |
| `LOCAL_API_BASE` | 空 | 自建 Bot API 地址，例如 `http://botapi:8081`。 |
| `LOCAL_API_MODE` | `false` | 是否启用 Bot API local mode 逻辑。 |

如果 `LOCAL_API_BASE` 已配置，TGDrive 访问的是自建 Bot API 服务，本身不会再套 `PROXY`，避免把内网地址错误转发到代理。

### Web / API 安全

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `WEBUI_TOKEN` | 空 | WebUI 与 `/api/*` 的 Bearer/query/cookie token。为空则不鉴权，生产环境不建议。 |
| `CORS_ALLOW_ORIGINS` | 空 | 允许跨域来源，逗号分隔。 |
| `WEBDAV_USERNAME` | 空 | WebDAV 用户名。为空则匿名。 |
| `WEBDAV_PASSWORD` | 空 | WebDAV 密码。为空则匿名。 |
| `RESUME_ALLOWED_DIRS` | 空 | `/api/resume-upload` 允许读取的服务器本地目录，逗号分隔。 |

### 数据与日志

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `DB_PATH` | `data/tgdrive.sqlite3` | SQLite 数据库路径。 |
| `PAGE_SIZE` | `20` | Bot 分页大小。 |
| `LOG_LEVEL` | `INFO` | 日志级别。 |

### 上传策略

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MAX_FILE_SIZE` | `0` | 应用层最大文件大小，单位 MB；`0` 表示不限。 |
| `TG_UPLOAD_CHUNK_SIZE_MB` | `18` / 示例为 `1500` | 非 local 分片上传大小。官方 Bot API 建议 18MB 左右。 |
| `TG_SINGLE_UPLOAD_THRESHOLD_MB` | 同分片大小 | 小于等于该阈值时走单次上传。 |
| `UPLOAD_CONCURRENCY` | `4` / 示例为 `1` | 非 local 分片并发数。旧分片模式内存峰值约为并发数 × 分片大小。 |
| `TG_LOCAL_SINGLE_UPLOAD_LIMIT_MB` | `1500` | local mode 下单文件直传阈值。 |
| `TG_LOCAL_PART_SIZE_MB` | `1500` | local mode 下超大文件 multipart 分片大小。 |

### WebUI 上传缓存

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `UPLOAD_CACHE_ENABLED` | `true` | 是否启用 WebUI 上传缓存。 |
| `UPLOAD_CACHE_DIR` | `data/cache` | 缓存文件目录。 |
| `UPLOAD_CACHE_MAX_SIZE_MB` | `10240` | 缓存总容量上限。 |
| `UPLOAD_CACHE_MAX_FILE_SIZE_MB` | `0` | 单个缓存文件上限，`0` 表示不限。 |
| `UPLOAD_CACHE_TTL_HOURS` | `24` | 失败、取消、未完成任务保留时长。 |
| `UPLOAD_CACHE_KEEP_AFTER_DONE` | `false` | Telegram 上传完成后是否保留缓存文件。 |
| `BROWSER_CHUNK_SIZE` | `8388608` | 浏览器上传到服务器缓存的分片大小，默认 8MiB。 |

### Bot API local mode 路径映射

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `BOT_API_SERVER_DIR` | `/var/lib/telegram-bot-api` | Bot API Server 返回的本地路径前缀。 |
| `BOT_API_LOCAL_DIR` | 同上 | TGDrive 容器/进程实际可读的本地路径前缀。 |

Docker Compose 默认把 `./data/botapi` 同时挂到 botapi 与 tgdisk 的 `/var/lib/telegram-bot-api`，所以两个值保持默认即可。

---

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
