# 净利助手 · Mac 版（Apple Silicon）

Windows 版和 Mac 版是**同一套代码**：核算、报表、取数、界面完全一样，
只有跟操作系统打交道的几处做了平台分支（见文末）。

## 一、怎么拿到 .app

Mac 版在 GitHub 的 macOS 机器上编译（本机是 Windows，PyInstaller 不能交叉编译）。

1. 打开仓库：`https://github.com/xm876229-cmd/suan-jing-li-run`
2. 点 **Actions** → 左边选 **Build macOS (Apple Silicon)** → 右边 **Run workflow**
3. 等 3～5 分钟，点进这次运行，页面底部 **Artifacts** 下载 `JingLiHelper-mac-arm64`
4. 解压得到 `JingLiHelper-mac-arm64-v版本.zip`，再解压出 **净利助手.app**

> 打标签（`git push origin v0.24.83`）会顺带把它挂到 Release 页，和 exe 放一起。

## 二、第一次打开（必看）

这个 .app **没有 Apple 开发者签名**（自己人用，不做每年 99 美元的公证），
macOS 第一次会拦，提示"无法打开，因为无法验证开发者"或"已损坏"。二选一：

- **右键点图标 → 打开 → 再点"打开"**（只需一次）
- 或者在"终端"里执行一次：
  ```bash
  xattr -dr com.apple.quarantine "/Applications/净利助手.app"
  ```

然后把 .app 拖进「应用程序」文件夹，以后双击即可。

## 三、装上之后要做的三件事

1. **加载浏览器扩展**（程序替不了这一步）
   Chrome → `chrome://extensions` → 右上「开发者模式」→「加载已解压的扩展程序」→
   选 `净利助手.app/Contents/Frameworks/浏览器扩展`
   （在 Finder 里右键 .app →「显示包内容」→ Contents → Frameworks）

2. **打开店铺后台**，扩展几秒内会报出店名，界面上出现店铺卡片。

3. **补成本表**：程序自带 9125 条种子成本；缺的编码到「成本表」页补。

## 四、数据存在哪

Mac 版不把数据写在 .app 里（.app 在 /Applications 是只读的，未签名应用还可能被系统
搬到随机只读路径）。默认位置：

```
~/Library/Application Support/JingLiHelper/
    配置/    数据/    报表/
```

- 想换位置：启动前设环境变量 `JINGLI_HOME=/你的/目录`
- 备份：直接拷这个目录
- 卸载：删掉 .app + 这个目录

Windows 版不受影响，仍然「数据跟着程序目录走」。

## 五、与 Windows 版的差异

| 项目 | Windows | macOS |
|---|---|---|
| 开机自启 | 注册计划任务（schtasks + VBS） | **不注册**（按"手动打开"设计） |
| 结束占用 8765 的进程 | netstat + taskkill | lsof + kill |
| 打开文件夹/网址 | `os.startfile` | `open` |
| 出错弹窗 | MessageBoxW | osascript `display alert` |
| 安装形态 | 安装包 exe / 免安装 zip | .app（zip 分发） |
| 数据位置 | 程序目录 | `~/Library/Application Support/JingLiHelper` |

功能上没有任何删减：取数、售后判定、口径、带公式报表、计划任务（**手动打开程序时才会按时跑**）、
多店铺并行、汇率 T-1 全部一致。

## 六、常见问题

**Q：提示"已损坏，无法打开"？**
没签名被 Gatekeeper 拦了，按第二节的 `xattr` 命令处理，或右键打开。

**Q：双击没反应？**
先看 `~/Library/Application Support/JingLiHelper/数据/日志/` 有没有异常日志；
也可以在终端里跑 `"/Applications/净利助手.app/Contents/MacOS/JingLiHelper" doctor` 看诊断。

**Q：界面能开但扩展连不上？**
确认端口 8765 没被别的程序占：终端 `lsof -nP -iTCP:8765 -sTCP:LISTEN`。
程序启动时会自动清理自己上一次留下的进程，但不认识别人的程序。

**Q：计划任务会自己跑吗？**
Mac 版没有开机自启，程序没开就不会跑；开着的时候到点会跑。
