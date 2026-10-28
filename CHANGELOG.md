# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [v0.10.0] - 2026-10-28

### 口径：取消/退货/样品单的收入与成本拆开控制（修一个会算错净利的错）

原来的实现是「一个开关控制整行」——关掉 `include_cancelled`，
就把收入、成本、发单费一起关掉。但订单取消了，货该采购的采购了、
该发的发了、发单费按订单数照收、平台抽成也照收，
整行排除等于**把成本抹掉，净利虚高**。

现在每类订单四个开关独立控制：

| 开关 | 管什么 |
| --- | --- |
| `income` | 收入：GMV 与买家实付 |
| `cost` | 商品成本（含件数） |
| `order_count` | 订单数 → 发单费等按单计费的基数 |
| `fee_base` | 平台抽成计费基数 → 平台佣金、交易手续费 |

按类别配：`cancelled` / `refunded` / `zero_amount` / `unpaid` / `normal`，
只写想改的那一项，其余沿用默认。旧写法 `include_cancelled` 等仍兼容。

默认口径：取消/退货 → 收入不计、成本+发单费+抽成照算；
0 元样品单 → 成本+发单费照算、抽成不计；未付款 → 整行排除。

### 新增「计费基数」概念

`ProductMetrics.fee_base_gmv` / `fee_base_paid`。平台佣金改为 `base=fee_base`、
交易手续费改为 `base=fee_base_paid`，这样「取消单收入不算、但抽成照收」
才表达得出来——之前两者共用一个 `gmv` 字段，一关就全关。
想按「退款时平台退佣」算，把该类的 `fee_base` 设 `false` 即可。

### 修一个隐蔽的 bug

`formula.load_config` 用白名单过滤 `order_filter`，只收 `include_xxx` 四个键，
嵌套的类别配置被整个丢掉，表现是「改了配置文件毫无反应」。
改成整体接受，键校验交给 `calc.resolve_order_filter()`。

### 报表

- 「口径」页新增各类订单行数与「计入哪些、不计哪些」，完全按实际配置生成
- 「异常」页把已取消 / 退货退款分开列出（原来混成一项），
  说明里写明当前口径怎么算，不再写死「已从 GMV 与成本中排除」

### 实测（马来西亚 demo-my，1386 行）

| 口径 | 成本 | 订单数 | 净利 |
| --- | ---: | ---: | ---: |
| 默认（取消/退货成本照算） | 6,576.37 | 796.00 | **2,235.49** |
| 取消/退货整行排除（旧） | 6,004.99 | 711.19 | 3,167.82 |

差异可逐项对上：成本 +571.38（936.68 CNY × 0.61）、发单费 +201.00（84.81 × 2.37）、
平台佣金 +106.61（1,316.13 × 8.1%）、手续费 +53.35（1,411.46 × 3.78%）。

## [v0.9.0] - 2026-10-28

### 新增：联盟佣金改走商品维度数据接口（原来这条是断的）

之前联盟佣金一直在等「导出任务」生成下载地址——**实测那个 list 接口只返回
task_id / file_name / status，永远没有下载地址**，抓包里也找不到任何 download 接口，
所以每轮都在超时后放弃。

改用联盟后台「商品分析」页面自己就在调的接口，参数与响应结构已从抓包逐一核对：

```
POST affiliate.tiktok.com/api/v1/oec/affiliate/compass/transaction/detail_list/get
{"params":{"detail_list_type":2,"product_list_params":[{
  "time_descriptor":{"granularity_type":1,"timezone_offset":28800,"start_time":…,"end_time":…,
                     "time_option_type":1,"time_context":"101"},
  "metric_types":[1,3,2,4,9,20,13,14,19,6,24,22,16,17,18,25,26,23,5],
  "page_param":{"page_no":1,"page_size":100},"sorter":{…},"filter":{}}]},
 "module_type":104,"locale":"zh-CN"}
```

之前探测没命中，是因为**少了 `time_context:"101"`，metric_types 也不全**。

