# 算净利润 · 长期约定

## 硬规则：改一处 = 同步所有版本
用户明确要求（2026-10-30）：「以后有修改所有版本都同步一起改，不管是项目目录还是安装版还是 release」。

四份东西必须版本一致：
1. 项目目录 `桌面\算净利润\`（源头，开发时改这里）
2. `release\净利助手\`（免安装版；**浏览器实际加载的扩展多半是这里的** `浏览器扩展\`）
3. `%LOCALAPPDATA%\JingLiHelper\`（安装版，扩展目录名 `extension\`）
4. `release\净利助手-安装包.exe` / `净利助手-免安装.zip`（分发包）

同步命令：
- `python -m scripts.sync_all`       扩展 + `_internal\默认配置` + `_internal\默认数据\costs` + 文档（只覆盖不删除）
- `python -m scripts.sync_all --check` 只看差异
- `python -m scripts.sync_all --build` 先重打包 exe/安装包再同步（Python 侧改动的唯一真正同步方式）

注意：打包版代码冻在 `jingli.exe` 内，只拷扩展不够；改了 Python 必须 `--build`。
`bridge/server.py::_sync_extension_copies()` 在桥启动时也会兜底同步扩展。

## 发布（硬规则：上传前必须校验，防止传旧包）
- 发布前先：`python -m scripts.verify_release --remote`（版本源 / 构建新鲜度 / 包内容 / 真跑一遍 / 远程哈希）
- `build/do_release.py` 已内置同样校验：不过就 sys.exit(1)，**绝不上传**
- 打包前必须关掉 release 目录里跑着的 jingli.exe，否则 exe 被锁、会留下半套目录
  （build_app 已加前置探测，会直接拒绝并提示，不再复制到一半报 PermissionError）
- 顺序：改版本号 → `sync_all --build` → `verify_release` → `python build/do_release.py`
- 传完再跑一次 `verify_release --remote` 作为独立复核
- 三个版本号必须一致：`浏览器扩展/manifest.json`、`CHANGELOG.md` 首个 `## [vX]`、`do_release.py` 的 TAG

## 健壮性硬约定（0.24.6 体检后确立）
- **所有 POST 必须走 `Handler._read_payload()`**，不要再手写
  `read(Content-Length)`：负数/超大/非 dict 会让线程卡住或断连接
- `do_GET` / `do_POST` 各有外层兜底（`_do_get` / `_do_post`），
  新增分支不需要自己 try；异常日志带 `_where(e)` 行号
- 任何"按目录名判断"必须中英都认（用 `paths.store_inbox()` 这类封装）
- 用户可反复点的接口要幂等：`_enqueue` 同店同 task 新顶旧
- 配置写入走 `settings.save` 白名单，不要 `cur[k]=v` 硬塞
- 压测脚本模式：沙箱 `JINGLI_HOME=.tmp_fuzz` + 独立端口，
  30 端点 × 10 种畸形 body；改完必须再用打包版跑一遍

