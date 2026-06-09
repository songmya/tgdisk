# TGDrive - Telegram Bot 网盘

基于 Telegram Bot 的轻量网盘服务。文件存储在 Telegram 服务器，本地 SQLite 保存文件索引和目录结构。

## 功能

- 📤 上传文件：直接发送文件给 Bot
- 📋 浏览文件：`/ls [路径]`
- 🔍 搜索文件：`/search 关键词`
- 📥 下载文件：`/get 文件ID`
- 🗑️ 删除索引：`/del 文件ID`
- 📁 目录管理：`/mkdir 目录名`、`/mv 文件ID 目标路径`
- ℹ️ 文件详情：`/info 文件ID`
- 📊 统计信息：`/stats`
- 🔐 白名单用户控制

## 快速开始

### 1. 安装依赖

```bash
cd /vol1/1000/openclaw/tgdisk
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入你的配置：

```bash
cp .env.example .env
```

必填项：
- `BOT_TOKEN`：从 @BotFather 获取的 Bot Token
- `ADMIN_IDS`：管理员 Telegram ID（逗号分隔）

可选项：
- `PROXY`：代理地址，如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`
- `DB_PATH`：数据库路径，默认 `data/tgdrive.sqlite3`
- `MAX_FILE_SIZE`：最大文件大小 MB，默认 2000

### 3. 运行

```bash
python bot.py
```

或使用 systemd 服务（推荐）：

```bash
# 见部署章节
```

## 命令列表

| 命令 | 说明 | 示例 |
|------|------|------|
| `/start` | 开始使用 | `/start` |
| `/help` | 帮助信息 | `/help` |
| `/ls [路径]` | 列出文件 | `/ls /books` |
| `/search 关键词` | 搜索文件 | `/search python` |
| `/get ID` | 下载文件 | `/get 123` |
| `/del ID` | 删除文件 | `/del 123` |
| `/info ID` | 文件详情 | `/info 123` |
| `/mkdir 名称` | 创建目录 | `/mkdir books` |
| `/mv ID 路径` | 移动文件 | `/mv 123 /books` |
| `/tag ID 标签` | 打标签 | `/tag 123 教程` |
| `/stats` | 统计信息 | `/stats` |

## 代理配置

国内机器无法直连 Telegram API，需要配置代理。支持三种方式：

### 方式一：.env 配置（推荐）

```env
PROXY=http://127.0.0.1:7890
# 或 socks5
PROXY=socks5://127.0.0.1:1080
```

### 方式二：环境变量

```bash
export PROXY=http://127.0.0.1:7890
python bot.py
```

### 方式三：ALL_PROXY

```bash
export ALL_PROXY=socks5://127.0.0.1:1080
python bot.py
```

## 部署

### systemd

```bash
sudo cp tgdisk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tgdisk
sudo systemctl start tgdisk
```

### Docker

```bash
docker compose up -d
```

## 注意事项

- 文件实际存储在 Telegram 服务器，删除索引 ≠ 删除 Telegram 上的文件
- Bot API 文件大小限制约 20MB（自建 Bot API Server 可提升至 2GB）
- file_id 在不同 bot 间不通用，不要换 bot token
- 不建议存储特别敏感的文件（证件、密钥等）
- 建议定期备份 `data/tgdrive.sqlite3`

## WebUI 上传缓存

WebUI 默认启用“先缓存、后后台上传 Telegram”的模式：浏览器按分片把文件上传到服务器缓存目录，支持真正暂停/继续；后端随后从缓存文件分片上传到 Telegram。这样页面可以展示两个阶段的进度：

1. 上传到服务器缓存
2. 上传到 Telegram 分片

相关环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UPLOAD_CACHE_ENABLED` | `true` | 是否启用 WebUI 上传缓存；关闭后回退旧的流式上传模式 |
| `UPLOAD_CACHE_DIR` | `data/cache` | 缓存文件目录 |
| `UPLOAD_CACHE_MAX_SIZE_MB` | `10240` | 缓存总容量上限，单位 MB |
| `UPLOAD_CACHE_MAX_FILE_SIZE_MB` | `0` | 单个缓存文件上限，0 表示不限 |
| `UPLOAD_CACHE_TTL_HOURS` | `24` | 失败/取消/未完成任务的缓存保留时间 |
| `UPLOAD_CACHE_KEEP_AFTER_DONE` | `false` | 上传成功后是否保留缓存文件；默认成功后自动删除 |
| `BROWSER_CHUNK_SIZE` | `8388608` | 浏览器上传到服务器缓存时的分片大小，默认 8MiB |

注意：缓存会让原始文件短暂落盘。请确保 `UPLOAD_CACHE_DIR` 不对外暴露，并按磁盘容量设置合理的 `UPLOAD_CACHE_MAX_SIZE_MB`。

## 回收站

WebUI 和 WebDAV 删除文件时都会进入回收站，而不是立刻彻底删除索引。

- WebUI：点击“回收站”可查看已删除文件、恢复文件或彻底删除索引
- WebDAV：客户端 DELETE 文件会标记为 `deleted=1`，并记录 `deleted_by=webdav`
- 恢复：`POST /api/trash/{file_id}/restore`
- 彻底删除索引：`DELETE /api/trash/{file_id}`

注意：彻底删除会先尽力调用 Telegram `deleteMessage` 删除原始上传消息/分片消息，再删除本地 SQLite 索引。但 Bot API 不能按 `file_id` 直接删除 Telegram 服务器文件，且 `deleteMessage` 可能受时间/权限限制，所以无法 100% 保证 Telegram 服务器文件被清除。

### 自建 Telegram Bot API 与分片大小

官方 Bot API 的 `sendDocument` 文件大小限制较低，所以默认 `TG_UPLOAD_CHUNK_SIZE_MB=18`，给 multipart/form-data 预留余量。
如果使用自建 `telegram-bot-api`，可以同时配置 API 地址和更大的分片，例如：

```env
LOCAL_API_BASE=http://127.0.0.1:8081
LOCAL_API_MODE=true
TG_UPLOAD_CHUNK_SIZE_MB=512
TG_SINGLE_UPLOAD_THRESHOLD_MB=512
UPLOAD_CONCURRENCY=2
```

分片越大，请求数越少，但上传并发内存也会增加，峰值大约是：

```text
UPLOAD_CONCURRENCY × TG_UPLOAD_CHUNK_SIZE_MB
```

例如 `512MB × 2` 约需要 1GB 以上可用内存。
