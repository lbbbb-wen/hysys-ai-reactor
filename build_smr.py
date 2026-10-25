# -*- coding: utf-8 -*-
"""
build_smr.py —— 甲烷蒸汽重整（SMR）Equilibrium Reactor 两工况模拟（手工参数）

场景：
  物性包 Peng-Robinson；组分 Methane / H2O / CO / CO2 / Hydrogen。
  进料：520 °C、13.5 bar；CH4 1000 kgmole/h : H2O 2700 kgmole/h（1:2.7），
        总摩尔流量 3700 kgmole/h。
        年处理量量级：CH4 1000 kgmole/h × 16.04 kg/kmol ≈ 16.0 t/h，
        按 8000 h/年 ≈ 12.8 万吨/年甲烷原料，对应中型天然气制氢 /
        合成氨装置的量级。
  反应（Equilibrium，ReactionPhase=Overall）：
        R1: CH4 + H2O = CO + 3 H2   （蒸汽重整，强吸热）
        R2: CO + H2O = CO2 + H2     （水汽变换，放热）
  反应器：Equilibrium Reactor，连接进料、汽/液两个产物出口（HYSYS 固有
        结构）与能量流股 Q-SMR；压降 0（出口 13.5 bar）；出口温度分别为
        710 °C 与 600 °C 两个工况。

Keq 来源（运行时先探测、再决定，见 build_reactions 的打印输出）：
  实测结论（HYSYS V15）：LnKSource 写 0（eqrxn_Gibbs）后读回恒为 2
  （eqrxn_FixedK），即 COM 层面 Gibbs 自动计算 Keq 不可用，因此回退到
  文献关联式 ln Keq = A + B/T + C·lnT + D·T（HYSYS 内建形式）。
  关联式来源：Rostrup-Nielsen, J. R., "Catalytic Steam Reforming",
  Springer, 1984（催化重整手册经典关联）：
      SMR: log10 Kp = -11650/T + 13.076  -> ln K = 30.113 - 26827/T
      WGS: log10 Kp =   2073/T -  2.029  -> ln K = -4.6716 + 4773.9/T
  基准压力一致性：该关联式以各组分分压（bar）表示 Kp；HYSYS 侧将
  Reaction.Basis 设为 rbPartialPressBasis(2) 且 BasisUnits='bar'
  （已实测写 BasisUnits2='bar' 后读回一致），因此关联式基准与 HYSYS
  要求一致。探针校核：983.15 K 时 HYSYS 出口组成的反应商
  Q1 = 16.88 bar² 与关联式 K1 = exp(2.826) = 16.87 bar² 吻合，
  Q2 = 1.2022 与 K2 = exp(0.18412) = 1.2022 吻合。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_minimal as bm  # 复用已验证的连接/求解/读数基建

CASE_710 = os.path.join(bm.CASES_DIR, "smr_710.hsc")
CASE_600 = os.path.join(bm.CASES_DIR, "smr_600.hsc")

FEED_NAME = "SMR-Feed"
VAP_NAME = "SMR-Vapour"
LIQ_NAME = "SMR-Liquid"
ENERGY_NAME = "Q-SMR"
REACTOR_NAME = "EQR-1"
REACTION_SET_NAME = "RS-SMR"

FEED_T_C = 520.0
FEED_P_BAR = 13.5
FEED_TOTAL_KGMOL_H = 3700.0  # CH4 1000 + H2O 2700
CASE_TEMPS = ((710.0, CASE_710), (600.0, CASE_600))

# Rostrup-Nielsen (1984) 关联式系数（HYSYS 形式 ln K = A + B/T + C·lnT + D·T，
# T 单位 K；基准：分压 bar）
RXN_SPECS = (
    {
        "name": "R1-SMR",
        "comps": ("Methane", "H2O", "CO", "Hydrogen"),
        "stoich": (-1.0, -1.0, 1.0, 3.0),
        "A": 30.113, "B": -26827.0, "C": 0.0, "D": 0.0,
    },
    {
        "name": "R2-WGS",
        "comps": ("CO", "H2O", "CO2", "Hydrogen"),
        "stoich": (-1.0, -1.0, 1.0, 1.0),
        "A": -4.6716, "B": 4773.9, "C": 0.0, "D": 0.0,
    },
)


def build_feed(flowsheet, names):
    feed = bm.com("创建进料流股 %s" % FEED_NAME,
                  lambda: flowsheet.MaterialStreams.Add(FEED_NAME))
    bm.com("进料温度 520 °C", lambda: feed.Temperature.SetValue(FEED_T_C, "C"))
    bm.com("进料压力 13.5 bar", lambda: feed.Pressure.SetValue(FEED_P_BAR, "bar"))
    idx = {n: i for i, n in enumerate(names)}
    x = [0.0] * len(names)
    x[idx["Methane"]] = 1.0 / 3.7
    x[idx["H2O"]] = 2.7 / 3.7
    bm.com("进料组成 CH4:H2O=1:2.7",
           lambda: setattr(feed, "ComponentMolarFractionValue", tuple(x)))
    bm.com("进料总摩尔流量 3700 kgmole/h",
           lambda: feed.MolarFlow.SetValue(FEED_TOTAL_KGMOL_H, "kgmole/h"))
    print("进料回读：T=%s °C, P=%s bar, F=%s kgmole/h"
          % (bm.fmt(bm.get_value(feed.Temperature, "C")),
             bm.fmt(bm.get_value(feed.Pressure, "bar")),
             bm.fmt(bm.get_value(feed.MolarFlow, "kgmole/h"))))
    bm.print_fractions("进料摩尔分率", names, bm.read_fractions(feed))
    return feed


def build_reactions(rpm):
    """创建两个 Equilibrium 反应；先探测 Gibbs，不支持则用文献关联式。"""
    rxns = []
    for spec in RXN_SPECS:
        rxn = bm.com("创建反应 %s" % spec["name"],
                     lambda s=spec: rpm.Reactions.Add(s["name"], "equilibriumrxn"))
        print("已创建反应：%s（TypeName=%s）" % (rxn.name, rxn.TypeName))
        for comp in spec["comps"]:
            bm.com("反应添加组分 %s" % comp,
                   lambda r=rxn, c=comp: r.Reactants.Add(c))
        bm.com("设置化学计量 %s" % spec["name"],
               lambda r=rxn, s=spec: setattr(r, "ReactantStoichCoefValue", s["stoich"]))
        bm.com("ReactionPhase=Overall(5)",
               lambda r=rxn: setattr(r, "ReactionPhase", 5))

        # --- Keq 来源探测 ---
        print("  [%s] Keq 来源探测：默认 LnKSource=%s, AutoDetect=%s"
              % (spec["name"],
                 bm.com("读 LnKSource", lambda r=rxn: r.LnKSource),
                 bm.com("读 AutoDetect", lambda r=rxn: r.AutoDetect)))
        bm.com("AutoDetect=False", lambda r=rxn: setattr(r, "AutoDetect", False))
        bm.com("尝试 LnKSource=0 (eqrxn_Gibbs)",
               lambda r=rxn: setattr(r, "LnKSource", 0))
        readback = bm.com("读回 LnKSource", lambda r=rxn: r.LnKSource)
        if readback == 0:
            print("  [%s] 探测结果：支持 Gibbs 自动计算 Keq，已采用 Gibbs 法"
                  % spec["name"])
        else:
            print("  [%s] 探测结果：写入 Gibbs 后读回 LnKSource=%d（≠0），"
                  "V15 COM 不支持 Gibbs 自动计算；回退到文献关联式"
                  % (spec["name"], readback))
            bm.com("LnKSource=1 (eqrxn_LnKEquation)",
                   lambda r=rxn: setattr(r, "LnKSource", 1))
            bm.com("关联式系数 A-D",
                   lambda r=rxn, s=spec: (
                       setattr(r, "LnKEquationAParameter", s["A"]),
                       setattr(r, "LnKEquationBParameter", s["B"]),
                       setattr(r, "LnKEquationCParameter", s["C"]),
                       setattr(r, "LnKEquationDParameter", s["D"])))
            # 关联式基准 = 分压（bar）；HYSYS 侧 Basis 设为 PartialPressure(bar)
            bm.com("Basis=rbPartialPressBasis(2)",
                   lambda r=rxn: setattr(r, "Basis", 2))
            bm.com("BasisUnits2='bar'",
                   lambda r=rxn: setattr(r, "BasisUnits2", "bar"))
            print("  [%s] 关联式 ln K = %.4f + %.1f/T（K），基准：分压 bar；"
                  "HYSYS 读回 Basis=%d, BasisUnits='%s'"
                  % (spec["name"], spec["A"], spec["B"],
                     bm.com("读 Basis", lambda r=rxn: r.Basis),
                     bm.com("读 BasisUnits", lambda r=rxn: r.BasisUnits)))
        rxns.append(rxn)

    rs = bm.com("创建 ReactionSet %s" % REACTION_SET_NAME,
                lambda: rpm.ReactionSets.Add(REACTION_SET_NAME))
    for rxn in rxns:
        bm.com("加入反应 %s" % rxn.name,
               lambda r=rxn: rs.ActiveReactions.Add(r))
    active = tuple(bm.com("读激活反应", lambda: rs.ActiveReactions.Names))
    inactive = tuple(bm.com("读未激活反应", lambda: rs.InactiveReactions.Names))
    print("ReactionSet %s：激活=%s，未激活=%s" % (REACTION_SET_NAME, active, inactive))
    if len(active) != len(RXN_SPECS) or inactive:
        raise RuntimeError("存在未激活的反应，请检查 ReactionSet")
    fp = bm.com("获取物性包", lambda: rpm.BasisManager.FluidPackages.Item(0))
    bm.com("反应集关联物性包", lambda: rs.AssociateFluidPackage(fp))
    return rxns, rs


def build_reactor(flowsheet, feed, reaction_set):
    reactor = bm.com("创建 Equilibrium Reactor %s" % REACTOR_NAME,
                     lambda: flowsheet.Operations.Add(REACTOR_NAME,
                                                      "EquilibriumReactorOp"))
    vapour = bm.com("创建汽相出口 %s" % VAP_NAME,
                    lambda: flowsheet.MaterialStreams.Add(VAP_NAME))
    liquid = bm.com("创建液相出口 %s" % LIQ_NAME,
                    lambda: flowsheet.MaterialStreams.Add(LIQ_NAME))
    energy = bm.com("创建能量流股 %s" % ENERGY_NAME,
                    lambda: flowsheet.EnergyStreams.Add(ENERGY_NAME))
    bm.com("连接进料", lambda: reactor.Feeds.Add(feed))
    # HYSYS 固有结构：汽/液两个产物出口必须同时连接
    bm.com("连接汽相出口", lambda: setattr(reactor, "VapourProduct", vapour))
    bm.com("连接液相出口", lambda: setattr(reactor, "LiquidProduct", liquid))
    bm.com("连接能量流股", lambda: setattr(reactor, "EnergyStream", energy))
    bm.com("关联 ReactionSet", lambda: setattr(reactor, "ReactionSet", reaction_set))
    bm.com("压降=0（出口 13.5 bar）", lambda: setattr(reactor, "PressureDropValue", 0.0))
    print("反应器连接回读：进料=%s，产物=%s，反应集=%s"
          % (tuple(reactor.AttachedFeeds.Names),
             tuple(reactor.AttachedProducts.Names),
             reactor.ReactionSet.name))
    return reactor, vapour, liquid, energy


def solve_and_read(case, flowsheet, names, target_t):
    """指定出口温度 -> 求解 -> 重新按名称取对象读数。"""
    vap_w = bm.com("按名称获取 %s 写温度" % VAP_NAME,
                   lambda: flowsheet.MaterialStreams.Item(VAP_NAME))
    bm.com("指定出口温度 %.0f °C" % target_t,
           lambda: vap_w.Temperature.SetValue(target_t, "C"))

    converged = bm.solve(case)
    solver = bm.com("获取 Solver", lambda: case.Solver)
    print("求解状态：CanSolve=%s, IsSolving=%s" % (solver.CanSolve, solver.IsSolving))
    if not converged:
        raise RuntimeError("求解超时（IsSolving 未归零）")

    # 求解后必须重新按名称获取对象（旧引用不刷新）
    feed = bm.com("重新获取 Feed",
                  lambda: flowsheet.MaterialStreams.Item(FEED_NAME))
    vapour = bm.com("重新获取 %s" % VAP_NAME,
                    lambda: flowsheet.MaterialStreams.Item(VAP_NAME))
    liquid = bm.com("重新获取 %s" % LIQ_NAME,
                    lambda: flowsheet.MaterialStreams.Item(LIQ_NAME))
    reactor = bm.com("重新获取 %s" % REACTOR_NAME,
                     lambda: flowsheet.Operations.Item(REACTOR_NAME))

    total_flow = 0.0
    component_flow = [0.0] * len(names)
    for stream in (vapour, liquid):
        fracs = bm.read_fractions(stream)
        flow = bm.get_value(stream.MolarFlow, "kgmole/h")
        if fracs is None or flow is None:
            raise RuntimeError("出口流股 %s 数据不完整" % stream.name)
        total_flow += flow
        for i, frac in enumerate(fracs):
            component_flow[i] += flow * frac
    overall = [c / total_flow for c in component_flow]
    outlet_t = bm.get_value(vapour.Temperature, "C")

    duty = reactor.HeatFlowValue  # 实测单位 kJ/s
    duty_mw = duty / 1000.0 if duty is not None else None

    feed_mass = bm.get_value(feed.MassFlow, "kg/h")
    out_mass = (bm.get_value(vapour.MassFlow, "kg/h")
                + bm.get_value(liquid.MassFlow, "kg/h"))
    mass_dev = abs(feed_mass - out_mass) / feed_mass * 100.0

    idx = {n: i for i, n in enumerate(names)}
    feed_fracs = bm.read_fractions(feed)
    feed_flow = bm.get_value(feed.MolarFlow, "kgmole/h")
    ch4_conv = ((feed_flow * feed_fracs[idx["Methane"]] - component_flow[idx["Methane"]])
                / (feed_flow * feed_fracs[idx["Methane"]]) * 100.0)

    return {
        "converged": converged,
        "outlet_t": outlet_t,
        "total_flow": total_flow,
        "overall": overall,
        "duty_mw": duty_mw,
        "mass_dev": mass_dev,
        "ch4_conv": ch4_conv,
    }


def main():
    print("=" * 72)
    print("HYSYS 甲烷蒸汽重整（SMR）Equilibrium Reactor 两工况模拟")
    print("=" * 72)

    if not os.path.isfile(bm.TEMPLATE):
        raise RuntimeError("模板不存在：%s" % bm.TEMPLATE)
    os.makedirs(bm.CASES_DIR, exist_ok=True)
    template_hash_before = bm.sha256(bm.TEMPLATE)
    for path in (CASE_710, CASE_600):
        if os.path.exists(path):
            os.remove(path)
            print("已删除旧的输出文件：%s" % path)

    app = bm.connect_hysys()
    case = bm.com("打开模板 %s" % bm.TEMPLATE,
                  lambda: app.SimulationCases.Open(bm.TEMPLATE))
    components, names = bm.add_components(case)
    flowsheet = bm.com("获取 Flowsheet", lambda: case.Flowsheet)
    feed = build_feed(flowsheet, names)
    rxns, reaction_set = build_reactions(case.BasisManager.ReactionPackageManager)
    reactor, vapour, liquid, energy = build_reactor(flowsheet, feed, reaction_set)

    results = {}
    for target_t, save_path in CASE_TEMPS:
        print("\n" + "=" * 72)
        print("工况：出口温度 %.0f °C" % target_t)
        print("=" * 72)
        r = solve_and_read(case, flowsheet, names, target_t)
        results[target_t] = r
        print("出口温度 = %.4f °C" % r["outlet_t"])
        print("出口总摩尔流量 = %.4f kgmole/h" % r["total_flow"])
        bm.print_fractions("出口总体摩尔分率", names, r["overall"])
        print("热负荷 = %.4f MW（能量流股 %s，正值=吸热）" % (r["duty_mw"], ENERGY_NAME))
        print("CH4 转化率 = %.4f %%" % r["ch4_conv"])
        print("质量守恒偏差 = %.4f %%" % r["mass_dev"])
        bm.com("另存 %s" % save_path, lambda p=save_path: case.SaveAs(p))
        print("case 已保存：%s" % save_path)

    bm.com("关闭 case（模板不保存）", lambda: case.Close(False))

    template_hash_after = bm.sha256(bm.TEMPLATE)
    if template_hash_before == template_hash_after:
        print("模板校验：SHA-256 未变化，模板未被覆盖")
    else:
        print("警告：模板 SHA-256 发生变化，请检查是否被其它操作改写")

    print("\n[断言] 结果校验")
    r710 = results[710.0]
    r600 = results[600.0]
    idx = {n: i for i, n in enumerate(names)}
    failed = []

    def check(desc, ok, detail):
        print("  [%s] %s（%s）" % ("PASS" if ok else "FAIL", desc, detail))
        if not ok:
            failed.append(desc)

    # a. 两工况均收敛，出口温度偏差 < 1 °C
    for t, r in ((710.0, r710), (600.0, r600)):
        check("a. 出口 %.0f °C 工况收敛且温度偏差 < 1 °C" % t,
              r["converged"] and r["outlet_t"] is not None
              and abs(r["outlet_t"] - t) < 1.0,
              "实测 %.4f °C" % (r["outlet_t"] if r["outlet_t"] is not None
                                else float("nan")))
    # b. 组成合计 = 1.000 ± 0.001
    for t, r in ((710.0, r710), (600.0, r600)):
        s = sum(r["overall"])
        check("b. 出口 %.0f °C 组成合计 = 1.000 ± 0.001" % t,
              abs(s - 1.0) <= 0.001, "实测 %.6f" % s)
    # c. 710 °C CH4 摩尔分率 < 600 °C
    check("c. CH4 摩尔分率：710 °C < 600 °C",
          r710["overall"][idx["Methane"]] < r600["overall"][idx["Methane"]],
          "710 °C: %.6f, 600 °C: %.6f"
          % (r710["overall"][idx["Methane"]], r600["overall"][idx["Methane"]]))
    # d. 710 °C CO 摩尔分率 > 600 °C（WGS 放热，低温偏向 CO2）
    check("d. CO 摩尔分率：710 °C > 600 °C",
          r710["overall"][idx["CO"]] > r600["overall"][idx["CO"]],
          "710 °C: %.6f, 600 °C: %.6f"
          % (r710["overall"][idx["CO"]], r600["overall"][idx["CO"]]))
    # e. 质量守恒偏差 < 0.5%
    for t, r in ((710.0, r710), (600.0, r600)):
        check("e. 出口 %.0f °C 质量守恒偏差 < 0.5%%" % t,
              r["mass_dev"] < 0.5, "实测 %.4f %%" % r["mass_dev"])

    if failed:
        print("\nFAIL：以下断言未通过：")
        for desc in failed:
            print("  - %s" % desc)
        sys.exit(1)

    print("\n全部断言通过：PASS")
    print("完成。可在 HYSYS GUI 中打开：%s 与 %s" % (CASE_710, CASE_600))


if __name__ == "__main__":
    main()
