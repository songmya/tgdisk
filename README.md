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
