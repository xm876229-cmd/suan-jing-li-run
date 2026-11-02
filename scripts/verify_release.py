"""发布前 / 发布后校验：确认「传上去的确实是这一版」。

为什么要有这个
--------------
打包目录 `release/` 是会残留的：上一次打好的 exe 如果这次忘了重新构建，
`do_release.py` 照样会把**旧包**传上去，而且大小、附件名全对，看不出问题。
用户下载回来一跑，发现还是老版本——这种错只有装的人才会发现。

所以这里做四类检查，任何一项不过就拒绝发布：

  1. 版本源一致：扩展 manifest / CHANGELOG / 传入的 tag 三者对得上
  2. 构建够新：exe 的修改时间晚于所有源码的修改时间（没重打包就会被抓出来）
  3. 包内内容：zip 里的版本号、关键模块、内层 exe 与 release 目录逐个核对
  4. 真跑一遍（最重要）：把 release 里的 exe 起在临时目录 + 冷门端口上，
     读 /health 和几个新接口。跑起来的版本对不上，前面全对也没用。

用法：
    python -m scripts.verify_release              # 只查本地（上传前）
    python -m scripts.verify_release --remote     # 再和 GitHub 上的发布对比
    python -m scripts.verify_release --no-run     # 跳过「真跑一遍」（省 10 秒）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "release" / "净利助手"
EXE = APP_DIR / "jingli.exe"
PORTABLE = ROOT / "release" / "净利助手-免安装.zip"
SETUP = ROOT / "release" / "净利助手-安装包.exe"
MANIFEST = ROOT / "浏览器扩展" / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"

REPO = "xm876229-cmd/suan-jing-li-run"

# 每个版本新增的、能代表「这一版」的模块。老包里没有 → 说明传的是旧包。
REQUIRED_SOURCES = ["src/safeio.py", "src/selfcheck.py"]


# ---------------- 小工具 ----------------

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version() -> str:
    return str(json.loads(MANIFEST.read_text(encoding="utf-8")).get("version") or "")


def changelog_version() -> str:
    """CHANGELOG 里最新的那个版本号（第一个 `## [vX]` ）。"""
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        m = re.match(r"##\s*\[v([0-9][^\]]*)\]", line.strip())
        if m:
            return m.group(1)
    return ""


def newest_source_mtime() -> tuple[float, str]:
    """最近改动过的源码时间。exe 比它旧，就说明没重新打包。"""
    newest, who = 0.0, ""
    for d in ("src", "bridge", "scripts", "installer"):
        for f in (ROOT / d).rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            try:
                t = f.stat().st_mtime
            except OSError:
                continue
            if t > newest:
                newest, who = t, str(f.relative_to(ROOT))
    return newest, who


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, timeout: float = 3.0):
    with _NO_PROXY.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _pid_on_port(port: int) -> int:
    """谁在监听这个端口。用来兜底清掉验证时起的进程。"""
    if os.name != "nt":
        return 0
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15, errors="replace").stdout
    except Exception:  # noqa: BLE001
        return 0
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0].upper().startswith("TCP")
                and parts[1].endswith(f":{port}") and "LISTEN" in parts[3].upper()):
            if parts[4].isdigit() and parts[4] != "0":
                return int(parts[4])
    return 0


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=20)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# ---------------- 各类检查 ----------------

def check_versions(tag: str) -> list[str]:
    """扩展、CHANGELOG、tag 三者必须是同一个版本。"""
    bad: list[str] = []
    av, cv = app_version(), changelog_version()
    tv = tag.lstrip("vV")
    if not av:
        bad.append("读不到 浏览器扩展/manifest.json 的 version")
    if not cv:
        bad.append("CHANGELOG 里找不到版本号")
    if av and cv and av != cv:
        bad.append(f"版本不一致：扩展 {av} / CHANGELOG {cv}")
    if tv and av and av != tv:
        bad.append(f"版本不一致：扩展 {av} / 要发布的 {tag}")
    if not bad:
        print(f"  ✓ 版本三处一致：{av}")
    return bad


def check_fresh(tag: str) -> list[str]:
    """exe 必须比源码新，否则就是拿旧包在发。"""
    bad: list[str] = []
    if not EXE.is_file():
        return [f"找不到 {EXE.relative_to(ROOT)}（还没打包？）"]
    exe_t = EXE.stat().st_mtime
    src_t, who = newest_source_mtime()
    if src_t > exe_t + 1:
        bad.append("exe 比源码旧：最近改的是 "
                   f"{who}（{time.strftime('%H:%M:%S', time.localtime(src_t))}），"
                   f"exe 却是 {time.strftime('%H:%M:%S', time.localtime(exe_t))}。"
                   "先跑 python -m scripts.sync_all --build")
    else:
        print(f"  ✓ exe 比源码新（exe {time.strftime('%m-%d %H:%M', time.localtime(exe_t))}）")
    if not PORTABLE.is_file() or not SETUP.is_file():
        bad.append("发布目录里缺少免安装包或安装包")
    return bad


def check_portable(version: str) -> list[str]:
    """免安装包：版本号、关键模块、内层 exe 都要对。"""
    bad: list[str] = []
    if not PORTABLE.is_file():
        return ["免安装 zip 不存在"]
    with zipfile.ZipFile(PORTABLE) as z:
        names = z.namelist()

        def find(suffix: str) -> str:
            for n in names:
                if n.replace("\\", "/").endswith(suffix):
                    return n
            return ""

        mf = find("浏览器扩展/manifest.json")
        if not mf:
            bad.append("zip 里找不到 浏览器扩展/manifest.json")
        else:
            v = str(json.loads(z.read(mf).decode("utf-8-sig")).get("version") or "")
            if v != version:
                bad.append(f"zip 内扩展版本 {v} ≠ {version}（传的是旧包）")

        cl = find("CHANGELOG.md")
        if not cl:
            bad.append("zip 里找不到 CHANGELOG.md")
        else:
            first = ""
            for line in z.read(cl).decode("utf-8-sig", "replace").splitlines():
                if line.startswith("## ["):
                    first = line
                    break
            if version not in first:
                bad.append(f"zip 内 CHANGELOG 最新版是「{first}」≠ {version}")

        for rel in REQUIRED_SOURCES:
            if not find("源码/" + rel) and not find(rel):
                bad.append(f"zip 里缺少 {rel}（老包没有这个文件）")

        # ---- 包里不能夹带「这台机器的私货」----
        # 数据根就在程序目录里，稍不留神就把订单/快照/报表打进去：
        # 体积几百 MB，还是别人不该看到的经营数据。
        # 更隐蔽的是 数据目录.txt：里面写的是相对路径，用户解压到桌面，
        # 数据根会解析到「桌面的上一级」——数据和配置散在程序目录外面。
        priv = [n for n in names
                if n.replace("\\", "/").split("/")[1:2] == ["数据"]
                and n.replace("\\", "/").split("/")[2:3]
                and n.replace("\\", "/").split("/")[2] in
                ("店铺", "stores", "抓包记录", "capture", "导出", "exports",
                 "日志", "logs", "回收站")]
        if priv:
            bad.append(f"zip 里混入了店铺/抓包数据 {len(priv)} 个文件"
                       f"（例：{priv[0]}）")
        if any(n.replace("\\", "/").endswith("/数据目录.txt") for n in names):
            bad.append("zip 里带了 数据目录.txt（相对路径，会让用户数据根跑到程序目录外）")
        seeds = [n for n in names
                 if n.replace("\\", "/").split("/")[1:2] == ["数据"]]
        if len(seeds) > 6:
            bad.append(f"zip 的 数据/ 下文件偏多（{len(seeds)} 个），只该有几个种子文件")

        inner = find("jingli.exe")
        if not inner:
            bad.append("zip 里找不到 jingli.exe")
        elif EXE.is_file():
            h_zip = hashlib.sha256(z.read(inner)).hexdigest()
            h_dir = sha256(EXE)
            if h_zip != h_dir:
                bad.append("zip 里的 jingli.exe 和 release 目录里的不是同一个文件"
                           "（打包后有人动过目录？）")

    if not bad:
        print(f"  ✓ 免安装包内容对得上（版本 {version}、{len(REQUIRED_SOURCES)} 个新模块、内层 exe 一致）")
    return bad


def check_setup() -> list[str]:
    """安装包只有二进制，验证「它比 app.zip 新」+ 体积合理。"""
    bad: list[str] = []
    if not SETUP.is_file():
        return ["安装包不存在"]
    size = SETUP.stat().st_size
    if size < 15 * 1024 * 1024:
        bad.append(f"安装包只有 {size/1024/1024:.1f} MB，像是被打断的半成品")
    appzip = ROOT / "build" / "app.zip"
    if appzip.is_file() and SETUP.stat().st_mtime + 1 < appzip.stat().st_mtime:
        bad.append("安装包比 build/app.zip 旧，可能是上一次留下的")
    if not bad:
        print(f"  ✓ 安装包正常（{size/1024/1024:.1f} MB）")
    return bad


def check_runtime(version: str) -> list[str]:
    """真跑一遍 release 里的 exe：跑起来的版本才对算数。

    用临时数据目录 + 随机端口，不碰用户的数据，也不占 8765。
    """
    bad: list[str] = []
    if not EXE.is_file():
        return ["没有 exe，跳过运行检查"]
    tmp = Path(tempfile.mkdtemp(prefix="jingli_verify_"))
    port = free_port()
    env = dict(os.environ)
    env["JINGLI_HOME"] = str(tmp)
    env["JINGLI_PORT"] = str(port)
    proc = None
    try:
        proc = subprocess.Popen([str(EXE), "serve"], cwd=str(APP_DIR), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        info = None
        for _ in range(40):                 # 最多等 20 秒
            time.sleep(0.5)
            try:
                info = http_json(base + "/health", timeout=1.5)
                if info.get("ok"):
                    break
            except Exception:  # noqa: BLE001
                info = None
        if not info or not info.get("ok"):
            bad.append("release 里的 exe 起不来（可能被打包坏了）")
            return bad

        ext = str(info.get("extVersion") or "")
        if ext != version:
            bad.append(f"跑起来的程序读到的扩展版本是 {ext} ≠ {version}")

        # 这一版才有的接口：老包没有这两个
        for path, label in (("/api/selfcheck", "自检接口"), ("/api/schedules", "计划任务接口")):
            try:
                http_json(base + path, timeout=3)
            except Exception as e:  # noqa: BLE001
                bad.append(f"缺少{label}（{path}）：{e}")

        if not bad:
            print(f"  ✓ 真跑了一遍：版本 {ext}，自检/计划任务接口都在")
    finally:
        if proc and proc.poll() is None:
            kill_tree(proc.pid)
        # 兜底：按端口找人。守护进程可能已经不在进程树里了，
        # 留一个后台桥会占着端口、还会挡住下一次打包（exe 被锁）。
        for _ in range(6):
            pid = _pid_on_port(port)
            if not pid:
                break
            kill_tree(pid)
            time.sleep(0.5)
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
    return bad


def _github_token() -> str:
    p = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, timeout=60)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def check_remote(tag: str) -> list[str]:
    """和 GitHub 上的发布比一遍：附件哈希、草稿状态、tag 指向。"""
    bad: list[str] = []
    tok = _github_token()
    if not tok:
        return ["取不到 GitHub 凭据，跳过远程核对"]

    def api(url: str):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + tok,
            "Accept": "application/vnd.github+json",
            "User-Agent": "jingli-verify"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")

    try:
        rel = api(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    except Exception as e:  # noqa: BLE001
        return [f"远程没有 {tag} 这个发布（{e}）"]

    if rel.get("draft"):
        bad.append("这个发布还是草稿状态，别人看不到")
    print(f"  · 远程发布：{rel.get('name')}（draft={rel.get('draft')}）")

    want = {
        f"JingLiHelper-Setup-{tag}.exe": SETUP,
        f"JingLiHelper-Portable-{tag}.zip": PORTABLE,
    }
    got = {a["name"]: a for a in rel.get("assets", [])}
    for name, lp in want.items():
        a = got.get(name)
        if not a:
            bad.append(f"远程缺少附件 {name}")
            continue
        if not lp.is_file():
            continue
        lh = sha256(lp)
        if a.get("size") != lp.stat().st_size:
            bad.append(f"{name} 大小不一致：远程 {a.get('size')} / 本地 {lp.stat().st_size}")
        dg = a.get("digest") or ""
        if dg:
            if dg != "sha256:" + lh:
                bad.append(f"{name} 哈希不一致（远程 {dg}）")
            else:
                print(f"  ✓ {name} 远程哈希与本地相同")
        else:
            print(f"  · {name} 远程未返回哈希，只比了大小")
    if not bad:
        print(f"  ✓ 远程附件齐了，且和本地是同一份文件")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布校验")
    ap.add_argument("--tag", default="", help="要核对的版本号，如 v0.24.2；默认取扩展版本")
    ap.add_argument("--remote", action="store_true", help="再和 GitHub 上的发布对比")
    ap.add_argument("--no-run", action="store_true", help="跳过启动 exe 的运行检查")
    args = ap.parse_args(argv)

    ver = app_version()
    tag = args.tag or (f"v{ver}" if ver else "")
    print(f"\n=== 发布校验（{tag or '未知版本'}）===\n")

    problems: list[str] = []
    print("[1/4] 版本源")
    problems += check_versions(tag)
    print("[2/4] 构建新鲜度")
    problems += check_fresh(tag)
    print("[3/4] 包内容")
    problems += check_portable(ver)
    problems += check_setup()
    if not args.no_run:
        print("[4/4] 真跑一遍")
        problems += check_runtime(ver)
    else:
        print("[4/4] 真跑一遍（已跳过）")
    if args.remote:
        print("[远程] 与 GitHub 发布对比")
        problems += check_remote(tag)

    print()
    if problems:
        print("❌ 校验没通过：")
        for p in problems:
            print("   -", p)
        return 1
    print("✅ 全部通过：包的版本、内容、实际运行都对得上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
