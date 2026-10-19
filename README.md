# 校园气象预报自动编撰系统

> 每日自动采集**聊城市东昌府区**官方气象数据 → 生成人性化中文文案 → 推送到微信公众号。
> 研究范围：**山东省聊城市东昌府区**（聊城市唯一市辖区，覆盖聊城大学等主城区）。
> 深度学习预测部分：**按用户要求暂缓**（当前无服务器训练环境 + 历史数据不足，详见 `reports/04`）。

---

## 一、这是什么

一个由时间驱动的自动化工作流，每日定时完成：**采集 → 生成 → 推送**。

```
调度器（每日 07:30 触发）
   │
   ├─ ① 采集：weather.com.cn + nmc.cn（官方源，交叉校验）
   │        └─ 落库 data/db/weather.sqlite + 原始报文归档 data/raw/
   ├─ ② 生成：人性化中文播报（含《气象法》要求的来源署名）
   ├─ ③ 推送：订阅号每日群发（未配置凭证时默认只干跑，绝不发送）
   └─ ④ 告警：任务失败时通知（企业微信/Server酱/邮件，可配）
```

## 二、5 分钟跑起来

### 环境要求
- Python ≥ 3.10（已在 3.14 验证）
- Windows / macOS / Linux 均可

### 步骤

```bash
# 1. 进入项目目录
cd G:\气象预报平台

# 2. （可选但推荐）创建虚拟环境
python -m venv venv
# Windows: venv\Scripts\activate    macOS/Linux: source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 复制密钥模板（真实密钥绝不入库）
copy .env.example .env     # Windows
# cp .env.example .env     # macOS/Linux

# 5. 先跑离线自检（不联网、不推送，确认代码完好）
python main.py --selftest

# 6. 联网采集一次（确认官方数据源可达）
python main.py --collect

# 7. 生成干跑文案（不推送，人工审阅 logs/dryrun_*.md）
python main.py --dry-run
```

**从第 5 步起，5 分钟内即可看到系统真实运行效果。** 无需任何密钥即可完成采集、生成、干跑全流程（数据源为国家官方公开接口）。

## 三、常用命令

| 命令 | 作用 |
|---|---|
| `python main.py --selftest` | 离线全链路自检（新人第一步） |
| `python main.py --collect` | 仅采集并落库，不生成文案 |
| `python main.py --dry-run` | 完整流程但**不推送**，文案写 `logs/dryrun_*.md` |
| `python main.py --gzh-pack` | 生成**公众号推送包**（`logs/gzh_pack_*.md/.html`），人工 1 分钟手动群发，覆盖全部关注者 |
| `python main.py --samples` | 用官方真实预报数据预演生成多日样例文案 |
| `python main.py --push` | 立即执行一次真实推送（需先配置 .env） |
| `python main.py --push --preview-openid <openid>` | 只发给指定 openid 预览（不消耗群发额度，**上线前必用**） |
| `python main.py --status` | 查看数据库与配置状态 |
| `python main.py --serve` | 启动每日定时调度（07:30 推送，06:50 采集） |
| `python main.py --serve --dry-run` | 以干跑模式启动调度（每日只生成文案不推送） |

## 四、配置说明

### `.env`（密钥，绝不入库）

| 变量 | 用途 | 必填 |
|---|---|---|
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 公众号凭证 | 真实推送前必填 |
| `WECHAT_PUSH_CHANNEL` | 推送路线：`subscription`（订阅号，已选定）/ `template` / `wecom_webhook` / `dryrun` | 推送前必填 |
| `QWEATHER_API_HOST` / `QWEATHER_API_KEY` | 和风天气（生活指数增强，可选） | 否 |
| `ALERT_*` | 任务失败告警渠道（企业微信/Server酱/邮件） | 否 |

> ⚠️ **推送路线现状（2026-10-19 更新）**：
> 1. **公众号**：当前为**未认证订阅号**，微信 2025-07 起回收个人主体账号的
>    API 群发/发布权限（48001），故无法代码自动群发；但**网页后台手动群发
>    每天 1 次仍可用**。每日用 `--gzh-pack` 生成推送包 → 人工粘贴群发，
>    覆盖全部关注者（详见 `reports/06` 与 `logs/gzh_pack_*.md` 指引）。
> 2. **企业微信群机器人**：`WECHAT_PUSH_CHANNEL=wecom_webhook` 可全自动推送，
>    但仅覆盖群成员（适合校园班级/社团群）。两条路线可并行。
> 3. 若未来公众号完成认证（需企业/个体户等非个人主体资质），可切回
>    `subscription` API 全自动群发。

