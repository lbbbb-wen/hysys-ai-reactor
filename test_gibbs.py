# -*- coding: utf-8 -*-
"""Gibbs 反应器路径实测（手工 spec，不走 LLM）。

三个用例：
  A: CH4 + 纯氧 1:2（化学计量），进料 25°C/1 bar，出口 800°C 等温
  B: CH4 + 空气（O2 为理论量 2 倍），进料 25°C/1 bar，绝热
  C: CH4 + 纯氧 1:1（欠氧），出口 1000°C 等温

验收物理判据见 docs/gibbs_test.txt 输出末尾。
"""
import json
import sys

import builder

# 空气组成（摩尔）：O2 21% / N2 79%
AIR_N2_PER_O2 = 79.0 / 21.0

CASES = {
    "A": {
        "desc": "甲烷+纯氧 1:2，等温 800°C",
        "spec": {
            "reactor_type": "gibbs", "selection_reason": "test A",
            "components": ["Methane", "Oxygen", "CO2", "H2O", "CO", "Hydrogen"],
            "reactions": [],
            "feed": {"T_C": 25.0, "P_bar": 1.0, "flow": 1000.0,
                     "flow_unit": "kgmole/h", "flow_basis": "total",
                     "composition": {"Methane": 1 / 3.0, "Oxygen": 2 / 3.0}},
            "outlet_spec": {"T_C": 800.0, "P_bar": None},
            "missing_info": [], "assumptions": [],
        },
    },
    "B": {
        "desc": "甲烷+空气（过量 100%，λ=2），绝热",
        "spec": {
            "reactor_type": "gibbs", "selection_reason": "test B",
            "components": ["Methane", "Oxygen", "Nitrogen", "CO2", "H2O",
                           "CO", "Hydrogen"],
            "reactions": [],
            "feed": {"T_C": 25.0, "P_bar": 1.0, "flow": 1000.0,
                     "flow_unit": "kgmole/h", "flow_basis": "total",
                     # CH4 : O2 : N2 = 1 : 4 : 4*79/21
                     "composition": {
                         "Methane": 1.0 / (1 + 4 + 4 * AIR_N2_PER_O2),
                         "Oxygen": 4.0 / (1 + 4 + 4 * AIR_N2_PER_O2),
                         "Nitrogen": 4 * AIR_N2_PER_O2
                         / (1 + 4 + 4 * AIR_N2_PER_O2)}},
            "outlet_spec": {"T_C": None, "P_bar": None},  # 绝热
            "missing_info": [], "assumptions": [],
        },
    },
    # 参照系：λ=1（化学计量空气）。1700-2100°C 的绝热火焰温度窗口
    # 对应的是这个工况；λ=2 时多余 2 倍 O2+N2 参与吸热，物理上
    # 正确值约 1200°C（热容近似翻倍，温升近似减半）。
    "B-stoich": {
        "desc": "参照：甲烷+化学计量空气（λ=1），绝热",
        "spec": {
            "reactor_type": "gibbs", "selection_reason": "test B reference",
            "components": ["Methane", "Oxygen", "Nitrogen", "CO2", "H2O",
                           "CO", "Hydrogen"],
            "reactions": [],
            "feed": {"T_C": 25.0, "P_bar": 1.0, "flow": 1000.0,
                     "flow_unit": "kgmole/h", "flow_basis": "total",
                     "composition": {
                         "Methane": 1.0 / (1 + 2 + 2 * AIR_N2_PER_O2),
                         "Oxygen": 2.0 / (1 + 2 + 2 * AIR_N2_PER_O2),
                         "Nitrogen": 2 * AIR_N2_PER_O2
                         / (1 + 2 + 2 * AIR_N2_PER_O2)}},
            "outlet_spec": {"T_C": None, "P_bar": None},
            "missing_info": [], "assumptions": [],
        },
    },
    "C": {
        "desc": "甲烷+纯氧 1:1（欠氧），等温 1000°C",
        "spec": {
            "reactor_type": "gibbs", "selection_reason": "test C",
            "components": ["Methane", "Oxygen", "CO2", "H2O", "CO", "Hydrogen"],
            "reactions": [],
            "feed": {"T_C": 25.0, "P_bar": 1.0, "flow": 1000.0,
                     "flow_unit": "kgmole/h", "flow_basis": "total",
                     "composition": {"Methane": 0.5, "Oxygen": 0.5}},
            "outlet_spec": {"T_C": 1000.0, "P_bar": None},
            "missing_info": [], "assumptions": [],
        },
    },
}

# 元素原子数（每摩尔组分）
ATOMS = {"Methane": {"C": 1, "H": 4}, "Oxygen": {"O": 2},
         "Nitrogen": {"N": 2}, "CO2": {"C": 1, "O": 2},
         "H2O": {"H": 2, "O": 1}, "CO": {"C": 1, "O": 1},
         "Hydrogen": {"H": 2}}


def element_moles(comp_map, total_flow):
    out = {"C": 0.0, "H": 0.0, "O": 0.0, "N": 0.0}
    for comp, x in comp_map.items():
        for el, n in ATOMS.get(comp, {}).items():
            out[el] += n * (x or 0.0) * total_flow
    return out


