# -*- coding: utf-8 -*-
"""
main.py —— CLI 入口：自然语言 -> HYSYS 反应器建模。

用法：
    python main.py "自然语言描述"
    python main.py --file prompts/smr.txt       # 从 UTF-8 文件读取提示词
    python main.py --offline "自然语言描述"     # 规则解析通道（需 parser 支持）

输出：反应器类型 + 选型理由 + assumptions + missing_info（若有）
      + 出口组成表 + 收敛状态 + case 文件路径。
"""

import sys

# Windows 控制台默认 cp1252/cp936，统一重配置为 UTF-8，避免中文输出
# UnicodeEncodeError（不依赖外部 PYTHONIOENCODING 设置）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from datetime import datetime, timedelta

import parser as llm_parser
import builder


def out(text=""):
    """统一输出函数：所有用户可见文本都经过这里。"""
    sys.stdout.write(str(text) + "\n")
    sys.stdout.flush()


def _print_spec_brief(spec):
    reactor_type = spec.get("reactor_type")
    out("[类型] %s" % (reactor_type if reactor_type is not None else "待定"))
    out("[选型理由] %s" % spec.get("selection_reason", ""))
    for a in spec.get("assumptions") or []:
        out("[假设] %s" % a)


def _print_result(result):
    for w in result.get("warnings") or []:
        out("[警告] %s" % w)
    if result.get("total_molar_flow_kgmol_h") is not None:
        out("[进料] 总摩尔流量 = %.4g kgmole/h"
            % result["total_molar_flow_kgmol_h"])
    outlet = result.get("outlet") or {}
    comp = outlet.get("composition_molfrac") or {}
    if result.get("converged"):
        out("[收敛] 是")
        out("[出口] T = %.2f °C, P = %.4f bar"
            % (outlet.get("T_C"), outlet.get("P_bar")))
        if result.get("heat_duty") is not None:
            out("[热负荷] %.4f MW（正值=吸热）" % result["heat_duty"])
        out("[出口组成（摩尔分率，汽/液加权）]")
        width = max([len(c) for c in comp] + [4])
        for c, x in comp.items():
            out("    %-*s  %.6f" % (width, c, x))
    else:
        out("[收敛] 否（详见上方警告）")
    out("[case 文件] %s" % result.get("case_file"))


def _log_clock():
    """打印系统时间与时区；非 Asia/Shanghai（UTC+8）仅提示，不改设置。"""
    lt = datetime.now().astimezone()
    off = lt.utcoffset()
    out("[时钟] 系统时间 %s，时区偏移 UTC%s" % (lt.strftime("%Y-%m-%d %H:%M:%S"), off))
    if off != timedelta(hours=8):
        out("[提示] 当前时区不是 Asia/Shanghai（UTC+8）；仅提示，不改系统设置")


def main(argv=None):
    ap = argparse.ArgumentParser(description="自然语言 -> HYSYS 反应器建模")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("text", nargs="?", help="自然语言描述")
    src.add_argument("--file", metavar="PATH",
                     help="从 UTF-8 文本文件读取提示词（兼容记事本 BOM）")
    ap.add_argument("--offline", action="store_true",
                    help="走规则解析通道（不调用 LLM）")
    ap.add_argument("--save", metavar="PATH",
                    help="把完整结果（spec + 建模结果）以 UTF-8 JSON 写入文件"
                         "（绕开 PowerShell 管道编码）")
    args = ap.parse_args(argv)

    _log_clock()

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8-sig") as f:
                text = f.read().strip()
        except OSError as exc:
            out("[错误] 无法读取提示词文件 %s: %s" % (args.file, exc))
            return 2
        if not text:
            out("[错误] 提示词文件为空：%s" % args.file)
            return 2
    elif args.text:
        text = args.text
    else:
        out("[错误] 请提供自然语言描述，或用 --file 指定提示词文件。")
        return 2

    # --- 解析 ---
    try:
        if args.offline:
            offline = getattr(llm_parser, "parse_user_input_offline", None)
            if offline is None:
                out("[错误] --offline 规则解析通道尚未在 parser.py 中实现，"
                    "请去掉 --offline 使用 LLM 解析。")
                return 2
            spec = offline(text)
        else:
            spec = llm_parser.parse_user_input(text)
    except Exception as exc:
        out("[解析失败] %s: %s" % (type(exc).__name__, exc))
        return 2

    _print_spec_brief(spec)
    out("[spec] %s" % json.dumps(spec, ensure_ascii=False))

    # --- 建模 ---
    try:
        result = builder.build_from_spec(spec)
    except builder.core.HysysError as exc:
        out("[建模失败] %s" % exc)
        return 3
    except Exception as exc:
        out("[建模失败] %s: %s" % (type(exc).__name__, exc))
        return 3

    if args.save:
        try:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump({"prompt": text, "spec": spec, "result": result},
                          f, ensure_ascii=False, indent=2)
            out("[结果已保存] %s" % args.save)
        except OSError as exc:
            out("[警告] 结果文件写入失败 %s: %s" % (args.save, exc))

    if result.get("status") == "need_clarification":
        out("[需要澄清] 信息不足，未建模。请补充：")
        for q in result.get("questions") or []:
            out("    - %s" % q)
        return 0
    if result.get("status") != "ok":
        out("[建模失败] %s" % result.get("message", result))
        return 3

    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