### `config.ini`（非敏感配置）

| 项 | 当前值 | 说明 |
|---|---|---|
| `wcc_city_id` | `101121701` | 聊城市（东昌府区）—— ⚠️ 已纠正蓝本的 `101120701`（那是济宁！） |
| `station_id` | `54806` | 聊城气象站号（WMO） |
| `push_hour/minute` | 07:30 | 每日推送时间（Asia/Shanghai） |
| `attribution_name` | 中央气象台·中国天气网 | 文案署名（《气象法》第二十五条，不可删除） |

## 五、目录结构

```
G:\气象预报平台\
├── main.py                # 命令行入口（selftest/collect/dry-run/push/serve）
├── config.ini             # 非敏感配置
├── .env.example           # 密钥模板（真实值填 .env，已 gitignore）
├── requirements.txt       # 核心依赖
├── requirements-wechat.txt# 微信推送路线依赖（选配）
├── src\
│   ├── weather_client.py      # 数据采集编排 + 多源归并 + 降级链
│   ├── content_generator.py   # 文案生成（含法规署名与合规拦截）
│   ├── wechat_manager.py      # 微信推送（订阅号三步流/预览/多路线）
│   ├── scheduler_main.py      # APScheduler 调度 + 告警升级
│   ├── daily_job.py           # 每日任务胶水层 + 合规闸门
│   ├── config.py              # 配置加载（ini + .env）
│   ├── alerting.py            # 失败告警（企微/Server酱/邮件）
│   ├── crawler\               # 数据源采集器（weather_com_cn / nmc_cn / qweather）
│   └── storage\               # SQLite 落库 + 原始报文归档
├── data\
│   ├── raw\               # 原始报文（按日期分目录，只增不改）
│   ├── db\weather.sqlite  # 结构化数据
├── reports\               # 调研/合规/数据/模型/部署 报告
├── logs\                  # 运行日志 + 干跑文案
├── tests\                 # pytest 测试（175 项，全通过）
└── THIRD_PARTY_LICENSES.md
```

## 六、合规要点（务必知晓）

- **《气象法》第二十五条**：传播气象预报必须使用气象主管机构所属台站提供的适时信息，**标明发布时间和台站名称**。→ 每条文案末尾自动附「信息来源 + 发布时间」，缺失即拒绝推送（测试覆盖）。
- **《气象预报发布与传播管理办法》第九条**：不得自行更改预报内容和结论。→ 官方天气现象/气温原样保留，仅做生活化转译。
- **《办法》第十条**：非气象台站不得发布气象预报结论。→ **本系统 AI/模型预测值永不对外推送**（深度学习暂缓的合规原因之一）。
- **robots.txt 与限速**：官方源均允许抓取；单域名请求间隔 ≥2 秒、失败熔断退避（详见 `reports/02`）。
- **密钥不落地**：所有密钥在 `.env`（已 gitignore），代码/日志/报告零密钥。

## 七、数据源

| 优先级 | 数据源 | 用途 | 合规 |
|---|---|---|---|
| 1 | 中央气象台 nmc.cn | 权威预报 + 预警 | ✅ 官方公开接口 |
| 2 | 中国天气网 weather.com.cn | 实况 + 逐时 + 指数（主通道） | ✅ 官方公开接口 |
| 5 | 和风天气 QWeather | 生活指数增强（可选项，待 Key） | ✅ 官方 API |

## 八、测试

```bash
python -m pytest tests -q
# 175 passed（含法规合规、解析器、采集器、编码表）
```

## 九、部署（摘要）

完整方案见 `reports/05_部署与运维手册.md`。推荐：**国内轻量云服务器（2G 内存即可）+ systemd 定时任务**，优于 PythonAnywhere（免费版有出站白名单与定时任务限制，且境外访问国内气象站/微信接口延迟高）。

---

**许可证与第三方声明**：见 `THIRD_PARTY_LICENSES.md`。本项目自身许可证由用户自行决定（未预设）。
