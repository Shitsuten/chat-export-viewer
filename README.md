# Agent App

一个自建的 Claude 风格聊天前端，后端只通过 OpenAI-compatible / Anthropic API 与模型对话，也可以导入 Claude / ChatGPT 导出的历史记录并继续聊天。

手机 → 你的服务器 → 外部模型 API

## 功能

- 聊天界面（对标 claude.ai 官端体验）
- 多会话管理（新建、切换、重命名、删除）
- 记忆系统（Profile、Saved Memories、Preferences）
- 文件上传（图片等）
- 模型切换
- Claude / ChatGPT export 导入与归档浏览
- 导入 Claude export zip 时自动同步 conversations、design chats、memories、project memories、thinking summaries、tool traces
- OpenAI-compatible / Anthropic 外部 API 续聊
- GitHub Pages 浏览器版：本地解析 export zip、IndexedDB 保存会话，并可直连 OpenAI-compatible / Anthropic API 续聊
- Clawd 彩蛋（0.1% 概率出现像素螃蟹动画）

## 环境要求

- Python 3.12+
- Node.js（仅用于 marked.js，已内置）

## 快速开始

### 1. 克隆 & 安装依赖

```bash
cd /your/path/agent-app
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入：

```
CHAT_PASSWORD=随便设一个前端访问密码
CHAT_SECRET=随便设一个32字符以上的密钥（用于session签名）

# OpenAI-compatible 默认 API
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=replace-with-your-openai-compatible-key
OPENAI_MODEL=gpt-4.1-mini

# 或 Anthropic API
# ANTHROPIC_API_KEY=replace-with-your-anthropic-api-key
# ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

也可以不在 `.env` 写 API key，进入网页后在 `External API` 面板里保存 provider、endpoint、model 和 key。不要提交个人认证凭据。

### 3. 可选环境变量

默认项目根目录是当前目录；如需覆盖：

```bash
AGENT_APP_ROOT=/path/to/agent-app
AGENT_APP_MEMORY_PATH=/path/to/CLAUDE.md
```

外部 API 可在界面里配置，也可用环境变量作为默认值：

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini

ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

### 4. 可选：创建 CLAUDE.md

在项目根目录（或你指定的路径）创建 `CLAUDE.md`，写入你想保留的系统提示 / 人设 / 记忆。Profile、Saved Memories、Preferences 会注入到每次 API 对话中。

### 5. 启动

```bash
# 方式一：直接启动
source .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8787

# 方式二：用 run.sh
bash run.sh
```

### 6. 访问

- 本地：浏览器打开 `http://localhost:8787`
- 手机（同一 WiFi）：`http://你电脑的内网IP:8787`
- 手机（远程）：安装 Tailscale，通过 Tailscale IP 访问

## 导入聊天记录

在侧栏点 `Import chats`，可以上传：

- Claude 官方导出的 zip / tar / tar.gz
- Claude export 解压后的 `conversations.json`
- `design_chats/*.json`
- `memories.json`
- ChatGPT `conversations.json`
- 普通 `.jsonl` / `.txt` / `.md` transcript

如果上传的是 Claude 官方 export zip，导入器会自动解析所有相关内容：

- conversations 和 design chats 写入 Recents
- `memories.json` 的 `conversations_memory` 写入 Saved memories
- `project_memories` 写入 Preferences
- assistant thinking 写入 Thought process block
- thinking summaries 优先使用导出中自带 summary
- tool_use / tool_result 写入工具 trace

## GitHub Pages 浏览器版

`docs/index.html` 是一个纯静态 demo，可以直接用 GitHub Pages 发布：

1. 在仓库设置里打开 `Pages`
2. Source 选 `Deploy from a branch`
3. Branch 选 `main`，目录选 `/docs`
4. 访问 `https://你的用户名.github.io/仓库名/`

这个版本只在浏览器内读取用户选择的 zip / json 文件：

- 不上传文件
- 会话、memories、projects 和 Profile 保存在当前浏览器的 IndexedDB
- 支持预览 conversations、design chats、thinking、tool traces、memories、projects
- 可在 `External API` 中配置 OpenAI-compatible 或 Anthropic Provider，并从浏览器直接续聊
- API key 保存在当前浏览器的 localStorage，适合个人设备上的调试

浏览器直连受 Provider 的 CORS 策略约束。新附件上传、服务端工具调用和服务器环境变量仍只在 FastAPI 部署版中提供。

## 项目结构

```
app/
  main.py          FastAPI 主入口，所有 API 路由
  export_importer.py Claude / ChatGPT / JSONL / transcript 导入解析器
  store.py         SQLite 存储（会话、消息）
  memory.py        记忆系统（Profile、Preferences）
  auth.py          认证
  ...
static/
  index.html       前端（单文件 SPA）
  design-system.css
docs/
  index.html       GitHub Pages 浏览器直连版
```

## 致谢

- [xixicc186/clawd-emotes-skill](https://github.com/xixicc186/clawd-emotes-skill) — Clawd 像素螃蟹 SVG 动画
