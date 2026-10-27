# 算净利润（suan-jing-li-run）

TikTok Shop 跨境电商（马来西亚 / 菲律宾 / 泰国）**按商品（链接）核算净利润**的本地工具。

把原来「人工导出 → 手工填四列 → Excel 拉公式」的流程，换成「取数 → 核算 → 出报表」一条流水线，
三种运行方式（命令行 / 双击 / 定时任务）共用同一套逻辑。

---

## 一、快速开始

### 方式一：命令行（最灵活）

```bash
# 读主工作簿里的原始表，算全量
python -m src.cli --workbook "V1-净利  -15.xlsx" --country 马来西亚

# 指定区间（任意起止，哪怕只算两天）
python -m src.cli --workbook "V1-净利  -15.xlsx" --from 2026-10-01 --to 2026-10-15

# 读导出目录（推荐：手工导出后放到一个文件夹即可）
python -m src.cli --dir data/exports/demo-my/20261001-20261015 --costs "V1-净利  -15.xlsx"

# 分别指定四个文件（更精确）
python -m src.cli --orders 订单.xlsx --ads 广告.xlsx \
                  --commissions 联盟佣金.xlsx --mapping 找id用的.xlsx \
                  --costs "V1-净利  -15.xlsx" --country 马来西亚

# 按店铺清单跑（店铺配置见 config/stores.json）
python -m src.cli --workbook "V1-净利  -15.xlsx" --store demo-my
```

### 解析层是按表头名识别，不认列号

平台导出模板会变、人工会在前面插列，写死列号必然错位。所以：

- 表头在哪一行自动找（前 8 行里挑最像表头的）
- 中文/英文表头都认（`Sku Subtotal Before Discount`、`商品 ID`、`Est. commission`…）
- 第 2 行的英文说明行自动跳过
- `RM2,678.75` 这类带货币符号的值能正确转成数字
- 广告按「货币」列自动换算（USD → 本地），**不会再乘两次汇率**
- `Order Amount` 是订单级金额，按订单内行数平分（与旧表口径一致）

> **GMV 口径提示**：报表沿用旧表的「折扣前小计 − 卖家折扣」，而平台后台统计的 GMV 是买家实付口径。
> 同一期间两者会差 1.8 倍左右（例：2026-10-01~10-15，本引擎 13,074.78，平台统计 14,770.66，买家实付 15,219.88）。
> 这个口径待定，切换只需要改 `calc.py` 里 `OrderLine.gmv` 一处。

### 方式二：双击 `scripts\run.bat`

默认算「本月 1 号 ~ 今天」。也支持参数：

```
run.bat --mode yesterday     昨天
run.bat --mode last-month    上月
run.bat --mode yesterday --store demo-my
```

