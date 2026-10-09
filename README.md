# 学习积累平台 v0.1

> 给 13 岁孩子用的本地化学习错题平台

## 功能

| 模块 | 状态 | 说明 |
|------|------|------|
| 📷 拍照录入 | ✅ | 手机/电脑上传, RapidOCR 离线识别 |
| 📖 错题本 | ✅ | 按学科/状态筛选, 6 标签详情(题面/图片/AI/搜题/复习/编辑) |
| 🔁 复习计划 | ✅ | 艾宾浩斯 1/2/4/7/15/30 天自动计算到期题 |
| 🧠 知识点图谱 | ✅ | 按学科聚合, 高频错标红 |
| 📊 学习统计 | ✅ | 总数/学科分布/近 30 天趋势 |
| 👨‍👩‍👧 家长端 | ✅ | PIN 登录, 监控/导出/重置 |
| 🤖 AI 讲题 | 🔌 预留 | 接 DeepSeek/通义/智谱/OpenAI 即可启用 |
| 🌐 联网搜题 | ✅ | DuckDuckGo 免 API key |

## 启动

### 方式一: Windows 一键脚本 (推荐)

| 脚本 | 用途 | 运行方式 |
|------|------|---------|
| `install.bat` | 首次装依赖 + 初始化数据库 | 双击 |
| `open_firewall.bat` | 放行 8501 端口, 供手机/平板访问 | **右键 → 以管理员身份运行** |
| `start.bat` | 启动服务 (`0.0.0.0:8501`, 支持局域网) | 双击 |
| `export.bat` | 打包成 zip 方便搬到别的电脑 | 双击 |

### 方式二: 命令行

```bash
pip install -r requirements.txt
streamlit run app.py
```
默认打开 http://localhost:8501

### 手机 / 平板访问 (同一 WiFi)

1. 先运行一次 `open_firewall.bat` (管理员权限) 放行端口
2. 双击 `start.bat` 启动
3. 手机浏览器打开: **http://192.168.3.228:8501**

> 若不通, 常见原因: ①手机与电脑不在同一 WiFi ②路由器开了"AP 隔离"(访客网络), 换主网络即可
> 电脑 IP 变了: 在 cmd 里跑 `ipconfig` 看 "IPv4 地址" 那一行; 建议路由器里给电脑绑定静态 IP

## 启用 AI 老师

### 本地运行 — 设置环境变量
```bash
# Windows CMD
set STUDY_LLM_PROVIDER=openai
set STUDY_LLM_API_KEY=sk-xxx
set STUDY_LLM_MODEL=deepseek-chat
set STUDY_LLM_BASE_URL=https://api.deepseek.com

# 装 openai SDK
pip install openai
```

### 云端部署 (Streamlit Cloud) — 在 Secrets 页面配置
App → Settings → Secrets, 粘贴:
```toml
STUDY_LLM_PROVIDER = "openai"
STUDY_LLM_API_KEY = "sk-xxx"
STUDY_LLM_MODEL = "deepseek-chat"
STUDY_LLM_BASE_URL = "https://api.deepseek.com"
```

### 推荐服务商
| 服务 | base_url | model | 价格 |
|------|----------|-------|------|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | ¥1/百万token |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-turbo` | ¥3/百万token |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 免费额度 |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | 美元 |

Provider 一律填 `openai` (都用 OpenAI 兼容接口), 通过 base_url 区分服务商。

## ☁️ 云端部署 (15 分钟, 免费, 任意地点访问)

适用场景: 孩子不在家 / 学校 / 路上想用 → 部署到 [share.streamlit.io](https://share.streamlit.io) 拿公网 URL

### 步骤 1: 注册 GitHub
1. 打开 https://github.com 注册账号
2. 新建一个仓库, 名字叫 `study-platform`, 选 **Public**(公开,免费), 不要勾选"Add README"

### 步骤 2: 推送代码到 GitHub
在项目根目录 `study-platform/` 下, 跑这些命令(替换 `<你的GitHub用户名>`):

```bash
cd study-platform
git init
git add .
git commit -m "init: 学习积累平台 v0.1"
git branch -M main
git remote add origin https://github.com/<你的GitHub用户名>/study-platform.git
git push -u origin main
```

> 推送时弹出 GitHub 登录框, 用浏览器授权(PAT token 或 GitHub Desktop 都行)
> 不会用 Git? 下载 [GitHub Desktop](https://desktop.github.com) 图形化操作

### 步骤 3: 在 Streamlit Cloud 部署
1. 打开 https://share.streamlit.io
2. 用 GitHub 账号登录
3. 点 "New app" → 选 `你的用户名/study-platform` 仓库
4. Branch 填 `main`, Main file path 填 `app.py`
5. 点 Deploy! → 2-3 分钟后给你一个 URL, 类似 `https://xxx.streamlit.app`

