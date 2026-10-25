# -*- coding: utf-8 -*-
"""
main.py —— CLI 入口：自然语言 -> HYSYS 反应器建模。

用法：
    python main.py "自然语言描述"
    python main.py --offline "自然语言描述"   # 规则解析通道（需 parser 支持）

输出：反应器类型 + 选型理由 + assumptions + missing_info（若有）
      + 出口组成表 + 收敛状态 + case 文件路径。
"""

import argparse
import json
import sys

import parser as llm_parser
import builder


def _print_spec_brief(spec):
    reactor_type = spec.get("reactor_type")
    print("[类型] %s" % (reactor_type if reactor_type is not None else "待定"))
    print("[选型理由] %s" % spec.get("selection_reason", ""))
    for a in spec.get("assumptions") or []:
        print("[假设] %s" % a)


def _print_result(result):
    for w in result.get("warnings") or []:
        print("[警告] %s" % w)
    if result.get("total_molar_flow_kgmol_h") is not None:
        print("[进料] 总摩尔流量 = %.4g kgmole/h"
              % result["total_molar_flow_kgmol_h"])
    outlet = result.get("outlet") or {}
    comp = outlet.get("composition_molfrac") or {}
    if result.get("converged"):
        print("[收敛] 是")
        print("[出口] T = %.2f °C, P = %.4f bar"
              % (outlet.get("T_C"), outlet.get("P_bar")))
        if result.get("heat_duty") is not None:
            print("[热负荷] %.4f MW（正值=吸热）" % result["heat_duty"])
        print("[出口组成（摩尔分率，汽/液加权）]")
        width = max([len(c) for c in comp] + [4])
        for c, x in comp.items():
            print("    %-*s  %.6f" % (width, c, x))
    else:
        print("[收敛] 否（详见上方警告）")
    print("[case 文件] %s" % result.get("case_file"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="自然语言 -> HYSYS 反应器建模")
    ap.add_argument("text", help="自然语言描述")
    ap.add_argument("--offline", action="store_true",
                    help="走规则解析通道（不调用 LLM）")
    args = ap.parse_args(argv)

    # --- 解析 ---
    try:
        if args.offline:
            offline = getattr(llm_parser, "parse_user_input_offline", None)
            if offline is None:
                print("[错误] --offline 规则解析通道尚未在 parser.py 中实现，"
                      "请去掉 --offline 使用 LLM 解析。")
                return 2
            spec = offline(args.text)
        else:
            spec = llm_parser.parse_user_input(args.text)
    except Exception as exc:
        print("[解析失败] %s: %s" % (type(exc).__name__, exc))
        return 2

    _print_spec_brief(spec)
    print("[spec] %s" % json.dumps(spec, ensure_ascii=False))

    # --- 建模 ---
    try:
        result = builder.build_from_spec(spec)
    except builder.core.HysysError as exc:
        print("[建模失败] %s" % exc)
        return 3
    except Exception as exc:
        print("[建模失败] %s: %s" % (type(exc).__name__, exc))
        return 3

    if result.get("status") == "need_clarification":
        print("[需要澄清] 信息不足，未建模。请补充：")
        for q in result.get("questions") or []:
            print("    - %s" % q)
        return 0
    if result.get("status") != "ok":
        print("[建模失败] %s" % result.get("message", result))
        return 3

    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
