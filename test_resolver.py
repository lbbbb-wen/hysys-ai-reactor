# -*- coding: utf-8 -*-
"""组分解析（别名 + 分子量校验）验证 + 用例3 与两必测场景回归。

输出汇总到 docs/component_resolver.txt。
"""
import sys
import time

import builder
from hysys import core

LINES = []


def log(s=""):
    print(s)
    LINES.append(str(s))


def main():
    log("组分解析器验证报告  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 66)
    all_ok = True

    # [1] 乙苯经别名解析为 E-Benzene，分子量读回 ≈106.17
    log("[1] Ethylbenzene 别名解析（canonical -> E-Benzene）")
    case = core.create_case()
    cl = case.BasisManager.ComponentLists.Item(0)
    case.BasisManager.StartBasisChange()
    recs = []
    actual = core.resolve_component(cl.Components, "Ethylbenzene",
                                    records=recs)
    case.BasisManager.EndBasisChange()
    r = recs[0]
    ok = actual == "E-Benzene" and abs(r["mw"] - 106.17) / 106.17 < 0.02
    all_ok &= ok
    log("  请求 Ethylbenzene -> 实得 %s(%s, MW=%.3f)  [%s]"
        % (actual, r["formula"], r["mw"], "PASS" if ok else "FAIL"))
    case.Close(False)

    # [2] 故意误配：alias 表把 Ethylbenzene -> Styrene，必须报误配
    log("")
    log("[2] 误配注入测试：alias 表 Ethylbenzene -> Styrene")
    case = core.create_case()
    cl = case.BasisManager.ComponentLists.Item(0)
    case.BasisManager.StartBasisChange()
    bad = dict(core.load_aliases())
    bad["Ethylbenzene"] = {"sim_name": "Styrene", "formula": "C8H10"}
    try:
        core.resolve_component(cl.Components, "Ethylbenzene",
                               aliases=bad, records=[])
        ok = False
        log("  [FAIL] 未报误配错误（危险：Styrene 被当成乙苯接受了）")
    except Exception as exc:
        ok = "误配" in str(exc) and "106.17" in str(exc) \
            and "104.15" in str(exc)
        log("  报错（符合预期）：%s" % str(exc).splitlines()[-1][:120])
        log("  [%s]" % ("PASS" if ok else "FAIL（报错内容不含分子量证据）"))
    try:
        case.BasisManager.EndBasisChange()
    except Exception:
        pass
    case.Close(False)
    all_ok &= ok

    # [3] 用例3：乙苯脱氢全链路（LLM parser -> builder）
    log("")
    log("[3] 乙苯脱氢全链路（equilibrium 预期，转化率 >30%）")
    import parser as llm_parser
    spec = llm_parser.parse_user_input(
        "模拟乙苯脱氢制苯乙烯：C6H5C2H5 <-> C6H5C2H3 + H2，可逆反应，"
        "进料620℃、1.2 bar，无动力学数据，反应器出口650℃，求产物分布")
    log("  parser 选型：%s" % spec.get("reactor_type"))
    ok = spec.get("reactor_type") == "equilibrium"
    all_ok &= ok
    log("  [%s] 选型 equilibrium" % ("PASS" if ok else "FAIL"))
    result = builder.build_from_spec(spec)
    log("  status=%s converged=%s" % (result.get("status"),
                                        result.get("converged")))
    for w in result.get("warnings", []):
        log("  [warning] %s" % w)
    comp = (result.get("outlet") or {}).get("composition_molfrac") or {}
    for k in sorted(comp):
        log("    x[%-14s] = %.6f" % (k, comp[k]))
    convs = result.get("conversions") or {}
    conv_eb = next(iter(convs.values()), None)
    if conv_eb is None:
        conv_eb = -1.0  # builder 未给出转化率，判 FAIL
    ok = (result.get("converged") is True
          and (comp.get("Styrene") or 0) > 0.05
          and (comp.get("Hydrogen") or 0) > 0.05
          and conv_eb > 0.30)
    all_ok &= ok
    log("  乙苯转化率 = %.1f%%  [%s]（要求 >30%%，产物含 Styrene/H2）"
        % (conv_eb * 100, "PASS" if ok else "FAIL"))
    log("  case = %s" % result.get("case_file"))

    # [4] 回归：甲苯歧化 + SMR 710°C
    log("")
    log("[4] 回归：甲苯歧化（conversion）")
    spec2 = llm_parser.parse_user_input(
        "甲苯歧化制苯，进料10000 kg/h纯甲苯，380°C、2.5 MPa，"
        "甲苯单程转化率50%，产物苯与二甲苯（邻:间:对约1:2:1）")
    r2 = builder.build_from_spec(spec2)
    c2 = (r2.get("outlet") or {}).get("composition_molfrac") or {}
    for k in sorted(c2):
        log("    x[%-10s] = %.6f" % (k, c2[k]))
    ok = (r2.get("converged") is True
          and abs((c2.get("Toluene") or 0) - 0.5) < 0.02
          and abs((c2.get("Benzene") or 0) - 0.25) < 0.02)
    all_ok &= ok
    log("  converged=%s  [%s]（Toluene≈0.5, Benzene≈0.25）"
        % (r2.get("converged"), "PASS" if ok else "FAIL"))

    log("")
    log("[5] 回归：SMR 710°C（equilibrium）")
    spec3 = llm_parser.parse_user_input(
        "Simulate steam methane reforming with equilibrium reactions. "
        "Feed: methane 1000 kgmole/h and steam 2700 kgmole/h at 520 °C "
        "and 13.5 bar. Outlet temperature 710 °C.")
    r3 = builder.build_from_spec(spec3)
    c3 = (r3.get("outlet") or {}).get("composition_molfrac") or {}
    for k in sorted(c3):
        log("    x[%-10s] = %.6f" % (k, c3[k]))
    ok = (r3.get("converged") is True
          and abs((c3.get("Methane") or 0) - 0.0965) < 0.005
          and (r3.get("heat_duty") or 0) > 5)
    all_ok &= ok
    log("  converged=%s duty=%s MW  [%s]（CH4≈0.0965, duty>5MW）"
        % (r3.get("converged"), r3.get("heat_duty"),
           "PASS" if ok else "FAIL"))

    log("")
    log("总体：%s" % ("ALL PASS" if all_ok else "HAS FAIL"))
    with open(r"c:\work\hysys-ai-reactor\docs\component_resolver.txt",
              "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