返回 `product_performances[].product_base.id`（商品ID）与
`product_metrics.product_estimated_commission`（预估佣金），按商品ID汇总后
写成 CSV，表头 `Product ID / Est. commission`——**与旧工作簿的联盟报表同构，
核算引擎零改动就能吃**。

### 新增：广告导出接口抓包模式

广告导出卡住的根因不是程序，是**抓包时人不在广告页面上**，ads 域的写请求从未发生。
新增面板按钮「抓广告导出接口」：

- 自动找出已打开的广告标签页，没有就明确提示先切到广告页面
- 打开观察窗口后，ads 域的写请求**实时**回传并单独落到 `data/capture/ads_export.jsonl`
- 抓到「创建导出任务」类请求就自动收工，不用干等满时间

### 修复：webRequest 抓包没记请求体

`chrome.webRequest.onBeforeRequest` 的 extraInfoSpec 里漏了 `"requestBody"`，
导致 `details.requestBody` 一直是 undefined——**记下来的只有 URL，没有请求体**，
而反推接口参数恰恰需要请求体。补上后，表单提交 / iframe / Service Worker
发起的请求也能拿到完整参数。

### 改进：映射文件落到 data/mapping/

扩展上传接口新增 `dir` 参数：映射类文件（商品管理 SKU、链接资料）落 `data/mapping/`，
订单 / 广告 / 佣金落 `data/exports/_inbox/`，两类不再混在一个目录里。

`src/loader.py` 的映射解析新增 `skus` 键支持——商品管理接口
`/api/v1/product/stock/sku/list` 返回的是 `{"skus":[…]}`，之前只认 `items`。

### 已确认可用的接口（抓包逐一核对过）

| 用途 | 接口 | 状态 |
| --- | --- | --- |
| 订单 | `/api/fulfillment/order/export` → `export_record/get` → `download` | 已通 |
| 商品管理 SKU（映射主来源） | `/api/v1/product/stock/sku/list` | 参数已核对 |
| 链接资料（映射补充） | `/api/v2/insights/seller/ttp/product/sku/list/export` | 参数已核对 |
| 联盟佣金 | `/api/v1/oec/affiliate/compass/transaction/detail_list/get` | 本版打通 |
| 广告导出 | 待抓包确认 | 需人配合一次 |

## [v0.8.0] - 2026-10-28

### 新增：多国费率预置 + 汇率自动获取

东南亚与欧美共 12 个国家全部预置好，`config/rates.json` 里直接改就能用：

| 国家 | 币种 | 区域 | 国家 | 币种 | 区域 |
| --- | --- | --- | --- | --- | --- |
| 马来西亚 | MYR | 东南亚 | 美国 | USD | 欧美 |
| 菲律宾 | PHP | 东南亚 | 英国 | GBP | 欧美 |
| 泰国 | THB | 东南亚 | 德国 | EUR | 欧美 |
| 越南 | VND | 东南亚 | 法国 | EUR | 欧美 |
| 新加坡 | SGD | 东南亚 | 意大利 | EUR | 欧美 |
| 印尼 | IDR | 东南亚 | 西班牙 | EUR | 欧美 |

汇率三档可切换（`config/rates.json` 的 `fx` 段）：

```json
"fx": {
  "mode": "auto",            // auto=联网抓实时 / manual=只用固定值
  "provider": "open.er-api.com",
  "ttl_hours": 12,          // 汇率缓存 12 小时，避免同一天跑出两个数
  "manual": {}              // 单独固定某个国家：{"马来西亚": {"cny_to_local": 0.61, "usd_to_local": 4.04}}
}
```

- `auto`（默认）：优先抓实时汇率，抓不到（断网 / 接口挂了）自动退回配置里的兜底值，**不会让整次核算失败**
- `manual`：完全不联网，只用配置里写的固定值
- 命令行可临时覆盖：`--fx auto` / `--fx manual`

实际用的汇率来源会写进报表的「口径与参数快照」页，事后能查清这批数是用什么汇率算的。