### 方式三：定时全自动

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
```

注册每天 09:00 跑「昨天」，关机错过会开机补跑。卸载：
`Unregister-ScheduledTask -TaskName "算净利润-每日" -Confirm:$false`

### 双击没反应 / 报「找不到 python」

Windows 自带的 `WindowsApps\python.exe` 是个**商店占位程序**，双击时什么都不做。
所有批处理都通过 `scripts\_python.bat` 自动找一个真正能用的解释器，搜索顺序：

1. 环境变量 `PYTHON_EXE`
2. PATH 里的 `python`（跳过 `WindowsApps` 占位）
3. `py` 启动器
4. `%LOCALAPPDATA%\Programs\Python\Python3xx`、`C:\Python3xx`、`%ProgramFiles%\Python3xx`
5. WorkBuddy 自带的 Python（本机当前走这条）

都没有才会报错。要固定用某个解释器，设一个环境变量即可：

```powershell
setx PYTHON_EXE "C:\Path\To\python.exe"
```

> 批处理内容全部为纯 ASCII，避免中文在 cmd 里因编码不同而乱码。

---

## 二、输出长什么样

每次运行生成一份 Excel，六页：

| 工作表 | 内容 |
| --- | --- |
| **汇总** | 每个商品一行：GMV、订单数、客单价、成本、毛利、九项费用、费用合计、净利率、净利（本地/人民币）。带筛选、冻结窗格、净利率色阶 |
| **费用拆解** | 长表形态（商品 × 费用项 × 金额 × 占GMV），可直接丢进透视表 |
| **日趋势** | 按日期汇总 GMV / 订单数 / 净利，看波动 |
| **异常** | 缺成本、缺商品映射、未付款、取消退货，逐条给「建议动作」 |
| **明细** | 订单行级原始数据，任何一个数字都能追到行 |
| **口径** | 本次运行的参数快照（汇率、费率、开关、区间、行数），保证结果可追溯 |

---

## 三、核算口径

```
GMV       = Σ(折扣前小计) − Σ(卖家折扣)        排除取消/退货行
成本       = 成本表单价 × 数量 × 人民币→本地汇率
毛利       = (GMV − 成本) / GMV
发单费     = 订单数 × 发单费(本地)              按单收，不按金额
平台佣金   = GMV × 佣金率
交易手续费 = 买家实付 × 费率
奖金返现 / 超级优惠券 / 商家成长 / 物流 = GMV × 各自费率
达人佣金   = 联盟报表按商品ID汇总（预估佣金）
广告花费   = 广告报表按商品ID汇总 × 美金→本地汇率
净利率     = 毛利 − 各项费用 / GMV
净利(本地) = GMV × 净利率      净利(人民币) = 净利(本地) ÷ 人民币汇率
```

### 几个关键约定

- **订单数与买家实付按订单内行数平分**。一个订单的分配数合计为 1，这是原表的口径，
  引擎已 100% 复刻。想改成「按行 GMV 占比」，把 `config/rates.json` 里
  `allocation_paid` 改成 `proportional`（更准，但与旧表会有细微差异）。
- **成本三级解析**：成本表精确命中 → 多件装（`-2Pcs` 按基座价×件数）→ 组合装（`+` 拆开相加）。
  都查不到就记进异常页，按 0 计，**绝不猜**。
- **商品ID 三级定位**：平台 SKU ID（唯一）→ 卖家 SKU → 多件装/组合装拆解后的基座 SKU。
- **超级优惠券默认按 0 计**（沿用原表 `×0` 的开关），在 `rates.json` 里可打开。

---

## 四、口径校验

`python -m scripts.validate` 会把引擎结果与主工作簿「净利润」表逐项比对：

| 校验项 | 结果 |
| --- | --- |
| 商品ID 映射 | 1884 / 1887 = **99.84%** |
| 订单数分摊 | **100%**（0 偏差） |
| 买家实付分摊 | **100%**（0 偏差） |
| 成本 | 1893 / 1896 一致 |
| 合计 GMV | 17352.29 对 17352.29（**一致**） |
| 合计净利 | 267.08 对 302.22，差 −35.14 |

那 −35.14 的来源已定位：`MY-J12-5PCS` 三行，原表成本填的是 0，引擎按「5 × 3.84」推出 19.2，
换算后正好 35.14。**是原表漏填，不是算错。**

---

## 五、取数自动化（进行中）

数据都在你已登录的**战斧浏览器**里。已验证：战斧的 API 与内核直连两条路都走不通
（本地 API 业务端未公开；内核启动需客户端生成的加密参数）。所以走浏览器扩展这条路。

### 已具备：抓包扩展 + 本地桥

```
extension/   浏览器扩展（MV3）：劫持页面 fetch / XHR，把接口调用记录下来
bridge/      本地桥服务    ：接收扩展推送，落盘成可读的接口清单
```

跑起来：

```bash
# 1. 起本地桥
python -m bridge.server        # http://127.0.0.1:8765

# 2. 战斧里加载扩展
#    扩展管理 → 加载已解压的扩展 → 选 extension/ 目录

# 3. 打开店铺后台，正常点一次「导出」
#    抓到的接口写到 data/capture/capture-YYYYMMDD.jsonl
#    去重清单写到 data/capture/endpoints.json
```

`endpoints.json` 就是后续自动取数的目标清单——**先看清真实接口，再写自动化，不靠猜页面结构。**

### 待做

- [ ] 按 `endpoints.json` 里的真实接口，实现四个数据源的自动拉取（订单 / 商品 / 广告 / 联盟）
- [ ] 下载文件自动归档到 `data/exports/<店铺>/<日期>/`，由 `--dir` 直接消费
- [ ] 多店铺批量跑（店铺清单已支持，取数端要跟上）
- [ ] 网页看板（本期不做）

---

## 六、目录结构

```
.
├── src/
│   ├── loader.py     原始数据读取、SKU→商品映射、成本解析
│   ├── calc.py       净利核算（口径在这一层，改口径只改这里）
│   ├── report.py     Excel 报表输出（六页模板）
│   └── cli.py        命令行入口
├── scripts/
│   ├── validate.py            口径校验（对你的旧表）
│   ├── run_default.py         一键运行的默认参数
│   ├── run.bat                双击入口
│   └── install_scheduled_task.ps1   注册定时任务
├── extension/        战斧浏览器扩展（抓包）
├── bridge/           本地桥服务
├── config/
│   ├── rates.json    国家汇率与费率（改参数不用改代码）
│   └── stores.json   店铺清单（加店铺不用改代码）
├── data/capture/     抓包原始记录与接口清单
├── output/           生成的报表（不入库）
└── docs/GIT-WORKFLOW.md
```

---

## 七、版本管理

- 远程仓库：<https://github.com/xm876229-cmd/suan-jing-li-run>（私有）
- 提交规范、分支策略、发版流程见 [`docs/GIT-WORKFLOW.md`](docs/GIT-WORKFLOW.md)
- 变更记录见 [`CHANGELOG.md`](CHANGELOG.md)
