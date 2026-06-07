# 交易所每日会员持仓数据系统

当前项目主线是建设一个可持续运行的中国期货交易所每日持仓与合约市场规模系统。

现阶段只正式支持：

- 上海期货交易所（SHFE）
- 数据表单：日交易排名
- 会员排名接口：`/data/tradedata/future/dailydata/pmYYYYMMDD.dat`
- 合约日行情接口：`/data/tradedata/future/dailydata/kxYYYYMMDD.dat`
- 结算参数接口：`/data/tradedata/future/dailydata/jsYYYYMMDD.dat`

原 `quant_platform/` 量化交易平台已暂停开发并进入废弃状态，不再作为项目主线，也不应继续增加功能。

## 当前能力

- 将上期所会员成交量、多头持仓、空头持仓统一存入 SQLite。
- 保存每日合约结算价、交易所持仓量、成交量和保证金率。
- 按合约乘数计算每日合约名义持仓市值与保证金估算。
- 增量更新时只下载数据库中缺失的工作日。
- 周末自动跳过；节假日无数据日期经过三次确认后停止重试。
- 排名仅保留 `1-20`，过滤汇总行和空会员。
- 从 SQLite 聚合生成本地 HTML 看板。
- 会员某交易日未进入排名时，持仓变化图按 `0` 展示。
- 网页支持按会员、品种和指标查看趋势与排行；明细历史通过 member_daily.json 懒加载，单文件 index.html 仅保留骨架。
- Windows 计划任务在工作日收盘后自动更新。

## 项目结构

```text
futures_positions/
├── adapters.py       # 上期所官方接口解析（其他交易所旧适配器保留，未纳入正式验收）
├── database.py       # SQLite 表结构、导入、查询与聚合
├── market_data.py    # 合约日行情、结算参数与合约乘数
├── system.py         # 统一命令行入口和增量更新服务
├── shfe_report.py    # HTML 看板模板与旧批量报告兼容入口
├── models.py         # ExchangeData 等共享数据模型
├── reports.py        # 品种 Top5 席位汇总与 DeepSeek AI 报告生成
└── utils.py          # 网络请求与数据清洗工具
.env                  # 项目本地密钥（DEEPSEEK_API_KEY/MODEL），已加入 .gitignore

scripts/
├── run_positions_daily.ps1   # 每日增量更新脚本
└── install_daily_task.ps1    # Windows 计划任务安装脚本

data/
└── shfe_positions.sqlite     # 主数据库，已在 .gitignore 中忽略

output/shfe_system/
├── index.html                # SQLite 数据库驱动的统一看板（精简后约 300 KB）
├── member_daily.json         # 会员 × 品种 × 交易日历史，由 index.html 懒加载
└── daily_update.log          # 每日更新日志
```

## 安装

```powershell
cd C:\Users\82505\Documents\金融信息
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 日常使用

查看数据库状态：

```powershell
python -m futures_positions status
```

只下载缺失交易日：

```powershell
python -m futures_positions update --start-date 2024-05-20
```

只补充缺失交易日的合约行情与结算参数：

```powershell
python -m futures_positions market-update --start-date 2024-05-20
```

增量更新并重建网页：

```powershell
python -m futures_positions daily --start-date 2024-05-20
```

生成某个品种近 N 日前 5 席位持仓报表（可选调用 DeepSeek 生成中文分析）：

```powershell
python -m futures_positions report --product 铜 --days 5 --ai --output output/report_cu5d.json
```

- `--days` 只允许 `5` / `10` / `15`。
- `--ai` 会调用 DeepSeek 产生 200–350 字的中文分析，默认模型 `deepseek-v4-pro`，API key 从项目根 `.env` 读取。
- 可用 `--model deepseek-v4-flash` 切换到更快的非推理型模型。
- 不加 `--output` 则输出到 stdout。

网页默认聚合数据库内全部历史。需要限制页面历史范围时，可以使用：

```powershell
python -m futures_positions dashboard --days 730
```

启动本地网页：

```powershell
python -m futures_positions serve --directory output\shfe_system --port 8765
```

然后访问：

```text
http://127.0.0.1:8765/
```


## 看板 AI 报表

看板顶部提供「生成 AI 报表」按钮，配合品种下拉与 5/10/15 日回滑窗口：

- 点击后弹出模态框，展示该品种近 N 个交易日的前 5 席位汇总。
- 每个交易日列出：多头总计、空头总计、净多空、三者的日度变化（与前一个交易日差值），以及 Top5 会员名单。
- 同步调用 DeepSeek 生成 200–350 字的中文纯文本分析。
- 后端调用 `/report?product=<品种>&days=<5|10|15>&ai=1`，该接口也可用 curl 或其他 HTTP 客户端调用。
- 未设置 `DEEPSEEK_API_KEY` 时，表格仍会正常生成，AI 部分会提示配置提示。

配置 DeepSeek API key（**项目本地、不进 Git**）：

在项目根目录创建 `.env` 文件，填入：

```text
DEEPSEEK_API_KEY=sk-...你的key
DEEPSEEK_MODEL=deepseek-v4-pro
```

- `.env` 已在 `.gitignore` 中排除，不会被提交。
- `futures_positions/reports.py` 会在导入时自动加载项目根的 `.env`，并以 `os.environ.setdefault` 的方式填入环境变量。
- 不需要在 Windows 系统/用户环境变量中设置，避免 key 泄漏到其他项目。
- 如需临时覆盖，仍可用 `$env:DEEPSEEK_API_KEY = "..."` 或 `set DEEPSEEK_MODEL=deepseek-v4-flash`，会优先于 `.env`。

当前默认模型：`deepseek-v4-pro`（推理型，会在可见输出前进行思考，调用耗时约 15–30 秒）。可选选项：

| 模型 ID | 说明 |
| --- | --- |
| `deepseek-v4-pro` | 推理型，质量高但耗时较长（默认） |
| `deepseek-v4-flash` | 非推理型，响应快、便宜，适合日常快速看板 |

CLI 可以用 `--model` 指定，例如：

```powershell
python -m futures_positions report --product 铜 --days 5 --ai --model deepseek-v4-flash
```
## 历史数据导入

可以将旧 CSV 导入 SQLite。重复导入不会产生重复数据：

```powershell
python -m futures_positions import-csv output\shfe_prev_1y\*.csv output\shfe_1y\*.csv
```

## 每日自动更新

安装 Windows 计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1
```

