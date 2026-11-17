# AGENTS.md — 算净利润（JingLiHelper）

给接手的 AI / 工程师看的**工程约定**。
业务背景看 `README.md`，变更历史看 `CHANGELOG.md`，**当前进度与待办看 `docs/交接-AI新对话.md`**。

> 当前 **0.24.156**（2026-11-16 发布，Windows + macOS 三个包都已上 GitHub Release）。
> 八家店已跑通并逐项核对：费率 8 项、广告、联盟、款式编码全部对得上。
> 待办第一项：SKU 分段取数慢（每店多 1~2 分钟），用户说先不改。
> **动手之前先看第 1.0 条。**

---

## 0. 一句话说清这是什么

TikTok Shop 跨境电商的**多店铺净利核算工具**：
浏览器扩展自动取数 → 本地桥（HTTP 8765）落盘 → Python 核算 → 出六页 Excel 报表。

Windows 打包成 `jingli.exe`（安装版 / 免安装版），macOS 走 GitHub Actions 编 arm64。
代码全部 Python 标准库 + openpyxl + pywebview，打包只用 PyInstaller。

---

## 1. 绝对硬规则（违反会直接出事）

### 1.0 先给方案，等确认再动手（最高优先级，读完再看别的）

用户明确要求：「我说完要求或者任务后不要直接动手，先把你的理解和方案给出来，
我说了动手再动手，以后都是」。

所以任何改动之前：

1. 先说清**我的理解**（这问题是什么、为什么会这样）
2. 再说**打算怎么做**（改哪几个文件、会影响什么、怎么验证）
3. **等用户说「动手 / 你做吧 / 按你说的来」**，再改代码

不要看到问题就直接改 —— 哪怕问题看起来很明显、改起来只有一行。
不要「顺手把别的问题也修了」—— 那是另一件事，先问。

改完之后**必须自检**，并**把结果说出来**（跑了哪几项、什么结果），
不能只说「完成了」。自检覆盖：语法/加载、真跑、结构、数据、边界、
三份配置与三处版本号一致性。见第 7 节的自检清单。

### 1.1 改一处 = 同步所有版本

同一份代码存在四份，**必须保持一致**：

| # | 位置 | 说明 |
|---|---|---|
| 1 | `桌面\算净利润\`（项目目录） | 源头，改这里 |
| 2 | `release\净利助手\` | 免安装版；**浏览器实际加载的扩展多半是这里的** `浏览器扩展\` |
| 3 | `%LOCALAPPDATA%\JingLiHelper\` | 安装版 |
| 4 | `release\净利助手-安装包.exe` / `-免安装.zip` | 分发包 |

```bash
python -m scripts.sync_all            # 扩展 + 默认配置 + 默认成本表 + 文档（只覆盖不删）
python -m scripts.sync_all --check    # 只看差异
python -m scripts.sync_all --build    # 先重打包 exe/安装包再同步
```

**改了 Python 必须 `--build`**。打包版代码冻在 `jingli.exe` 里，只拷扩展没用。

### 1.2 配置正本是 `release\净利助手\配置\`

不是项目根的 `配置\`。0.24.44 起数据跟着程序目录走。
改项目根那份 = 没改，`sync_all` 还会说"已一致"。

### 1.3 三处版本号必须同时改

```
浏览器扩展/manifest.json          "version": "0.24.xxx"
CHANGELOG.md                      首个 "## [v0.24.xxx]"
build/do_release.py               TAG = "v0.24.xxx"
```

### 1.4 发布前必须校验，绝不上传旧包

```bash
python -m scripts.verify_release          # 本地七项
python -m scripts.verify_release --remote # 加远程哈希核对
```

`build/do_release.py` 已内置同样校验，不过就 `sys.exit(1)`。

**打包前必须关掉 release 目录里跑着的 jingli.exe**，否则 exe 被锁、留下半套目录。
`build_app.py` 已加前置探测，会直接拒绝。

### 1.5 不允许「隐藏开关」

任何能让一笔费用**静默不生效**的开关，必须在界面上看得见。
反例（0.24.155 已删除）：`switches.super_coupon_enabled` 出厂 false、界面无入口、
还在代码里覆盖了「计算项」的启用勾，导致马来四家每期少扣 1.08% GMV。

现在的规则：**参数表那一行的「启用」勾 + 费率本身（填 0 = 不算）**，不要有第三个开关。

### 1.6 别把「跟模板算得一模一样」当目标

模板 `V1-净利  -15.xlsx` 里的 `*0` 可能是「暂时没填」，不是业务规则。
抄模板前问一句：这个 0 是真的不算，还是没填？

---

## 2. 常用命令

```bash
# 起桥（开发时）
python -m bridge.server                    # 前台，Ctrl+C 停

