# -*- coding: utf-8 -*-
"""通用性回归：4 个现场反应，全程走 自然语言 -> LLM parser -> builder。

- 不允许手工构造 spec；4 个用例在同一 Python 进程内连续执行（复用 HYSYS）
- 每个用例间隔 3 秒
- 输出 docs/generalization_test.md（markdown 表）+ docs/gen_results/*.json
- 末尾跑静态硬编码检查（Select-String）
"""
import json
import os
import subprocess
import sys
import time

import parser as llm_parser
import builder

ROOT = os.path.dirname(os.path.abspath(__file__))

CASES = [
    {"id": 1,
     "prompt": "甲烷与空气在过量空气下完全燃烧，进料25℃，1 atm，"
               "产物分布未知，模拟绝热燃烧炉出口温度和烟气组成。"
               "空气过量100%",
     "expected": ["gibbs"],
     "note": "多反应、产物分布未知"},
    {"id": 2,
     "prompt": "模拟合成氨反应：N2 + 3H2 -> 2NH3，进料25℃、200 atm、"
               "氢氮比3:1，单程转化率15%，绝热反应器",
     "expected": ["conversion", "equilibrium"],
     "note": "给了转化率故 conversion 更合适；equilibrium 需看理由"},
    {"id": 3,
     "prompt": "模拟乙苯脱氢制苯乙烯：C6H5C2H5 <-> C6H5C2H3 + H2，可逆反应，"
               "进料620℃、1.2 bar，无动力学数据，反应器出口650℃，求产物分布",
     "expected": ["equilibrium"],
     "note": "可逆、无动力学、无转化率，受化学平衡控制"},
    {"id": 4,
     "prompt": "模拟乙醇在液相中的水解反应，反应器为釜式，已给出动力学参数"
               "k=0.05 1/s，活化能 50000 kJ/kmol，进料25℃",
     "expected": ["cstr"],
     "note": "液相 + 动力学参数"},
]


def run_case(case):
    t0 = time.time()
    rec = {"id": case["id"], "prompt": case["prompt"],
           "expected": case["expected"], "note": case["note"],
           "parse_error": None, "spec": None, "build_error": None,
           "result": None, "elapsed_s": None}
    # 1) LLM 解析
    try:
        spec = llm_parser.parse_user_input(case["prompt"])
        rec["spec"] = spec
    except Exception as exc:
        rec["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    # 2) 建模
    try:
        result = builder.build_from_spec(spec)
        rec["result"] = result
    except Exception as exc:
        rec["build_error"] = "%s: %s" % (type(exc).__name__, exc)
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def verdict(rec):
    """(选型判定, 建模状态, 收敛, 备注)"""
    spec = rec["spec"]
    if rec["parse_error"]:
        return "N/A", "解析失败", "-", rec["parse_error"][:120]
    actual = spec.get("reactor_type")
    type_ok = actual in rec["expected"]
    type_str = "正确" if type_ok else "错误"
    reason = (spec.get("selection_reason") or "")[:60]
    if rec["build_error"]:
        return type_str, "失败", "-", rec["build_error"][:120]
    r = rec["result"]
    if r.get("status") == "need_clarification":
        qs = "；".join(r.get("questions", []))[:100]
        return type_str, "correct_clarification", "-", qs
    conv = "是" if r.get("converged") else "否"
    remark = reason
    if not r.get("converged"):
        w = "；".join(r.get("warnings", []))[:100]
        remark = "未收敛：" + w
    return type_str, "成功", conv, remark


def main():
    records = []
    for case in CASES:
        print("=" * 70)
        print("用例 %d | 预期 %s" % (case["id"], "/".join(case["expected"])))
        print("提示词：%s" % case["prompt"])
        rec = run_case(case)
        records.append(rec)
        if rec["parse_error"]:
            print("[解析失败] %s" % rec["parse_error"])
        else:
            spec = rec["spec"]
            print("[选型] %s（预期 %s）" % (spec.get("reactor_type"),
                                              "/".join(case["expected"])))
            print("[理由] %s" % spec.get("selection_reason"))
            if rec["build_error"]:
                print("[建模失败] %s" % rec["build_error"])
            else:
                r = rec["result"]
                if r.get("status") == "need_clarification":
                    print("[correct_clarification]")
                    for q in r.get("questions", []):
                        print("    - %s" % q)
                else:
                    out = r.get("outlet") or {}
                    print("[收敛] %s  [T] %s °C  [耗时] %s s"
                          % (r.get("converged"), out.get("T_C"),
                             rec["elapsed_s"]))
                    comp = out.get("composition_molfrac") or {}
                    top = sorted(comp.items(), key=lambda kv: -kv[1])[:4]
                    print("[主要组分] %s" % ", ".join(
                        "%s=%.4f" % kv for kv in top))
        if case is not CASES[-1]:
            time.sleep(3)

    # --- 存 result JSON ---
    os.makedirs(os.path.join(ROOT, "docs", "gen_results"), exist_ok=True)
    for rec in records:
        p = os.path.join(ROOT, "docs", "gen_results",
                         "case%d.json" % rec["id"])
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)

    # --- markdown 表 ---
    lines = ["# 通用性回归测试", "",
             "| 用例 | 预期 | 实际 | 选型正确 | 建模结果 | 收敛 | 耗时(s) | 备注 |",
             "|---|---|---|---|---|---|---|---|"]
    for rec in records:
        spec = rec["spec"] or {}
        actual = spec.get("reactor_type") or "-"
        t_ok, build_st, conv, remark = verdict(rec)
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s |"
                     % (rec["id"], "/".join(rec["expected"]), actual,
                        t_ok, build_st, conv, rec["elapsed_s"],
                        remark.replace("|", "\\|")))
    md = "\n".join(lines) + "\n"
    md_path = os.path.join(ROOT, "docs", "generalization_test.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print("")
    print(md)

    # --- 静态硬编码检查 ---
    print("=" * 70)
    print("[静态检查] builder.py / main.py / parser.py 物质名扫描")
    cp = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Select-String -Path builder.py,main.py,parser.py "
         "-Pattern 'Methane|Toluene|Ethylbenzene|甲烷|甲苯|乙苯' "
         "| ForEach-Object { '{0}:{1}: {2}' -f $_.Filename, $_.LineNumber, "
         "$_.Line.Trim() }"],
        capture_output=True, text=True, cwd=ROOT)
    hits = (cp.stdout or "").strip()
    print(hits if hits else "（零命中，无物质名出现）")
    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\n## 静态硬编码检查\n\n```\n%s\n```\n"
                % (hits or "（零命中）"))

    print("")
    print("结果已存：%s 与 docs/gen_results/" % md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
