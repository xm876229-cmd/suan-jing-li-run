# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 待办
- 净利核算逻辑脚本化（`scripts/`），实现从原始数据到结果的自动复现
- 退款 / 取消订单口径统一校验

## [v0.1.0] - 2026-10-27

### 新增
- 初始化仓库结构与版本管理规范
- 纳入主工作簿 `V1-净利  -15.xlsx`：汇率-参数 / 成本 / 链接资料 / 订单 / 联盟佣金 / 广告 / 净利润 共 8 张表
- `README.md`：核算口径、工作表说明、目录约定
- `docs/GIT-WORKFLOW.md`：分支策略、提交规范、二进制文件管理、版本号与标签、回滚方式
- `.gitignore` / `.gitattributes`：Office 临时文件、产出目录、换行符与二进制文件规范