# 起桥（要持久、不被工具会话杀掉）
cd release/净利助手 && explorer.exe "start-bridge.vbs"
# ↑ 父进程是 explorer 才持久。cmd start / Start-Process 启动的会随会话结束被杀

# 健康检查
curl -s http://127.0.0.1:8765/health
curl -s "http://127.0.0.1:8765/log?limit=5000"     # 日志（上限 5000 行）

# 核算一家店
JINGLI_HOME="C:/Users/Administrator/Desktop/算净利润/release/净利助手" \
  python -m src.cli --store <shop_id> \
  --dir "release/净利助手/数据/店铺/<店名>/收件箱" \
  --from 2026-11-01 --to 2026-11-15 \
  --out ".tmp_check.xlsx"

# 打包版核算（口径与源码版必须一致）
cd release/净利助手 && ./jingli.exe run --store <shop_id> \
  --dir ".../数据/店铺/<店名>/收件箱" --from 2026-11-01 --to 2026-11-15 --out "..."
# 打包版输出是 GBK，管道要看内容时用 | iconv -f GBK -t UTF-8

# 自检（改完代码必跑）
python -m scripts.check_ext                # 扩展静态检查（27 项）
python -m scripts.check_style_scope        # 款式编码口径（5 项）
python -m scripts.check_switch_flags       # 参数启用状态（3 项）
python -m scripts.check_ui_dom             # 界面 DOM 结构
python -m scripts.check_ui_runtime         # 界面脚本真跑（Node + 最小 DOM）
python -m scripts.check_download_guard     # 下载拦截器四种触发
python -m scripts.selftest_calc            # 核算勾选/费率影响

# 发布
python -m scripts.sync_all --build
python -m scripts.verify_release
python build/do_release.py                 # 上传 GitHub Release（先本地校验）
```

---

## 3. 目录与关键文件

```
算净利润/
├── jingli.py                 打包入口（PyInstaller 入口必须在项目根，否则 import src 失败）
├── src/
│   ├── app.py                程序入口：菜单 / serve / run / install / doctor / selfcheck-ui
│   ├── cli.py                命令行核算入口，串起 loader → calc → report
│   ├── paths.py              路径解析（源码模式 / 打包模式统一）；数据目录中文名与迁移
│   ├── loader.py             读订单/广告/联盟/映射表；SKU→商品映射；成本三级解析；款式编码
│   ├── calc.py               净利核算（口径在这一层）；三份参数表（主/含样品/不含样品）
│   ├── formula.py            公式引擎（支持中文变量名）+ load_config 白名单 + country_config
│   ├── report.py             六页 Excel；汇总列目录 _summary_specs / merge_report_columns
│   ├── fx.py                 汇率（T-1，frankfurter 为主，逐层退）
│   ├── safeio.py             所有配置/状态/成本表写入必须走这里（tmp→fsync→replace + .bak）
│   ├── selfcheck.py          启动自检与修复
│   ├── snapshots.py          取数快照（同盘用硬链接）
│   └── setup_core.py         安装/卸载/自启/快捷方式
├── bridge/
│   ├── server.py             本地桥（ThreadingHTTPServer，8765）；所有 POST 走 _read_payload
│   ├── ui.py                 整个前端（HTML+CSS+JS 都在这一个文件里，内嵌字符串）
│   └── config_page.py        配置读写（/api/config 下发 report_column_catalog）
├── 浏览器扩展/
│   ├── background.js         取数主力（约 7400 行）：订单/广告/联盟/SKU 四类 + 下载拦截
│   ├── inject.js             页面上下文注入（fetch 超时、参数学习）
│   └── manifest.json         版本号在这里，改完必须升版本
├── 配置/                      ← 注意：正本是 release\净利助手\配置\
│   ├── formulas.json         参数表（每行一条公式）+ report_columns + report_columns_seen
│   ├── rates.json            各国费率与汇率 fixed 值 + switches
│   ├── sites.json            域名 → 站点能力（ads_spec / download_retry）
│   ├── api_specs.json        规格接口步骤定义（订单/联盟/SKU/广告）
│   └── stores.json           店铺清单
├── scripts/                  构建 / 自检 / 导入 / 迁移工具（见第 7 节）
├── packaging/
│   ├── mac.spec              PyInstaller mac 配置（datas 的 dest 不能写 "."！）
│   └── build_mac.sh
├── .github/workflows/build-mac.yml   Mac 云构建（含 5 项资源断言 + 真跑接口）
├── build/                    ← 本地发布工具，**被 .gitignore 忽略，不在仓库里**
│   ├── build_app.py（实为 scripts/build_app.py）
│   └── do_release.py         上传 GitHub Release
├── release/                   打包产物（不入库）
│   ├── 净利助手/              免安装版（含 数据\ 报表\ 配置\）
│   ├── 净利助手-安装包.exe
│   └── 净利助手-免安装.zip
└── docs/
    ├── GIT-WORKFLOW.md
    ├── Mac版-安装与使用.md
    └── 交接-AI新对话.md       ← 当前进度快照
