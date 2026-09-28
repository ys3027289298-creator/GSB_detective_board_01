"""侦探证据板 CMD 菜单。

用法：
    python -m detective          交互模式（stdin 逐行读命令）
    python -m detective --demo   使用仓库根目录的固定演示输入 demo_input.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

from .core import Case, DetectiveBoard, GameLockedError, UnknownNodeError

ROOT = Path(__file__).resolve().parent.parent
CASE_FILE = ROOT / "cases" / "case01.json"
DEMO_INPUT = ROOT / "demo_input.txt"

MENU = """====== 侦探证据板 ======
 1) 查看案件状态
 2) 合并证据 <节点A> <节点B>
 3) 排除证据 <证据id>
 4) 推进一轮推理
 5) 查看时间线
 6) 确认证词 <证词id>
 7) 指认凶手 <人物id>
 8) 撤销上一步
 9) 保存 <路径>
10) 加载 <路径>
 0) 退出
======================"""


def _print_status(case, out):
    print(f"案件 {case.case_id}《{case.title}》 状态={case.state} 轮次={case.round}", file=out)
    print(f"  人物: {sorted(case.persons)}", file=out)
    print(f"  证据: {sorted(case.evidence)}", file=out)
    print(f"  边:   {sorted(case.edges)}", file=out)
    print(f"  排除: {sorted(case.excluded)}", file=out)
    print(f"  结局可触发: {case.ending_available()}", file=out)


def _print_timeline(case, out):
    for event in case.sorted_timeline():
        time = event.time if event.time is not None else "未知时间"
        print(f"  [{time}] {event.description}", file=out)


def run_command(case, parts, out):
    """执行一条菜单命令，返回 (case, should_quit)。"""
    cmd = parts[0]
    if cmd == "1":
        _print_status(case, out)
    elif cmd == "2":
        merged = case.merge(parts[1], parts[2])
        print("合并成功" if merged else "边已存在，忽略重复合并", file=out)
    elif cmd == "3":
        case.exclude(parts[1])
        print(f"已排除 {parts[1]}", file=out)
    elif cmd == "4":
        case.advance_round()
        print(f"进入第 {case.round} 轮", file=out)
    elif cmd == "5":
        _print_timeline(case, out)
    elif cmd == "6":
        ok = case.confirm_testimony(parts[1])
        print("证词已确认" if ok else "证词与已确认证词矛盾，标记为矛盾", file=out)
    elif cmd == "7":
        state = case.accuse(parts[1])
        print("指认正确，案件告破！" if state == "solved" else "指认错误，案件失败。", file=out)
    elif cmd == "8":
        print("已撤销" if case.undo() else "没有可撤销的操作", file=out)
    elif cmd == "9":
        case.save(parts[1])
        print(f"已保存到 {parts[1]}", file=out)
    elif cmd == "10":
        case = Case.load(parts[1])
        print(f"已加载 {parts[1]}（轮次={case.round}）", file=out)
    elif cmd == "0":
        return case, True
    else:
        print(f"未知命令: {cmd}", file=out)
    return case, False


def run(case, lines, out):
    print(MENU, file=out)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        print(f"> {line}", file=out)
        try:
            case, quit_ = run_command(case, line.split(), out)
        except GameLockedError as exc:
            print(f"案件已锁定: {exc}", file=out)
        except (UnknownNodeError, KeyError) as exc:
            print(f"节点不存在: {exc}", file=out)
        except IndexError:
            print("命令参数不足", file=out)
        if quit_:
            break
    print("调查结束。", file=out)
    return case


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    board = DetectiveBoard()
    case = board.add_case(Case.load(CASE_FILE))
    if "--demo" in argv:
        with open(DEMO_INPUT, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin
    run(case, lines, sys.stdout)


if __name__ == "__main__":
    main()
