# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 待办
- 按 `data/capture/endpoints.json` 里的真实接口实现四类数据自动拉取
- 下载文件自动归档到 `data/exports/<店铺>/<日期>/`
- 多店铺批量跑（配置侧已就绪）
- 网页看板

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