def run_case(key, case):
    spec = case["spec"]
    r = builder.build_from_spec(spec)
    out = r.get("outlet") or {}
    comp = out.get("composition_molfrac") or {}
    flow = r.get("total_molar_flow_kgmol_h") or 0.0

    lines = ["", "用例 %s：%s" % (key, case["desc"]),
             "  converged = %s" % r.get("converged"),
             "  出口 T = %s °C" % out.get("T_C"),
             "  出口 P = %s bar" % out.get("P_bar"),
             "  热负荷 = %s MW（负=放热）" % r.get("heat_duty"),
             "  case = %s" % r.get("case_file")]
    for w in r.get("warnings", []):
        lines.append("  [warning] %s" % w)
    for k in sorted(comp):
        lines.append("    x[%-9s] = %.6f" % (k, comp[k]))

    # 出口总摩尔流量（汽+液）需从结果外推——用组成×总流量验证元素守恒；
    # builder 返回的 total_molar_flow_kgmol_h 是进料总流量，出口总流量
    # 通过 case 文件无法直接拿到，这里用元素守恒比率即可（进出口各元素
    # 摩尔数之比应=1），出口流量用进料流量×元素比率反推。
    checks = []

    def chk(name, cond, detail):
        checks.append((name, bool(cond), detail))
        lines.append("  [%s] %s：%s" % ("PASS" if cond else "FAIL", name, detail))

    chk("收敛", r.get("converged") is True, "converged=%s" % r.get("converged"))

    if key == "A":
        chk("CH4 燃尽", (comp.get("Methane") or 0) < 1e-3,
            "x_CH4=%.2e" % (comp.get("Methane") or 0))
        chk("O2 耗尽", (comp.get("Oxygen") or 0) < 1e-3,
            "x_O2=%.2e" % (comp.get("Oxygen") or 0))
        s = (comp.get("CO2") or 0) + (comp.get("H2O") or 0)
        chk("CO2+H2O 为主产物", s > 0.99, "x_CO2+x_H2O=%.4f" % s)
    elif key == "B":
        t = out.get("T_C") or -999
        # 原判据 1700-2100°C 对应化学计量（λ=1）；λ=2 物理正确值约 1200°C，
        # 见 B-stoich 参照组。此处按 λ=2 的正确物理区间判定。
        chk("λ=2 绝热温度 1050-1400°C（热容翻倍的物理正确区间）",
            1050 <= t <= 1400, "T=%.1f" % t)
        chk("N2 为最大组分",
            (comp.get("Nitrogen") or 0) == max(comp.values()),
            "x_N2=%.4f" % (comp.get("Nitrogen") or 0))
        chk("过量 O2 有剩余", (comp.get("Oxygen") or 0) > 0.05,
            "x_O2=%.4f" % (comp.get("Oxygen") or 0))
    elif key == "B-stoich":
        t = out.get("T_C") or -999
        chk("λ=1 绝热火焰温度 1700-2100°C（原判据的适用工况）",
            1700 <= t <= 2100, "T=%.1f" % t)
        chk("高温离解出 CO", (comp.get("CO") or 0) > 1e-6,
            "x_CO=%.2e" % (comp.get("CO") or 0))
        chk("N2 为最大组分",
            (comp.get("Nitrogen") or 0) == max(comp.values()),
            "x_N2=%.4f" % (comp.get("Nitrogen") or 0))
    elif key == "C":
        chk("出现 CO", (comp.get("CO") or 0) > 1e-3,
            "x_CO=%.4f" % (comp.get("CO") or 0))
        chk("出现 H2", (comp.get("Hydrogen") or 0) > 1e-3,
            "x_H2=%.4f" % (comp.get("Hydrogen") or 0))
        chk("CH4 有残留（>0）", (comp.get("Methane") or 0) > 1e-8,
            "x_CH4=%.2e" % (comp.get("Methane") or 0))
        chk("O2 接近 0", (comp.get("Oxygen") or 0) < 1e-3,
            "x_O2=%.2e" % (comp.get("Oxygen") or 0))

    return r, lines, checks


def main():
    all_lines = ["Gibbs 反应器路径三用例实测", "=" * 60]
    all_ok = True
    results = {}
    for key, case in CASES.items():
        r, lines, checks = run_case(key, case)
        results[key] = r
        all_lines.extend(lines)
        all_ok = all_ok and all(c[1] for c in checks)

    # 用例 A 元素守恒（进出口）：C/H/O 各元素摩尔数偏差 < 0.5%
    specA = CASES["A"]["spec"]
    feed_comp = specA["feed"]["composition"]
    feed_flow = specA["feed"]["flow"]
    fin = element_moles(feed_comp, feed_flow)
    out_comp = results["A"]["outlet"]["composition_molfrac"]
    # 出口总流量未知 -> 用 C 元素守恒反推出口流量，再校核 H、O
    if out_comp:
        c_out_per_flow = sum(ATOMS[c].get("C", 0) * (x or 0.0)
                             for c, x in out_comp.items())
        if c_out_per_flow > 0:
            f_out = fin["C"] / c_out_per_flow
            fout = element_moles(out_comp, f_out)
            all_lines.append("")
            all_lines.append("用例 A 元素守恒（反推出口流量 %.2f kgmole/h）" % f_out)
            for el in ("C", "H", "O"):
                dev = abs(fout[el] - fin[el]) / fin[el] * 100 if fin[el] else 0
                ok = dev < 0.5
                all_ok = all_ok and ok
                all_lines.append("  [%s] 元素 %s：进 %.2f 出 %.2f 偏差 %.3f%%"
                                 % ("PASS" if ok else "FAIL", el,
                                    fin[el], fout[el], dev))

    all_lines.append("")
    all_lines.append("总体：%s" % ("ALL PASS" if all_ok else "HAS FAIL"))
    text = "\n".join(all_lines)
    print(text)
    with open(r"c:\work\hysys-ai-reactor\docs\gibbs_test.txt", "w",
              encoding="utf-8") as f:
        f.write(text + "\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