```

---

## 4. 架构：三层

```
① 浏览器扩展（background.js，跑在已登录的战斧浏览器里）
   学到页面的 Cookie / 风控参数（fp、X-Tts-Oec-Bsid、aadvid）→ 用这些参数调平台接口
   ↓ 下载到文件后 POST /upload-raw 回传
② 本地桥（bridge/server.py，127.0.0.1:8765）
   收文件、认类型（靠表头不靠文件名）、落到 数据\店铺\<店>\收件箱\<类型>\
   提供界面（/）、实时状态（/api/live）、配置（/api/config）、日志（/log）
   ↓ 派单 POST /run 或 /api/run-stores
③ 核算（src/cli.py 子进程）
   loader 读四类表 → calc 算 → report 出 Excel 到 报表\自动\<店>\
```

**四类数据 + 一条兜底链**（每类都是「接口 → 页面导出 → 已完成任务」三段）：

| 数据 | 主接口 | 落盘位置 |
|---|---|---|
| 订单 | `POST /api/fulfillment/order/export` → `export_record/get` 轮询 → `order/download` | `收件箱/订单表/` |
| 广告 | `POST /oec_ads/shopping/v1/oec/stat/post_product_list_v2`（`action:download`）→ `download_task/query` | `收件箱/广告表/` |
| 联盟佣金 | `POST /api/v1/oec/affiliate/compass/transaction/detail_list/get` | `收件箱/联盟佣金表/` |
| 商品 SKU | 分页 `sku/list`（JSON）+ 页面导出 xlsx（链接归属权威） | `链接映射/` |

---

## 5. 数据流与目录约定

数据根 `数据\`（**子目录全中文**）：

```
数据\
├── 抓包记录\          capture-YYYYMMDD.jsonl、task.log（日志正文，8MB 轮转）
├── 导出\
├── 成本\              master.csv（9126 行）+ history.jsonl
├── 链接映射\          master.csv（累积表，只增不删）+ sku_list_*.json + sku_export_*.xlsx
├── 日志\
├── 回收站\
├── 汇率存档\          YYYY-MM-DD.json（同一天重算必须用同一组）
└── 店铺\<店名>\
    ├── meta.json      店铺身份（shop_id 为准；目录名只是显示）
    ├── profile.json   adv_id 等学到的参数
    ├── 收件箱\{订单表,广告表,联盟佣金表,商品映射}\
    ├── 商品SKU表\
    ├── 链接资料\
    └── 快照\<时间戳>\  取数快照 + manifest.json