## 数据与目录（0.24.4 起子目录全中文）
- 数据根 `数据\`：`抓包记录 / 导出 / 成本 / 链接映射 / 日志 / 店铺 / 回收站`
- 每店 `数据\店铺\<店名>\收件箱\{订单表,广告表,联盟佣金表,商品映射}` + `商品SKU表`、`链接资料`、`快照`
- 店铺身份以 `meta.json` 的 `shop_id` 为准；目录用店名，新店首次识别自动改名
- 映射主表 `数据\链接映射\master.csv`（全局累积，跨进程锁 `.merge.lock`）
- 成本 `数据\成本\master.csv`；配置 `配置\{rates,formulas,stores,...}.json`
- 迁移靠 `paths.migrate_data_dirs(root)`：新名不存在就改名；两边都在就合并（`_merge_into`）。
  **打包、sync_all、启动三处都调**——只靠启动时改，release/安装版可能永远不启动就一直英文
- 手动入口 `python -m scripts.migrate_dirs [--check]`（`--check` 只看不改）
- 坑：任何「按目录名判断」的逻辑在改中文后都会静默失效。已踩过：
  `_skip_in_pack`（差点把 45 个店铺文件打进包）、`setup_core` 种子成本表、
  `sync_all.COST_SRC`、`build_app` 的 `数据/costs`。新增目录名判断必须中英都写
- `数据目录.txt` **绝不能进发布包**（内容是相对路径 `..\..`，用户解压到桌面后
  数据根会跑到上级目录）。`verify_release` 已加硬检查
- **`.gitignore` 必须忽略整个 `数据/*`**：改名后旧的 `data/` 规则失效，
  0.24.4 差点把几十 MB 订单表提交进仓库（已改成整目录忽略 + 几个说明文件例外）

## 清数据要连快照一起清（2026-11-03）
- `cli.py` 核算前跑 `snapshots.restore_inbox(sid)`：收件箱删了会从
  `店铺/<店>/快照/<最新>` **自动补回**。只清收件箱 = 白清。
- 数据源实际分散四处：`店铺/<店>/收件箱/{订单表,广告表,联盟佣金表,商品映射}`、
  `店铺/<店>/商品SKU表`、`店铺/<店>/快照`、`链接映射/sku_export_*.xlsx`。
- 报表在 `报表/自动/<店>/`、`报表/手动/<店>/`，不在 `报表/*.xlsx`。
- 别清：`成本/master.csv`、`链接映射/master.csv`、`sku_list_链接资料.csv`、
  各店 `meta.json`/`profile.json`（身份 + 成本 + 历史链接映射，清了会让数字静默偏）。

## 汇率口径：T-1（0.24.47 起）
- 核算用**操作日前一天**的汇率（2 号核算用 1 号）。`src/fx.py::rate_day()`。
- 取价：frankfurter（欧洲央行参考价，免 key，支持按日期），周末自动回落工作日。
- 每天存 `数据/汇率存档/YYYY-MM-DD.json`，同一天重算必须用同一组数，否则净利会飘。
- 退回链：T-1 → 存档 → 最新价 → rates.json 固定值，口径页写明退到哪层。
- ECB 没有 VND；缺币种要明写「无 T-1 报价，用配置固定值」，不能装成取到了。
- 界面 `GET /api/fx` 显示「核算用 X 日的汇率」；排障用 `--fx-day today`。

## 详细表结构联动（改列必看）
- 明细「成本合计」后插了 6 个费用列；`_db = len(DETAIL_HEADERS) + 费用列数 + 1`，
  汇总 SUMIFS 的列字母全靠它。**加列不改这个 → 公式整天指向费用列，不报错但全错**。
- 合计在第 1 行时，数据行不能再按 `ri==0` 判合计（会把首条链接写成假合计行：
  灰底加粗、无公式、无辅助列，切口径不跟着动）。

## 多店铺
- 一家店一个浏览器、各装一份扩展；核算锁按 shop_id 分开，可并行
- 店铺名以「浏览器实际登录的店铺」为准，弹窗选错不会串店

## 落盘与自愈（0.24.2 起）
- **所有配置/状态/成本表必须用 `src/safeio.py` 写**：`write_json/write_text/write_bytes`
  （tmp → fsync → os.replace，自动留 `.bak`）。别再写 `path.write_text(...)`。
- 读：`safeio.read_json(path, default=...)` 会自己从 `.bak` 恢复，并隔离坏文件到 `_损坏文件/`
- 启动自检 `src/selfcheck.py::run()` 已在 `bridge/server.py::main()` 调用；
  手动入口 `POST /api/selfcheck`，界面在「调试 → 自检与修复」
- 异常退出标记：`数据/.running`（启动写、正常退出删）；在标记 = 上次是断电/强杀
- 报表/快照/安装包一律「先写临时名再改名」，不要直接往最终文件名里写

## 模块边界（防止两套逻辑打架）
- **定时只认桥的「计划任务」**：扩展 `checkSchedule()` 在桥在线时直接 return
- 任务队列 TTL：`_AUTO_JOB_TTL_MS`=12h（计划）、`_JOB_TTL_MS`=30min（手动）
- 按店锁 key 一律过 `_canon_shop()`（shop_id / 别名 / 目录名统一成 shop_id）
- 计划状态文件读写必须持 `_SCHED_STATE_LOCK`
- 报表目录/公式：手动看 `settings.report`，计划看规则本身，**不要再回落 `settings.auto`**

## 计划任务（0.24.1）
- 规则存在 `配置/settings.json` 的 `schedules[]`；状态在 `数据/schedule_state.json`
- 一条规则 = 触发(每天/每周几/每月几号+时间) + 周期(day/week/month/last7/all) + 偏移(0本期) + 截到哪天(end/today/day)
- 派单统一走 `_dispatch_rule()` → `_enqueue()` → `auto_run.normalize_range()`
- **派单区间必须补齐 startDate/to/endDateExclusive/startMs/endMs**：缺了订单会按 undefined 查，报「该时段没有订单」
- 漏跑容差 600 秒；补跑用「当时该跑的时刻」算区间，不是用今天

## 约定
- CHANGELOG.md 用 Python 写，别用 heredoc/regex
- 扩展改完要升 `manifest.json` 版本号，否则刷新后看不出变没变

## 站点差异层（加新店不卡住的关键）
- `配置/sites.json`：域名→站点代码/国家/站点特性（`ads_spec` 是否支持规格接口、`download_retry` 下载重试次数）。bridge 通过 `/stores` 一并下发，扩展缓存到 chrome.storage。
- 扩展逻辑：`siteProfile(host)` 读这份档案；未登记的站点走默认（先试规格接口，失败自动兜底），**不需要改代码**。
- 店铺身份只认 `stores/<店名>/meta.json`，核算不再要求 stores.json 里有这家店（cli.py 已兜底）。
- 每类数据统一三段兜底：规格接口 → 页面点导出（拦截下载，不弹另存为）→ 已完成的导出任务。
- 缺表不再静默：`/run` 返回 `missing`，日志和面板都提示「本轮没有广告数据，净利偏高」。

## 广告全自动（2026-10-31 定型，0.21.99）
四层机制，任何店铺/任何电脑都走同一条路，不需要人工介入：
1. **建任务**：`POST /oec_ads/shopping/v1/oec/stat/post_product_list_v2`
   `{action:"download", page_size:-1, dims:["spu_id","campaign_id"], query_list:[...,"mixed_real_cost","extra_currency"], filters:[...]}`
   → `download_task/query` **只查不建**，别再拿它建任务
2. **广告账户 ID**：在**页面 URL 参数 `aadvid`** 上（不在请求头！头里只有 `x-tt-oec-region`）
   程序从 `learnParams` 存下的参数里取；取不到就开广告页 12 秒自学再重试
3. **轮询**：`download_task/query {download_id}` → `task_status:SUCCESS` + `url`（签名地址，无 .xlsx 后缀）
4. **下载**：自己 fetch，不弹保存框

导出表是**商品维度**（带「商品 ID」列），转换器见商品 ID 就跳过，不要再按计划分摊。
币种可能是 USD（LR Home.MY 实测），核算按 USD→本地汇率换算。

平台报错原文对照（`system_msg`）：
- `req adv id missing` → 缺广告账户 ID
- `no download_id provided` → 把 query 当建任务用了
- `21016003` → 导出文件还在生成

## 发布脚本断线处理
GitHub 上传偶发 `IncompleteRead`（响应没读完但文件已传上去）。
`build/do_release.py` 已改为：失败先查附件列表核对大小，对上就算成功，否则重试 3 次。

## 订单导出（2026-10-31, 0.22.2）
`POST /api/fulfillment/order/export` 建任务 → `export_record/get` 轮询 → `order/download`。
- **file_key ≠ export_task_id**：file_key 形如 `b595bc8c907f427bsafe`（16 位十六进制+safe），
  任务号是纯数字。拿任务号当 file_key 去下载，永远回 `21016003`（"文件还在生成"）。
- 建任务返回的 `data.file_key` 有时就是任务号，必须过滤纯数字。

## 核算卡死（0.22.2）
没有订单表时，`jingli.exe run` 子进程抛异常后卡在退出阶段（CPU 0），父进程干等 15 分钟。
- `/run` 前先用 `loader.has_orders_table(dir)` 预检，没有就直接回提示
- 子进程超时 900s → 300s
- 子进程输出按 GBK 兜底解码（打包版是 GBK，强制 utf-8 会乱码）
- 失败时把堆栈翻译成人话，不要把 traceback 丢给界面

## 大店订单（2026-10-31, 0.22.8）
Chroma Home 实测：30 天 **57,703 行 / 40 MB**，平台生成导出文件约 **5 分钟**。
- 以前只等 44 秒就判失败、反复重排任务 → 永远拿不到，还越排越慢
- 正解：建一个整段任务 → 等 48 秒 → 没出来就进 `pendingOrders` 待办，
  由 `pending-watch` 闹钟每分钟收一次（实测 5 分钟内到位）
- **撤销按周切片**：5 个片段在大店同样慢，等于把 1 个慢任务变 5 个
- 订单文件名带区间：`全部 笔订单-20261001-20261030-HHMM.csv`，
  同区间再点运行可直接复用已生成的导出，不重排
- 判定 file_key 真假：真实格式 `b595bc8c907f427bsafe`（16 位十六进制+safe），
  纯数字是 export_task_id，拿它下载必回 21016003
