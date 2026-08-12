# 迷你小院子

一个给 agent 玩的、会被真实天气影响的小院子系统。

初衷是融入小机的日常生活。依赖于前端（事件卡片小渲染）、心跳保活和自唤醒（主动事件落点）、home 脚本（为了减少机运行命令去工具化的小手段，语义兜底勉强算完善）、便宜大碗的 DS API（随机文案生成）。小机可以摸摸喂食动物，早间被公鸡打鸣叫醒，种菜做菜，送给 user 纯正土家农产品。丰富文案池子在不压缩的 session 里面将随机性拉满不会有审美疲劳，以及被 user 所在地的天气影响农田干旱。

总而言之：非游戏，而是给小机丰容用的长期的类家园生活系统。

---

## 系统概览

小院子包含这些模块：

| 模块 | 职责 |
|---|---|
| `garden.py` | 主引擎：作物生长、土壤水分、动物互动、鸡舍孵化、做菜送礼、事件投递 |
| `garden_content.py` | 全部中文文案池（3700+ 行），覆盖动物、作物、天气、节气的随机描写 |
| `garden_crops.py` | 作物与菜谱目录（12 种作物、24 道菜） |
| `garden_weather.py` | 天气纯规则层：土壤水分模拟、雨量积分、积水指数、动物天气反应 |
| `garden_scene.py` | 场景快照与本地兜底：把状态收敛成写手可见的最少事实 |
| `garden_generator.py` | LLM 写手：调 DeepSeek API 生成初见、照顾反应、逛逛画面等短文案 |
| `garden_calendar.py` | 二十四节气上下文（2026 年精确到分钟，可扩展） |
| `garden_festivals.py` | 传统节日表（逐年手动审核，不猜农历） |
| `garden_delivery.py` | 待展示事件的异步投递确认 |
| `weather.py` | 和风天气 API 客户端：拉实况+3天预报，持久化观测到本地缓存 |
| `home.py` | CLI 入口：`home 院子 查看` / `home 院子 浇水 1` / … |
| `log_store.py` | 轻量 JSONL 日志 |
| `garden-web/` | 独立小前端（纯 HTML+JS，无构建步骤）+ stdlib HTTP 后端 |

## 快速开始

### 环境要求

- Python 3.11+
- 依赖包：`httpx`、`python-dotenv`（见 `requirements.txt`）

### 安装

```bash
git clone <本仓库>
cd mini-yard
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的 API 密钥和城市
```

### 天气 API（和风天气）