```

报表在 `报表\自动\<店>\` 和 `报表\手动\<店>\`，**不在 `报表\*.xlsx`**。

**店铺身份只认 `meta.json` 的 `shop_id`**，目录名可以改。
核算锁按 shop_id 分开，多店可并行。

---

## 6. 核算口径（业务核心，改之前先想清楚）

### 6.1 净利公式

```
净利 = GMV − 商品成本 − 发单费 − 平台佣金 − 交易手续费 − 奖金返现
       − 广告花费 − 联盟佣金 − 订单服务费 − 超级优惠券 − 商家成长 − 物流费
```

全部由 `配置/formulas.json` 的 `params[]` 驱动，每行一条 `expr`，支持中文变量名。

### 6.2 十项费用（当前配置）

| 费用 | 公式核心 | 马来西亚 | 菲律宾 |
|---|---|---|---|
| 发单费 | `orders × dispatch_fee_cny × cny_to_local` | 3 CNY × 0.61264 = 1.8379/单 | 2 CNY × 9.5644 = 19.1288/单 |
| 订单服务费 | `orders × order_service` | 0.54/单 | 5/单 |
| 平台佣金 | `gmv × platform_commission` | 8.1% | 6.2% |
| 交易手续费 | `paid × tx_fee` | 3.78% | 2.24% |
| 奖金返现 | `gmv × cashback` | 4.86% | 0 |
| 超级优惠券 | `gmv × super_coupon` | **1.08%** | 0 |
| 商家成长 | `gmv × growth_fee` | 0 | 1.0% |
| 物流费 | `gmv × logistics` | 0 | 5.5% |
| 广告花费 | 广告表该链接成本 × 币种汇率 | 来自数据源 | 同 |
| 联盟佣金 | 联盟表该链接 Est. commission | 来自数据源 | 同 |

**kinds（算哪些订单类别）决定反算基数**，不是全部按 GMV：

- `tx_fee` 的 kinds 只有 `normal` → 反算基数 < 明细买家实付合计，**这是对的**
- `order_service` 的 kinds 是 `normal + refunded` → 反算基数 < 订单数，**这也是对的**

### 6.3 款式编码：只认本期出单（0.24.153，用户明确要的）

- **明细页**：每行只写**这一行自己**的款式（映射优先 → 本行 Seller SKU 推）
- **汇总页**：这条链接**本期出过单**的款式，去重 `/` 连接
- 字段分工：`OrderLine.style_codes` = 本行；`OrderLine.link_styles` = 链接级并集
- **不要从 `SkuIndex.styles_of()` 捞整条链接**：映射主表是累积表、只增不删，
  下架 SKU 会永远留着（`M14-1pc` 11-05 下架，报表挂了一个月）
- 这与模板 `TEXTJOIN` 有意不同，`scripts/verify_styles.py` 从此不再是"必须一致"

款式编码来源：平台没有这一列，从卖家 SKU 推（`V27-White-10Pcs → V27`、`M13-9-Green → M13`）。

### 6.4 汇率口径：T-1

核算用**操作日前一天**的汇率（2 号核算用 1 号）。
`src/fx.py::rate_day()`；frankfurter 为主（欧洲央行参考价，免 key）。
每天存 `数据/汇率存档/YYYY-MM-DD.json`，同一天重算必须用同一组数，否则净利会飘。
退回链：T-1 → 存档 → 最新价 → `rates.json` 固定值，口径页写明退到哪层。

### 6.5 报表列

- 列目录由**后端** `/api/config` 的 `report_column_catalog` 下发（43 项），前端只用不推
- `report_columns` = 用户勾选结果；`report_columns_seen` = 历来见过的列
- 新增列 = `default_keys − seen`，自动勾上；用户取消过的不强拉回来
- **`formula.load_config` 白名单必须带上这两个键**，漏了 → 取消的列全被当"新增"加回来
- `country_config()` 也要透传，否则按国家核算又丢一次

---

## 7. 自检清单

改完代码**必须**跑，且要能说出"哪一项在守什么"。

**分两组**：第一组每次改完就跑（几秒到十几秒）；第二组发布前才跑（会真跑报表和桥，2~3 分钟）。
Codex 会自动执行这里列的命令，所以别把慢的放进第一组。

### 7.1 每次改完必跑

| 脚本 | 守什么 | 耗时 |
|---|---|---|
| `check_ext.py` | 扩展静态：符号定义/引用、下载拦截、adv_id 来源、联盟表命名、注入超时（27 项） | ~2s |
| `check_style_scope.py` | 明细只写自己 / 下架款式不进汇总 / 推不出不崩 / 单款不虚增（5 项） | ~3s |
| `check_switch_flags.py` | 不许代码强制关闭参数 / 三份参数表状态一致 / 超级优惠券真参与（3 项） | ~2s |
| `check_ui_dom.py` | 界面真实 DOM 结构（id 不重复、容器都在、选择器对得上） | ~2s |
| `check_card_seed.py` | 驾驶舱卡片「补文件名」按钮的常驻自检（基线快照比对） | ~2s |
| `selftest_calc.py` | 勾选/费率/新增/删除是否真的影响结果（10 项） | ~10s |

### 7.2 发布前才跑（慢，且需要 Node / 真跑报表）

| 脚本 | 守什么 | 依赖 |
|---|---|---|
| `verify_release.py` | 发布七项：版本/新鲜度/包内容/随包配置/公式体检/界面/真跑 | 2~3 分钟 |
| `check_ui_runtime.py` | 界面脚本在 Node + 最小 DOM 桩里真跑（含列目录用后端下发） | 需 Node |
| `check_download_guard.py` | 四种下载触发（click / dispatchEvent / form.submit / iframe）都要拦住（4 项） | 需 Node |
| `check_formula.py` | 带公式报表体检：真跑一份，查自引用 / 合计行常数 / 取消各列 | 需真报表 |

**自检必须验证"会红"**：把 bug 临时加回去，确认它报错。
否则你不知道它是真在盯，还是永远绿的摆设。

`sync_all --build` 打包前会自动跑 `check_ext` + `check_style_scope` + `check_switch_flags`。

---

## 8. 已知坑 TOP 15（都是真踩过的）

1. **`ls | tail -6` 会漏文件** —— 目录按名字排序，`20261101_20261115` 排在中间。
   核对文件用 `ls | grep 关键字`，或用 mtime 排序。
2. **核对脚本选文件不能"优先不带序号"** —— 每轮新增 `(N)`，不带序号那份往往是几小时前的。
   一律**按 mtime 取最新**。
3. **报表有两列净利**（`净利（MYR）` / `净利（CNY）`）。
   用 `startswith('净利（')` 会拿到最后一列 CNY。要写死或精确匹配。
4. **`cmd start` / `Start-Process` 启动的桥会随会话结束被杀**，用 `explorer.exe "start-bridge.vbs"`。
5. **`ThreadingHTTPServer.request_queue_size` 默认 5** —— 8 家心跳会瞬时超限。
   已改 128。
6. **静默失效的根源是「按目录名判断」** —— 数据目录 0.24.4 起改中文，
   所有按英文名判断的地方都要中英都认（`_skip_in_pack`、`setup_core`、`sync_all`、`build_app`）。
7. **`数据目录.txt` 绝不能进发布包** —— 内容是相对路径 `..\..`，用户解压后数据根会跑到上级。
   `verify_release` 已加硬检查。
8. **`.gitignore` 必须忽略整个 `数据/*`** —— 否则几十 MB 订单表进仓库。
9. **加报表列必须改 `_db = len(DETAIL_HEADERS) + 费用列数 + 1`** ——
   SUMIFS 的列字母全靠它，漏改不报错但全错。
10. **合计行在第 1 行时，数据行不能按 `ri==0` 判合计** —— 会把首条链接写成假合计行。
11. **大店订单（22.7 MB）平台生成要 5 分钟** —— 只等 44 秒会反复重排，越来越慢。
    正解：建一个整段任务 → 等 48 秒 → 没出来进 `pendingOrders`，`pending-watch` 每分钟收。
12. **`order/download` 的 `file_key` ≠ `export_task_id`** ——
    `file_key` 形如 `b595bc8c907f427bsafe`（16 位十六进制+safe），纯数字是任务号。
    拿任务号当 file_key 永远回 `21016003`。
13. **广告接口认 `aadvid`（广告账户 ID）** —— 缺了回 `req adv id missing`，
    然后一路退到「页面点导出」并弹「另存为」。
    这个 ID 在**广告页发出的 `oec_ads` 请求查询串**上，不在地址栏。
14. **`set` 遍历顺序随 hash 种子变** —— `for pid in set(...)` 入 dict 会导致
    报表尾部行顺序每次不同（那些链接 GMV 都是 0，`sorted(key=gmv)` 对同 key 稳定排序）。
15. **Mac 打包 `datas` 的 dest 不能写 `"."`** —— 会把目录**内容**摊平到 `Frameworks` 根下，
    程序按 `resource_root()/浏览器扩展` 找不到。要写 `"浏览器扩展"`。

（更多历史坑见 `.workbuddy/memory/MEMORY.md` 与 `CHANGELOG.md`）

---

## 9. 代码约定

- **所有 POST 必须走 `Handler._read_payload()`**，不要手写 `read(Content-Length)`：
  负数/超大/非 dict 会让线程卡住或断连接
- **`do_GET` / `do_POST` 各有外层兜底**（`_do_get` / `_do_post`），新增分支不必自己 try
- **所有配置/状态/成本表写入走 `src/safeio.py`**（`write_json/write_text/write_bytes`），
  它做 tmp → fsync → `os.replace` + 自动留 `.bak`。不要再写 `path.write_text(...)`
- **用户可反复点的接口要幂等**（`_enqueue` 同店同 task 新顶旧）
- **任何"按目录名判断"必须中英都认**
- **报表/快照/安装包一律「先写临时名再改名」**
- **加配置键必须同时改三处**：`formula.load_config`（白名单）、`formula.country_config`（透传）、
  `bridge/config_page`（界面读写）
- **`CHANGELOG.md` 用 Python 写**，别用 heredoc / regex（中文转义容易出错）
- 批处理 `.bat` 内容全 ASCII，避免 cmd 编码问题

---

## 10. 发布流程

```bash
# 1. 确认没有任务在跑，关掉 release 里的 jingli.exe
Get-Process jingli -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. 改版本号（三处）+ 写 CHANGELOG
# 3. 打包并同步
python -m scripts.sync_all --build

# 4. 本地校验（七项）
python -m scripts.verify_release

# 5. 提交推送
git add -A && git commit -F .gitmsg.txt && git push origin main

# 6. 发布（内置校验，不过就拒绝上传）
python build/do_release.py

# 7. 传完再独立复核一次
python -m scripts.verify_release --remote

# 8. Mac 版：触发 GitHub Actions
#    POST /repos/{owner}/{repo}/actions/workflows/build-mac.yml/dispatches
#    {"ref":"main","inputs":{"release_tag":"v0.24.xxx"}}
```

**发布说明要手动补全**：`do_release.py` 自动生成的很单薄（取 CHANGELOG 首条第一句），
用 `PATCH /repos/{owner}/{repo}/releases/{id}` 重写。

**Mac 包验证要点**：
- 资源真实位置在 `Contents/Resources/浏览器扩展/`，
  `Contents/Frameworks/浏览器扩展` 只是**符号链接**（zip 里看不到其下文件）
- zip 里中文名是 cp437 编码，Python 读要 `n.encode("cp437").decode("utf-8")`
- 比对 `background.js` 的 MD5 与 Windows 版是否一致（这是最硬的"没传错版本"证据）

---

## 11. 不要做的事

- 不要 `git add -f` 把 `build/` 塞进仓库（本地发布工具，不进公开仓库）
- 不要删除 `.workbuddy/`（项目记忆，已被 git 忽略，但要保留在本地）
- 不要在 `数据/` 里做递归删除；清数据要连 `快照/` 一起清，否则核算时会自动补回
- 不要把「自检脚本」当成"随时可以删的开发垃圾"——它们是防复发的唯一手段
- 不要只跑 `sync_all` 就以为同步完了（Python 改动必须 `--build`）
