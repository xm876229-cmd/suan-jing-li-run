#!/usr/bin/env bash
# macOS（Apple Silicon）打包脚本。必须在 macOS 上跑——PyInstaller 不能交叉编译。
#
# 用法（在项目根目录）：
#     bash packaging/build_mac.sh
#
# 产物：
#     dist/JingLiHelper-mac-arm64-v<版本>.zip
# 里面是「净利助手.app」，解压后拖进"应用程序"即可。
#
# 脚本做四件事：
#   1. 准备「默认配置 / 默认数据」：从项目的 配置/、数据/成本/ 拷一份，
#      并把 settings.json 里的计划任务清空（计划属于每台机器，不进包）
#   2. 用 Pillow 从 assets/jingli.png 生成 .icns
#   3. 调 PyInstaller 打 .app（onedir，arm64，窗口模式）
#   4. 用 ditto 压成 zip（保留 .app 里的符号链接，普通 zip 会破坏它）

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "这个脚本只能在 macOS 上运行（当前：$(uname -s)）。"
  echo "Windows 上用 build/do_release.py 出 exe。"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c "import json;print(json.load(open('浏览器扩展/manifest.json',encoding='utf-8'))['version'])")"
export JINGLI_VERSION="$VERSION"
echo "版本：$VERSION"

STAGE="$ROOT/build/mac_stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/默认配置" "$STAGE/默认数据/costs"

echo "准备默认配置（清空计划任务）…"
python3 - "$ROOT" "$STAGE" <<'PY'
import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
stage = Path(sys.argv[2])
src = root / "配置"
dst = stage / "默认配置"
if not src.is_dir():
    raise SystemExit("找不到 配置/ 目录")

for f in sorted(src.glob("*.json")):
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    if f.name == "settings.json" and isinstance(d, dict):
        d["schedules"] = []          # 计划只属于本机，不进包
    (dst / f.name).write_text(
        json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("  ", f.name)

# 成本表在哪个位置因环境而异：项目根 / release / 构建中间产物，按顺序找
for cand in (root / "数据" / "成本" / "master.csv",
             root / "release" / "净利助手" / "数据" / "成本" / "master.csv",
             root / "build" / "app" / "jingli" / "_internal" / "默认数据" / "costs" / "master.csv"):
    if cand.is_file():
        shutil.copy2(cand, stage / "默认数据" / "costs" / "master.csv")
        print(f"   成本表 {cand.relative_to(root)}")
        break
else:
    print("   ⚠ 没找到成本表：这个包首次使用要手动导入 成本表")
PY

echo "生成图标…"
python3 - "$ROOT" "$STAGE" <<'PY'
import sys
from pathlib import Path

from PIL import Image

root = Path(sys.argv[1])
stage = Path(sys.argv[2])
src = root / "assets" / "jingli.png"
if not src.is_file():
    print("   没有 assets/jingli.png，跳过图标")
    raise SystemExit(0)
im = Image.open(src).convert("RGBA")
im.save(stage / "icon.icns",
        sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512)])
print("   icon.icns")
PY

echo "PyInstaller 打包（第一次会慢一些）…"
python3 -m PyInstaller \
  --noconfirm --clean \
  --distpath "$ROOT/dist" \
  --workpath "$ROOT/build/mac_build" \
  "$ROOT/packaging/mac.spec"

APP="$ROOT/dist/净利助手.app"
if [[ ! -d "$APP" ]]; then
  echo "打包失败：没生成 $APP"
  ls -la "$ROOT/dist" || true
  exit 1
fi

OUT="$ROOT/dist/JingLiHelper-mac-arm64-v${VERSION}.zip"
rm -f "$OUT"
# ditto 会保留 .app 里的符号链接与权限；用普通 zip 压完再解压，.app 会打不开
ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"

SIZE="$(du -m "$OUT" | cut -f1)"
echo
echo "完成：$OUT （${SIZE} MB）"
echo "提示：未签名的 .app 首次打开会被拦，右键→打开，或执行"
echo "      xattr -dr com.apple.quarantine \"/Applications/净利助手.app\""