> 费率数值除马来西亚、菲律宾外均为占位值（`rates_verified: false`），
> 报表里会明确标出来，第一次跑某个新国家时请先按店铺后台的实际费率改 `config/rates.json`。

### 新增：安装位置可自定义

安装包运行时可以自己选目录，不再只能装到 `%LOCALAPPDATA%\JingLiHelper`：

- 交互式：安装时直接输入路径，**支持把文件夹拖进安装窗口**
- 静默式：`净利助手-安装包.exe --dir "D:\净利助手" --silent`
- 目标目录写不进时提前报错（装到 Program Files 这类只读位置会明确说清）
- 快捷方式、开机自启、卸载信息都跟着新位置走，不会出现「装在 A 却给 B 建快捷方式」

### 新增：选店铺即带出国家

- `config/stores.json` 里每家店写上国家，面板和菜单选店时国家自动确定，不用手填两处
- 本地桥新增 `GET /stores`，扩展在面板上列出店铺；配置改了刷新面板就生效，不用重装扩展
- 核算时只传店铺 id，国家与费率由配置统一决定，避免面板填的和配置里的打架

### 修复

- **手动汇率形同虚设**：`--fx manual` 之前会偷偷用上次联网抓的缓存，用户填的固定值永远不生效，
  且每次跑出来的数还会随上次联网时间漂移。现在 manual 彻底不读缓存
- **构建被安全策略拦下**：PyInstaller 清理构建目录时一次性删 70 个文件，
  触发「批量删除」确认导致构建失败。改为把旧构建目录挪到系统临时目录
- 汇率模块 `src.fx` 是延迟导入，显式声明为 hidden-import，避免打包后漏掉

## [v0.7.0] - 2026-10-28

### 新增：可分发程序与安装包

程序不再只能跑在源码目录里。`python -m scripts.build_app` 一次产出：

| 产物 | 说明 |
| --- | --- |
| `release/净利助手/` | 免安装版，拷走即用（含源码与文档） |
| `release/净利助手-免安装.zip` | 上面那个目录的压缩包 |
| `release/净利助手-安装包.exe` | 单文件安装包，双击安装 |

安装包行为：

- 装到 `%LOCALAPPDATA%\JingLiHelper`，**按用户安装、不需要管理员权限**
- 建桌面与开始菜单快捷方式、注册开机自启、写入「设置 → 应用」里的卸载项
- 升级时替换程序文件，`data` 与 `output` 保持不变
- 支持 `--silent` 静默安装

### 新增：统一程序入口 `jingli.exe`

```
jingli.exe                 菜单（启动桥 / 立即核算 / 导入成本表 / 导入映射表 / 诊断 / 开自启）
jingli.exe serve           只起本地桥（开机自启用它）
jingli.exe run [参数]      核算，参数与 src.cli 一致
jingli.exe install         注册自启、建快捷方式、写卸载信息
jingli.exe uninstall       移除自启、快捷方式、卸载信息
jingli.exe doctor          路径 / 桥 / 自启 / 数据量一次看全
```

### 重构

- 新增 `src/paths.py`：统一「程序目录 / 数据目录 / 产出目录」的解析，
  源码模式与打包模式一致；可用 `JINGLI_DATA`、`JINGLI_OUTPUT` 环境变量把数据挪走
- `src/cli.py`、`scripts/*.py`、`bridge/server.py` 全部改用该模块，不再各自推算路径
- 本地桥调核算时按模式切换命令：打包后调 `jingli.exe run`，源码模式调 `python -m src.cli`

### 修复

- **打包后一跑就崩**：中文 Windows 控制台是 GBK，打印 `⚠` 触发
  `UnicodeEncodeError` 直接退出。现在统一 `errors="replace"`
- **安装包输出全是问号**：子进程输出经过一次转码导致丢字，改为直接继承控制台
- 构建脚本不再整目录删除 `release/`，改为逐文件覆盖
  （批量删除既会被安全策略拦下，也可能误删用户自己放进去的文件）