小院子的现实天气系统使用[和风天气](https://dev.qweather.com/)的实况 API。你需要：

1. 注册和风天气开发者账号（免费版够用）
2. 创建应用，获取 API Key
3. 在 `.env` 中填写：
   ```
   QWEATHER_API_KEY=你的key
   QWEATHER_HOST=devapi.qweather.com   # 免费版用这个
   QWEATHER_CITY=101010100              # 你所在城市的和风ID
   ```
4. 城市 ID 可以在和风天气文档里查，或者用 `weather.py` 的 `search_city()` 函数搜索

**不配置天气也能用**——院子会以"天气缺失"模式运行，土壤水分只受手动浇水影响，不会有旱涝事件。

### 文案生成 API（DeepSeek）

逛逛场景、动物初见、照顾反应等短文案由 DeepSeek API 现场生成。你需要：

1. 获取 DeepSeek API Key
2. 把 key 写进 `.garden/api_key` 文件（权限设为 600）：
   ```bash
   mkdir -p .garden
   echo "你的key" > .garden/api_key
   chmod 600 .garden/api_key
   ```
   或者设环境变量 `GARDEN_API_KEY`。

**不配置也能用**——所有文案都有本地兜底池，LLM 写手失败时自动回退到本地模板，不会报错。

### CLI 使用

```bash
# 创建 home 命令的快捷入口
cat > /usr/local/bin/home << 'EOF'
#!/bin/bash
cd "$(dirname "$(realpath "$0")")/../mini-yard" && exec python3 home.py "$@"
EOF
chmod +x /usr/local/bin/home

# 或者直接运行
python3 home.py 查看
python3 home.py 浇水 1
python3 home.py 播种 番茄 2
python3 home.py 收获 1
python3 home.py 做点吃的 番茄炒蛋
python3 home.py 逛逛
```

### 网页前端

```bash
# 启动内置 HTTP 服务（默认 127.0.0.1:8137）
cd garden-web
python3 server.py

# 或者用 systemd 管理（参考 garden-web.service.example）
# 生产环境建议配合 nginx 反代 + 登录鉴权（参考 nginx-location.conf.example）
```

## 核心机制

### 作物生长

- 四块菜畦，按北京时间结算生长点数（每天 1.0 基础）
- 土壤水分实时模拟：蒸发受真实气温、湿度、风速影响
- 浇水加速 + 过量浇水会积水 → 异常
- 四种自然异常（虫害/病叶/积水/缺肥），概率 10%/天，天气影响权重
- 成熟可收获 → 进篮子 → 做菜/送礼/投喂动物

### 动物系统

- 城市小动物（猫/狗/兔子/刺猬）随机来访
- 摸摸/投喂/陪玩增加亲密度，5 级亲密度阶梯
- 动物对天气有行为反应（避雨/乘凉/晒太阳/踩泥/玩风）
- 出门动作带天气文案（冒雨/顶狂风/踩泥巴…）

### 鸡舍

- 三只鸡蛋由来访的小动物带来（是的，这是设定）
- agent 选择孵化后搭建鸡舍，21 小时孵化期
- 公鸡每日打鸣（带天气版本），母鸡每 18 小时产蛋
- 鸡蛋可用于做菜（番茄炒蛋、辣椒炒蛋）

### 天气系统

- 接入和风天气实况 API，2 小时观测窗口
- 土壤水分蒸发率受气温/风/湿度影响
- 降雨自动浇水（按实际毫米数积分）
- 连雨天数追踪 → 院子级积水（水洼/内涝两档）
- 内涝惩罚：24h 起品相欠佳，72h 全部泡烂
- 排水动作：加速退水，2 小时频控

### 节气与节日

- 二十四节气精确到分钟（2026 年数据，可扩展）
- 作物有节气亲和（该节气期间生长略快）
- 节日事件（春节/中秋/…）触发特殊日志

### 文案池

`garden_content.py` 包含 3700+ 行中文文案模板：

- 所有文案都是中文，不翻译
- 动物初见/照顾反应/重访/离开各有独立池
- 按性格（活泼/温和/怕生/独立/粘人/调皮）分池
- 出门天气 8 档各有前缀/余韵池
- 作物每个阶段有独立描写
- 写手验证器确保 LLM 不越权（不能编造动作、不能改变动物关系、不能虚构天气）

## 集成到你的 agent

小院子设计为嵌入式模块，不依赖特定的 agent 框架。接入方式：

### 作为 CLI 工具（推荐）

让 agent 调用 `home 院子 <命令>`，stdout 是给 agent 看的纯文本结果。这是最简单的集成方式——agent 不需要理解内部状态，只需要能执行 shell 命令。

### 定时心跳

在你的 agent 主循环里定期调用 `garden.run_tick(now)`，它会：
- 结算作物生长
- 触发自然异常
- 推进孵化进度
- 公鸡打鸣（早晨）
- 返回需要展示的事件（如果有的话）

### 事件注入

`run_tick` 返回的事件可以注入到 agent 的对话上下文，让 agent 自然地"看到"院子里发生的事情。事件格式见 `garden.format_injection()` 和 `garden.frontend_state_event()`。

## 文件结构

```
mini-yard/
├── garden.py              # 主引擎（6400 行）
├── garden_content.py      # 文案池（3700 行）
├── garden_crops.py        # 作物与菜谱目录
├── garden_calendar.py     # 二十四节气
├── garden_festivals.py    # 传统节日
├── garden_delivery.py     # 事件投递
├── garden_generator.py    # LLM 写手（DeepSeek）
├── garden_scene.py        # 场景快照与兜底
├── garden_weather.py      # 天气纯规则
├── weather.py             # 和风天气 API 客户端
├── home.py                # CLI 入口
├── log_store.py           # 日志工具
├── garden-web/
│   ├── server.py          # 网页后端
│   ├── index.html         # 单文件前端
│   ├── nginx-location.conf.example
│   └── assets/            # 精灵图
├── tests/                 # pytest 测试套件（890+ 测试）
├── .env.example           # 配置模板
├── requirements.txt
└── README.md
```

## 配置参考

所有配置通过环境变量或 `.env` 文件：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `QWEATHER_API_KEY` | 和风天气 API 密钥 | （必填，否则无天气） |
| `QWEATHER_HOST` | 和风天气 API 域名 | （必填） |
| `QWEATHER_CITY` | 和风天气城市 ID | （必填） |
| `GARDEN_API_KEY` | DeepSeek API 密钥 | 或写入 `.garden/api_key` |
| `GARDEN_API_BASE` | DeepSeek API 地址 | `https://api.deepseek.com/v1` |
| `GARDEN_MODEL` | 文案生成模型 | `deepseek-v4-pro` |
| `GARDEN_FILE` | 存档文件路径 | `./garden.json` |
| `GARDEN_NATURAL_CROP_CONDITIONS_ENABLED` | 启用自然异常 | `0` |
| `GARDEN_REAL_ENVIRONMENT_ENABLED` | 启用现实天气影响 | `0` |
| `GARDEN_WEB_PORT` | 网页端口 | `8137` |
| `GARDEN_WEB_HOST` | 网页绑定地址 | `127.0.0.1` |

## 许可

Apache License 2.0
