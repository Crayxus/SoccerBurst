# SoccerBurst ⚽

足球竞彩盘口异动扫描工具。自动监控当天竞彩比赛（sporttery.cn）的 Crow* 亚让赔率变化，在开赛前40分钟内发现异动时发出报警。

## 功能

- 从 **sporttery.cn** 获取当天竞彩比赛列表
- 按联赛权重（五大联赛优先）自动选取前3场扫描
- 监控 **Crow*** 公司亚让赔率历史变化
- 仅在"即"状态（开赛前约40分钟）触发报警
- 同盘口赔率变化 ≥ 0.10 时报警
- 每5分钟自动扫描，网页实时展示

## 本地运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 启动

```bash
python app.py
```

访问 http://localhost:5000

### Windows 一键启动

双击 `start.bat`

---

## 部署到云端（让别人访问）

> ⚠️ **重要限制**：本项目使用 Playwright 控制 Chromium 浏览器抓取数据，需要在服务器上安装 Chromium。大多数免费云平台（如 Vercel、Netlify）**不支持**。

### 推荐方案：Railway / Render / VPS

#### 方案A：Railway（推荐，有免费额度）

1. 注册 [Railway](https://railway.app)
2. 新建项目 → 从 GitHub 导入
3. 添加环境变量（如需要）
4. Railway 会自动检测 Python 项目并部署

**注意**：需要在 `Dockerfile` 中安装 Chromium（见下方）

#### 方案B：Render

1. 注册 [Render](https://render.com)
2. 新建 Web Service → 连接 GitHub 仓库
3. Build Command: `pip install -r requirements.txt && playwright install chromium`
4. Start Command: `python app.py`

#### 方案C：VPS（最稳定）

在任意 Linux VPS 上：

```bash
git clone https://github.com/你的用户名/SoccerBurst.git
cd SoccerBurst
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
python app.py
```

用 nginx 反向代理到 5000 端口即可公网访问。

---

### Dockerfile（用于 Railway/Docker 部署）

```dockerfile
FROM python:3.11-slim

# 安装 Chromium 依赖
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
```

---

## 联赛权重说明

| 权重 | 联赛 |
|------|------|
| 5 | 西甲、英超、德甲、意甲、法甲、欧冠 |
| 4 | 英冠、葡超、荷甲、比甲、苏超、欧联 |
| 3 | 西乙、德乙、意乙、法乙、欧会 |
| 2 | 韩职、日职、中超 |
| 1 | 其他 |

## 报警逻辑

1. 只扫描当天竞彩比赛中权重最高的前3场
2. 只有"即"状态记录（开赛前约40分钟）才触发报警
3. 同盘口下，主队或客队赔率变化 ≥ 0.10 → 报警 ⚠️