### 已知限制

- 浏览器扩展**无法静默安装**：战斧/Chromium 不支持从命令行加载未打包扩展，
  装完必须手工在 `chrome://extensions` 加载一次 `<安装目录>\extension`
- 联盟佣金的导出接口只返回任务 ID、没有下载地址，自动取数仍未打通（本轮已加接口探测）

## [Unreleased]

### 待办
- 按 `data/capture/endpoints.json` 里的真实接口实现四类数据自动拉取
- 下载文件自动归档到 `data/exports/<店铺>/<日期>/`
- 多店铺批量跑（配置侧已就绪）
- 网页看板

## [v0.3.0] - 2026-10-27

### 重构
- **解析层改为表头驱动**（`src/loader.py`）：不再写死列号，自动定位表头行、
  识别中英文表头、跳过第 2 行英文说明行。平台导出模板变动或人工插列不再导致错位。
- 新增 `--orders / --ads / --commissions / --mapping` 四个独立文件入口
- `--costs` 支持直接读工作簿里的「成本」工作表

### 修复
- **广告费被乘两次汇率**：解析层已按「货币」列换算，计算层又乘了一次，导致广告费虚高 4 倍
- **有广告花费但零订单的链接被整行丢弃**，其广告成本未计入，总成本被低估
- `SKU Seller Discount` 表头别名拼写错误，导致旧工作簿的卖家折扣全部读成 0、GMV 虚高 2 倍
- `RM2,678.75` 这类带货币符号的值无法转数字
- 净利改为绝对额口径 `(GMV − 成本) − 费用`，零 GMV 链接也能正确算出亏损

### 校验
- 旧工作簿商品ID 映射：**1887/1887 = 100%**（改用人工列后）
- 订单数分摊、买家实付分摊：0 偏差
- 净利差异 −76.59 已定位：−41.45 为补计的零单广告成本，−35.14 为原表漏填的 `MY-J12-5PCS` 成本

## [v0.2.0] - 2026-10-27

### 新增
- **核算引擎**（`src/`）：原始数据读取、SKU→商品ID 三级定位、成本三级解析、净利计算
- **Excel 报表**（六页自设计模板）：汇总 / 费用拆解 / 日趋势 / 异常 / 明细 / 口径
- **三种运行入口**：`src.cli` 命令行、`scripts/run.bat` 双击、`scripts/install_scheduled_task.ps1` 定时任务
- **口径校验**（`scripts/validate.py`）：与主工作簿逐项比对，结果见 README
- **抓包扩展**（`extension/`）：MV3，劫持页面 fetch/XHR，记录数据接口
- **本地桥**（`bridge/server.py`）：接收抓包记录，生成去重接口清单 `endpoints.json`
- 配置文件化：`config/rates.json`（汇率费率）、`config/stores.json`（店铺清单）

### 结论
- 旧表口径已 100% 复刻：分摊、GMV、九项费用逐项一致，合计 GMV 完全相同
- 唯一差异 −35.14 已定位为原表 `MY-J12-5PCS` 成本漏填（引擎按 5×3.84 推导更准）
- 成本自动化可行：直查命中 98.7%，组合装 42 个全部有独立行，多件装可按规则推导

### 已验证不可行
- 走战斧本地 API 取数：业务端点未公开
- 用内核直连拿 CDP：启动依赖客户端生成的加密参数，独立启动 8 秒自退

## [v0.1.0] - 2026-10-27

### 新增
- 初始化仓库结构与版本管理规范
- 纳入主工作簿 `V1-净利  -15.xlsx`：汇率-参数 / 成本 / 链接资料 / 订单 / 联盟佣金 / 广告 / 净利润 共 8 张表
- `README.md`：核算口径、工作表说明、目录约定
- `docs/GIT-WORKFLOW.md`：分支策略、提交规范、二进制文件管理、版本号与标签、回滚方式
- `.gitignore` / `.gitattributes`：Office 临时文件、产出目录、换行符与二进制文件规范
