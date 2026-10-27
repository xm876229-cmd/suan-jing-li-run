"""一键运行的默认参数封装。

没有命令行习惯时用它：双击 scripts\\run.bat，或由任务计划程序调用。

    python -m scripts.run_default            # 本月 1 号 ~ 今天
    python -m scripts.run_default --mode yesterday
    python -m scripts.run_default --mode last-month
    python -m scripts.run_default --mode yesterday --store demo-my

其余参数（--dir / --workbook / --country）会原样透传给 src.cli。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import cli  # noqa: E402


def default_range(mode: str) -> tuple[date, date]:
    today = date.today()
    if mode == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if mode == "last7":
        return today - timedelta(days=6), today
    if mode == "last-month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    return today.replace(day=1), today  # month（默认）


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--mode", default="month",
                    choices=["month", "yesterday", "last7", "last-month"])
    known, rest = ap.parse_known_args()

    dfrom, dto = default_range(known.mode)
    argv = list(rest)
    if "--from" not in argv and "--to" not in argv:
        argv += ["--from", dfrom.isoformat(), "--to", dto.isoformat()]

    # 没指定数据来源时，默认读主工作簿
    if "--workbook" not in argv and "--dir" not in argv:
        wb = next((p for p in ROOT.glob("*.xlsx") if not p.name.startswith("~$")), None)
        if wb is None:
            print("目录下没有找到工作簿，请用 --workbook 或 --dir 指定数据来源")
            return 2
        argv += ["--workbook", str(wb)]

    print(f"区间：{dfrom} ~ {dto}")
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
