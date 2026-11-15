"""检查界面的真实 DOM 结构（不经过任何桩）。

为什么单独做这一项：
    `check_ui_runtime` 用 Node + DOM 桩跑逻辑，但桩里的 `document.querySelector()`
    **永远返回一个元素**——所以「HTML 里根本没有这个节点」它查不出来。
    2026-11-11 连出两次这种事故：
      · 合并卡片时把 `pscope`（国家下拉）弄丢了，逻辑照跑，界面上没有
      · 拼 HTML 时多删了两个 `</div>`，`#fx-items` 被套进 `#fx-formula`，
        点「计算项」整块空白
    这两类都只在真实浏览器里才看得见，必须在打包前用**真实 HTML** 校验。

检查内容：
    1. 标签是否配平（缺 / 多 / 提前闭合都报，并指出卡在哪）
    2. id 是否唯一
    3. 关键容器是否都在
    4. 关键嵌套关系（如 #fx-items 不能套在 #fx-formula 里面）
    5. JS 里 $('#xxx') 引用的 id 是否真的存在于页面（白名单除外）

用法：
    python -m scripts.check_ui_dom
    python -m scripts.check_ui_dom --quiet
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "bridge" / "ui.py"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}

# 必须存在的容器：少了任何一个，界面上就是一块空白
REQUIRED_IDS = [
    # 页面骨架
    "view-overview", "view-reports", "view-calc", "view-fields", "view-settings",
    # 计算中心
    "calc-sticky", "calc-trial", "ptabs",
    "fx-tabs", "fx-formula", "fx-items", "pg-tabs",
    "fbody", "ftray", "freset", "net-check",
    "pbody", "ptbl", "pscope", "pg-hint", "drop-zero",
    "fee-overview", "fee-audit", "rtbl", "btn-rate-save-top", "btn-rate-save",
    "ps-list", "pr-list", "btn-save", "btn-preview",
]

# JS 里引用的、但由脚本动态生成的 id（不在静态 HTML 里是正常的）
DYNAMIC_IDS = {
    "btn-fee-new", "fee-audit-fix", "net-fix", "trial-retry",
    "calc-sticky-holder", "imp-all", "imp-del", "imp-sel", "plan-catchup",
    "toast", "diff-body",
}

# 关键嵌套：子 id → 不允许出现在哪些父 id 内部
FORBIDDEN_NEST = {
    "fx-items": {"fx-formula"},
    "fx-formula": {"fx-items"},
    "pbody": {"fx-formula"},
    "fbody": {"fx-items"},
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.parent = parent


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.cur = self.root
        self.errors: list[str] = []
        self.ids: dict[str, Node] = {}
        self.dups: set[str] = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        node = Node(tag, d, self.cur)
        self.cur.children.append(node)
        if d.get("id"):
            if d["id"] in self.ids:
                self.dups.add(d["id"])
            self.ids[d["id"]] = node
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is None:
            self.errors.append(f"多出来的 </{tag}>（当前在 <{self.cur.tag}> 里面）")
            return
        # 中间那些没闭合的，逐个报出来
        while self.cur is not node:
            self.errors.append(f"<{self.cur.tag}> 没有闭合就被 </{tag}> 截断了")
            self.cur = self.cur.parent
        self.cur = node.parent

    def close(self):
        super().close()
        while self.cur is not self.root:
            self.errors.append(f"<{self.cur.tag}> 到文件末尾都没有闭合")
            self.cur = self.cur.parent


def _html_part(text: str) -> str:
    """拿到完整 HTML：把 <script> 里的内容剥掉（脚本不算 DOM），标签本身留着。

    不能只取 <script> 之前那段——那样 </body></html> 会被判成「没有闭合」，
    是个假警报。
    """
    return re.sub(r"<script\b[^>]*>.*?</script>", "<script></script>",
                  text, flags=re.S)


def _script_part(text: str) -> str:
    return "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S))


def _chain(node: Node) -> list[str]:
    out, cur = [], node.parent
    while cur is not None:
        if cur.attrs.get("id"):
            out.append(cur.attrs["id"])
        out.append(cur.tag)
        cur = cur.parent
    return out


def main(argv: list[str] | None = None) -> int:
    quiet = "--quiet" in sys.argv
    if not UI.exists():
        print(f"[失败] 找不到 {UI}")
        return 1

    src = UI.read_text(encoding="utf-8")
    html = _html_part(src)
    js = _script_part(src)

    b = DomBuilder()
    b.feed(html)
    b.close()

    problems: list[str] = []

    # 1) 标签配平
    problems += b.errors[:8]

    # 2) id 唯一
    if b.dups:
        problems.append("id 重复：" + "、".join(sorted(b.dups)))

    # 3) 关键容器
    for rid in REQUIRED_IDS:
        if rid not in b.ids:
            problems.append(f"缺少容器 #{rid}")

    # 4) 关键嵌套
    for child, bad_parents in FORBIDDEN_NEST.items():
        node = b.ids.get(child)
        if node is None:
            continue
        chain = _chain(node)
        hit = [p for p in bad_parents if p in chain]
        if hit:
            problems.append(f"#{child} 被套在 #{hit[0]} 里面（切页签时会跟着一起消失）")

    # 5) JS 引用的 id 是否存在
    refs = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    missing = sorted(r for r in refs
                     if r not in b.ids and r not in DYNAMIC_IDS)
    if missing:
        problems.append("JS 里引用了、但 HTML 里没有的 id：" + "、".join(missing[:10]))

    if quiet:
        print("DOM 结构：" + ("通过" if not problems else f"失败（{len(problems)} 项）"))
    else:
        print(f"id 共 {len(b.ids)} 个，检查了 {len(REQUIRED_IDS)} 个关键容器、"
              f"{len(refs)} 个 JS 引用")
        if problems:
            print("\n发现问题：")
            for p in problems:
                print("  -", p)
        else:
            print("DOM 结构检查通过：标签配平、id 唯一、关键容器齐全、嵌套正确")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