### 步骤 4 (可选): 配置 LLM
App 跑起来后, 你的 App 页面右上 ⋯ → Settings → Secrets, 粘贴上面的 Secrets 配置, Save → 自动重启生效

### ⚠️ 云端数据持久化说明
- Streamlit Cloud 给每个 App 分配持久磁盘, SQLite 数据和上传图片**重启不会丢**
- 但 app 闲置 7 天无访问会进入休眠, 重新访问会冷启动(慢 10-30 秒)
- **重要数据建议每周导出一次** (家长端 → 数据导出 CSV)

## 🩺 常见排错

### 云端 OCR 报错 `libGL.so.1: cannot open shared object file`

**原因**: Streamlit Cloud 跑在 Linux 上, `rapidocr` 依赖的 opencv 需要系统库 `libGL.so.1`, 云端基础镜像不带。

**修复**: 仓库根目录必须存在 `packages.txt` (已包含):
```
libgl1
libglib2.0-0
libgomp1
libsm6
libxext6
libxrender1
```
Streamlit Cloud 会在构建时自动 `apt-get install` 这些包。改完推送到 GitHub 后:
- App 一般会**自动重新部署**(1-3 分钟)
- 没动静就手动触发: App 页面右上 `⋮` → **Reboot**

### 侧边栏一直显示 `AI 老师: 离线 (offline)`

这是**正常默认状态**, 不是报错 —— 表示还没配 LLM API。按上面「启用 AI 老师 → Secrets」配好即可。
> 注意: 配完 Secrets 后必须 **Reboot** 应用, 因为配置在模块导入时读取。

### 本地 `.bat` 双击闪退 / 提示"不是内部或外部命令"

含中文的 `.bat` 必须存为 **GBK/ANSI** 编码。用 UTF-8 会让 cmd 解析失败。脚本里已加 `chcp 936`。

## 文件结构

```
study-platform/
├── app.py              # Streamlit 主应用
├── db.py               # SQLite 数据层
├── ocr.py              # RapidOCR 封装
├── llm.py              # LLM 抽象层
├── web_search.py       # 联网搜题
├── requirements.txt    # pip 依赖
├── packages.txt        # 云端 apt 系统依赖 (必须, 修 libGL)
├── .streamlit/         # 云端主题/端口配置
├── install.bat         # 首次装依赖 (Windows)
├── open_firewall.bat   # 放行 8501 端口 (需管理员)
├── start.bat           # 一键启动 (Windows)
├── export.bat          # 打包成 zip
├── data/               # SQLite 数据库
└── uploads/            # 拍照图片
```

> ⚠️ 修改 `.bat` 脚本时注意: **含中文的 .bat 必须保存为 GBK/ANSI 编码**, 用 UTF-8 会导致 cmd 中文乱码、命令解析失败。

## 数据安全

- 所有数据存本机 SQLite, 不上传任何服务器
- AI 调用走用户自己的 API key, 孩子题目只在调用时发给 LLM 服务方
- 联网搜题用 DuckDuckGo HTML 接口, 题目不发给 Google/Bing

## 后续可扩展

- 家长推送 (邮件/微信)
- 多孩子账号
- 错题智能分类 (按学段/教材版本)
- 每日/周学习报告
- 拍照自动识别学科 (LLM 二次判断)