任务名称：

```text
SHFE Daily Positions Update
```

运行时间：

```text
周一至周五 18:30
```

任务会执行 `scripts/run_positions_daily.ps1`，增量更新会员排名、合约行情、结算参数，并重建 `output/shfe_system/index.html`。

## SQLite 数据结构

核心表：

- `positions`：会员持仓排名明细。
- `sync_status`：每个自然日的采集状态和重试次数。
- `contract_daily_market`：每日合约结算行情与交易所持仓量。
- `contract_settlement_params`：每日合约保证金率与结算参数。
- `contract_specs`：带生效日期的品种合约乘数。
- `market_sync_status`：合约行情采集状态。
- `metadata`：系统元数据。
- `contract_market_value`：实时计算合约市场规模的 SQLite 视图。

`positions` 主键为：

```text
trade_date + exchange + contract + rank + metric
```

因此重复运行和重复导入都是幂等的。

## 市场规模计算口径

名义持仓市值：

```text
交易所持仓量 × 当日结算价 × 合约乘数
```

投机保证金估算（多空双边）：

```text
名义持仓市值 ×（投机多头保证金率 + 投机空头保证金率）
```

套保保证金估算（多空双边）：

```text
名义持仓市值 ×（套保多头保证金率 + 套保空头保证金率）
```

名义持仓市值适合比较不同合约的市场规模。保证金估算适合观察资金沉淀趋势，但不等同于真实缴存保证金，因为实际值还受投机/套保结构、组合优惠和期货公司加收影响。

## 测试

运行持仓系统测试：

```powershell
python -m pytest tests\test_positions_system.py -q
```

运行全部现存测试：

```powershell
python -m pytest -q
```

当前持仓系统测试覆盖：

- 上期所官方接口排名过滤。
- 空会员与无效排名过滤。
- 增量更新只下载缺失日期。
- 合约日行情与结算参数解析。
- 合约乘数、结算价和交易所持仓量的市值计算。
- SQLite 幂等写入。
- 会员缺失交易日补零。
- 无数据日期重试策略。
- 品种 Top5 汇总与日度变化、DeepSeek 调用 payload 与错误处理。
- `.env` 加载、`DEEPSEEK_MODEL` 环境变量优先级、推理模型不发 `response_format`。

## 关于 quant_platform/

`quant_platform/` 是早期尝试搭建的事件驱动量化回测/模拟/CTP 平台，目前**已停止开发并归档**：

- 不再作为项目主线，也不应继续增加功能。
- 历史阶段文档保留在 `docs/quant_platform_phase1.md` 至 `docs/quant_platform_phase38.md`。
- 既有回测/回放产物仍留在 `output/backtests/`、`output/replays/`、`output/web_runs/` 等目录，仅作为参考。
- 后续治理计划见本文「进一步规划」一节。

## 当前边界

- 当前只正式维护上期所。
- 其他交易所旧适配器仍保留在代码中，但未纳入正式验收。
- SQLite 数据库与输出文件不提交到 Git。
- Git 基线提交将在本轮项目验收成功后执行。

## 进一步规划

下述事项按优先级排列，单次工作日可以完成 1–2 项。

1. **Git 基线提交**：当前仓库 `master` 分支没有任何 commit，所有目录都是 `??` 未跟踪。完成验收后做首次提交。
2. **归档 quant_platform/**：把 `quant_platform/`、`docs/quant_platform_phase*.md`、`output/backtests/`、`output/replays/`、`output/web_runs/`、`output/paper/`、`output/optimizations/`、`output/data_quality/` 迁到 `attic/` 子目录或单独分支，主线仓库只保留持仓系统。
3. **看板进一步瘦身**：考虑把 `member_daily.json` 压缩为按月份分片（`member_daily_YYYYMM.json`）或加 gzip 预压缩；前端按需 fetch。
4. **每日产物签名**：`index.html` 和 `member_daily.json` 在每次重建后写入 SHA-256 校验文件到 `output/shfe_system/`，方便快速核对完整性。
5. **数据库备份策略**：`data/shfe_positions.sqlite` 已达 818 MB，但仅有本地一份。引入每日 `VACUUM INTO` 形成增量快照（保留 14 天滚动）。
6. **统一时间戳格式**：现有 `fetched_at` 用 ISO 带时区，但部分历史 CSV 用裸日期；在导入时统一为 `+08:00`。
7. **HTML 报告模块化**：`shfe_report.py` 单文件已超过 700 行，把 CSS/JS 抽到 `futures_positions/web_assets/`便于将来分品种看板扩展。
8. **测试补强**：补 `system.py`（命令行 dispatcher、serve 子命令）的端到端测试，以及 `database.dashboard_payload` 在大日期窗口下的回归测试。
9. **其他交易所正式化**：评估 INE/CZCE/DCE/CFFEX 的官方接口对账与幂等写入，按 SHFE 模板逐一纳入正式验收。
