# -*- coding: utf-8 -*-
"""
build_minimal.py —— 纯手工参数打通 HYSYS V15 反应器建模全链路（无 AI 决策）

步骤：
  1. 打开模板 templates/blank_pr.hsc（已含 Peng-Robinson），另存工作副本
  2. 在 Basis 变更模式内补加组分：H2O、CO、CO2、Hydrogen
  3. 进料 Feed：520 °C、13.5 bar、1000 kgmole/h，CH4:H2O = 1:2.7（摩尔）
  4. Conversion 反应：CH4 + H2O -> CO + 3 H2，基准组分 CH4，转化率 50%
  5. Conversion Reactor：连接进料、出口物流，关联 ReactionSet
  6. 求解并打印：收敛状态、出口温度、出口各组分摩尔分率
  7. case 另存为 cases/minimal_test.hsc（模板绝不覆盖）

V15 实测要点（写代码前已用 COM 探针验证）：
  - 禁止 SimulationCases.Add() 从零建 case（空白 basis 写不进物性包）；
    必须 Open() 已有的 Peng-Robinson 模板再另存。
  - 组分名使用 HYSYS 库名：Methane / H2O / CO / CO2 / Hydrogen。
  - 组分与物性包修改在 BasisManager.StartBasisChange()/EndBasisChange() 内完成。
  - 反应类型字符串为内部类型名 "conversionrxn"（不是 "ConversionReaction"）；
    反应器类型字符串为 "ConversionReactorOp"。
  - Conversion Reactor 固定有汽相/液相两个出口，两个都需连接才能求解；
    脚本同时打印两股出口，并给出按流量加权的"总体出口组成"。
  - 流股变量一律 SetValue(值, "单位") / GetValue("单位")；返回值 -32767 表示空值。
  - 反应必须先加入 ReactionSet，才能设置 BaseComponent；ReactionSet 必须先
    AssociateFluidPackage(fp)，才能赋给反应器的 ReactionSet 属性。
"""

import hashlib
import os
import sys
import time
import traceback

try:
    import win32com.client as win32
except ImportError:
    print("缺少 pywin32，请先执行：pip install -r requirements.txt")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates", "blank_pr.hsc")
CASES_DIR = os.path.join(HERE, "cases")
WORK_COPY = os.path.join(CASES_DIR, "minimal_work.hsc")
FINAL_CASE = os.path.join(CASES_DIR, "minimal_test.hsc")

COMPONENTS_TO_ADD = ("H2O", "CO", "CO2", "Hydrogen")
REACTION_NAME = "R-1"
REACTION_SET_NAME = "RS-1"
REACTOR_NAME = "Reactor-1"

EMPTY_VALUE = -32767.0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def com(label, fn):
    """执行一次 COM 调用；失败时打印完整 traceback 与排查建议后再抛出。"""
    try:
        return fn()
    except Exception:
        print("\n[失败] " + label)
        traceback.print_exc()
        print("排查建议：")
        print("  1) 确认 Aspen HYSYS V15 已正常启动且没有弹出模态对话框；")
        print("  2) 确认模板文件存在且未被其它程序占用：%s" % TEMPLATE)
        print("  3) 若模板已在 GUI 中打开，请先关闭该 case 再重试；")
        print("  4) 必要时结束 AspenHysys 进程后重新运行本脚本。")
        raise


def get_value(var, unit):
    """RealVariable.GetValue(单位)；空值 -32767 统一返回 None。"""
    try:
        v = var.GetValue(unit)
    except Exception:
        return None
    if v is None or abs(v - EMPTY_VALUE) < 1e-6:
        return None
    return v


def fmt(value, digits=6):
    if value is None:
        return "空值(-32767)"
    return ("%." + str(digits) + "f") % value


def read_fractions(stream):
    """读取流股全部组分摩尔分率；含空值则返回 None。"""
    try:
        fracs = tuple(stream.ComponentMolarFractionValue)
    except Exception:
        return None
    for x in fracs:
        if abs(x - EMPTY_VALUE) < 1e-6:
            return None
    return fracs


def print_fractions(label, names, fracs):
    print("  %s：" % label)
    if fracs is None:
        print("    <未计算/空值>")
        return
    for name, x in zip(names, fracs):
        print("    %-10s %.6f" % (name, x))


def connect_hysys():
    try:
        app = win32.GetActiveObject("HYSYS.Application")
        print("已连接到正在运行的 HYSYS 实例")
    except Exception:
        app = com("启动 HYSYS.Application", lambda: win32.Dispatch("HYSYS.Application"))
        print("已启动新的 HYSYS 实例")
    print("HYSYS 版本：%s" % com("读取 HYSYS 版本", lambda: app.Version))
    return app


def open_template(app):
    for path in (WORK_COPY, FINAL_CASE):
        if os.path.exists(path):
            try:
                os.remove(path)
                print("已删除旧的输出文件：%s" % path)
            except OSError as exc:
                raise RuntimeError(
                    "无法删除旧的输出文件 %s（可能仍被 HYSYS 打开）：%s" % (path, exc)
                )
    case = com("打开模板 %s" % TEMPLATE, lambda: app.SimulationCases.Open(TEMPLATE))
    print("模板已打开：%s" % com("读取 case 路径", lambda: case.FullName))
    com("另存工作副本 %s" % WORK_COPY, lambda: case.SaveAs(WORK_COPY))
    print("工作副本已保存：%s" % WORK_COPY)
    return case


def add_components(case):
    bm = case.BasisManager
    # HYSYS 打开 case 时的环境取决于应用当前状态：可能已在 Basis 变更模式，
    # 也可能在 Simulation 环境，此时需要显式进入 Basis 变更模式。
    if not com("读取 IsChangingBasis", lambda: bm.IsChangingBasis):
        com("进入 Basis 变更模式", lambda: bm.StartBasisChange())
        print("已进入 Basis 变更模式")
    else:
        print("case 已处于 Basis 变更模式")

    components = com("获取组分列表", lambda: bm.ComponentLists.Item(0).Components)
    existing = set(com("读取现有组分", lambda: components.Names))
    print("模板现有组分：%s" % ", ".join(com("读取现有组分", lambda: components.Names)))
    for name in COMPONENTS_TO_ADD:
        if name in existing:
            print("组分 %s 已存在，跳过" % name)
            continue
        try:
            components.Add(name)
            print("已添加组分：%s" % name)
        except Exception:
            print("\n[失败] 添加组分 %r 时 HYSYS 报错" % name)
            traceback.print_exc()
            print("请确认该名称是 HYSYS 组分库中的合法库名（例如 H2O 而不是 Water）。")
            raise

    names = tuple(com("读取组分", lambda: components.Names))
    print("当前组分（Count=%d）：%s" % (len(names), ", ".join(names)))
    if "Methane" not in names:
        raise RuntimeError("模板中缺少 Methane，无法定义以 CH4 为基准的转化反应")
    for name in COMPONENTS_TO_ADD:
        if name not in names:
            raise RuntimeError("组分 %s 添加后仍未出现在组分列表中" % name)

    if com("读取 CanEndBasisChange", lambda: bm.CanEndBasisChange):
        com("提交 Basis 变更", lambda: bm.EndBasisChange())
        print("Basis 变更已提交（离开 Basis 环境）")
    else:
        raise RuntimeError("CanEndBasisChange=False，物性包/组分信息不完整，无法进入模拟环境")
    return components, names


def build_reaction(bm, components):
    rpm = com("获取 ReactionPackageManager", lambda: bm.ReactionPackageManager)
    rxn = com(
        "创建 Conversion 反应",
        lambda: rpm.Reactions.Add(REACTION_NAME, "conversionrxn"),
    )
    print("已创建反应：%s（TypeName=%s）" % (rxn.name, rxn.TypeName))

    # 化学计量：反应物为负、产物为正（HYSYS 约定）
    for name in ("Methane", "H2O", "CO", "Hydrogen"):
        com("为反应添加组分 %s" % name, lambda name=name: rxn.Reactants.Add(name))
    com(
        "设置化学计量系数",
        lambda: setattr(rxn, "ReactantStoichCoefValue", (-1.0, -1.0, 1.0, 3.0)),
    )
    com("设置转化率 50%", lambda: setattr(rxn, "Conversion", 50.0))

    reaction_set = com(
        "创建 ReactionSet %s" % REACTION_SET_NAME,
        lambda: rpm.ReactionSets.Add(REACTION_SET_NAME),
    )
    com("把反应加入 ReactionSet", lambda: reaction_set.ActiveReactions.Add(rxn))

    # V15 实测：BaseComponent 必须在反应加入 ReactionSet 之后才能赋值
    ch4_index = tuple(components.Names).index("Methane")
    com(
        "设置基准组分 CH4",
        lambda: setattr(rxn, "BaseComponent", components.Item(ch4_index)),
    )
    # V15 实测：不关联物性包时，反应器无法接受该 ReactionSet
    fp = com("获取物性包", lambda: bm.FluidPackages.Item(0))
    com("反应集关联物性包", lambda: reaction_set.AssociateFluidPackage(fp))

    print("反应定义回读：")
    print("  组分序列 ：%s" % (tuple(rxn.ReactantName.Values),))
    print("  化学计量 ：%s" % (tuple(rxn.ReactantStoichCoefValue),))
    print("  基准组分 ：%s" % rxn.BaseComponent.name)
    print("  转化率   ：%s %%" % rxn.Conversion)
    print("  ReactionSet 中激活的反应：%s" % (tuple(reaction_set.ActiveReactions.Names),))
    return rxn, reaction_set


def build_feed(flowsheet, names):
    feed = com("创建进料流股 Feed", lambda: flowsheet.MaterialStreams.Add("Feed"))
    com("设置进料温度 520 °C", lambda: feed.Temperature.SetValue(520.0, "C"))
    com("设置进料压力 13.5 bar", lambda: feed.Pressure.SetValue(13.5, "bar"))

    idx = {name: i for i, name in enumerate(names)}
    x = [0.0] * len(names)
    x[idx["Methane"]] = 1.0 / 3.7
    x[idx["H2O"]] = 2.7 / 3.7
    com(
        "设置进料组成 CH4:H2O=1:2.7",
        lambda: setattr(feed, "ComponentMolarFractionValue", tuple(x)),
    )
    com(
        "设置进料总摩尔流量 1000 kgmole/h",
        lambda: feed.MolarFlow.SetValue(1000.0, "kgmole/h"),
    )

    print("进料回读：")
    print("  温度 = %s °C" % fmt(get_value(feed.Temperature, "C")))
    print("  压力 = %s bar" % fmt(get_value(feed.Pressure, "bar")))
    print("  摩尔流量 = %s kgmole/h" % fmt(get_value(feed.MolarFlow, "kgmole/h")))
    print_fractions("摩尔分率", names, read_fractions(feed))
    return feed


def build_reactor(flowsheet, feed, reaction_set):
    reactor = com(
        "创建 Conversion Reactor %s" % REACTOR_NAME,
        lambda: flowsheet.Operations.Add(REACTOR_NAME, "ConversionReactorOp"),
    )
    vapour = com(
        "创建汽相出口流股 Outlet-Vapour",
        lambda: flowsheet.MaterialStreams.Add("Outlet-Vapour"),
    )
    liquid = com(
        "创建液相出口流股 Outlet-Liquid",
        lambda: flowsheet.MaterialStreams.Add("Outlet-Liquid"),
    )

    com("连接进料", lambda: reactor.Feeds.Add(feed))
    com("连接汽相出口", lambda: setattr(reactor, "VapourProduct", vapour))
    com("连接液相出口", lambda: setattr(reactor, "LiquidProduct", liquid))
    com("关联 ReactionSet", lambda: setattr(reactor, "ReactionSet", reaction_set))

    print("反应器连接回读：")
    print("  进料   ：%s" % (tuple(reactor.AttachedFeeds.Names),))
    print("  出口   ：%s" % (tuple(reactor.AttachedProducts.Names),))
    print("  反应集 ：%s" % reactor.ReactionSet.name)
    return reactor, vapour, liquid


def solve(case, streams=None, timeout=60.0):
    """显式触发求解并轮询直到 Solver.IsSolving == False。

    注意：HYSYS V15 类型库中的 Solver 对象没有 Solve() 方法
    （实测 GetIDsOfNames('Solve') 返回 Unknown name）。这里优先尝试
    Solver.Solve() 以兼容其它版本；不存在时退化为 CanSolve=False->True
    强制求解器做一次完整重算。
    streams 形参仅为向后兼容保留，收敛判据以 Solver.IsSolving 为准。
    """
    solver = com("获取 Solver", lambda: case.Solver)
    if hasattr(solver, "Solve"):
        com("Solver.Solve()", solver.Solve)
        print("已显式调用 Solver.Solve()")
    else:
        com("Solver.CanSolve=False", lambda: setattr(solver, "CanSolve", False))
        com("Solver.CanSolve=True（触发完整重算）",
            lambda: setattr(solver, "CanSolve", True))
        print("V15 类型库无 Solver.Solve() 方法；已用 CanSolve 复位强制完整重算")

    time.sleep(0.5)  # 给异步求解线程一个启动窗口，避免误读 IsSolving
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not com("读取 Solver.IsSolving", lambda: solver.IsSolving):
            return True
        time.sleep(0.2)
    return False


def molar_flow_of(stream, fraction):
    flow = get_value(stream.MolarFlow, "kgmole/h")
    if flow is None or fraction is None:
        return None
    return flow * fraction


def main():
    print("=" * 70)
    print("HYSYS 最小反应器建模链路（手工参数）")
    print("=" * 70)

    if not os.path.isfile(TEMPLATE):
        raise RuntimeError("模板不存在：%s" % TEMPLATE)
    os.makedirs(CASES_DIR, exist_ok=True)
    template_hash_before = sha256(TEMPLATE)

    app = connect_hysys()
    case = open_template(app)
    components, names = add_components(case)
    flowsheet = com("获取 Flowsheet", lambda: case.Flowsheet)
    feed = build_feed(flowsheet, names)
    rxn, reaction_set = build_reaction(case.BasisManager, components)
    reactor, vapour, liquid = build_reactor(flowsheet, feed, reaction_set)

    print("\n[6] 求解")
    converged = solve(case)
    solver = com("获取 Solver", lambda: case.Solver)
    print("最终求解状态：CanSolve=%s, IsSolving=%s, IsForgetting=%s"
          % (solver.CanSolve, solver.IsSolving, solver.IsForgetting))
    if not converged:
        raise RuntimeError("HYSYS 求解超时（IsSolving 未归零），请检查反应器连接与反应定义")

    # 关键：求解前持有的流股 COM 引用在求解后不保证刷新（可能读到旧值），
    # 必须按名称从 flowsheet.MaterialStreams 重新获取对象再读数。
    print("求解完成，按名称重新获取流股/反应器对象（丢弃求解前的引用）")
    feed = com("重新获取 Feed",
               lambda: flowsheet.MaterialStreams.Item("Feed"))
    vapour = com("重新获取 Outlet-Vapour",
                 lambda: flowsheet.MaterialStreams.Item("Outlet-Vapour"))
    liquid = com("重新获取 Outlet-Liquid",
                 lambda: flowsheet.MaterialStreams.Item("Outlet-Liquid"))
    reactor = com("重新获取反应器 %s" % REACTOR_NAME,
                  lambda: flowsheet.Operations.Item(REACTOR_NAME))

    print("\n反应器求解结果（重新获取的反应器对象）：")
    print("  RxnPercentConversionValue  = %s %%"
          % (tuple(reactor.RxnPercentConversionValue),))
    print("  ComponentTotalReactedValue = %s kgmole/s"
          % (tuple(reactor.ComponentTotalReactedValue),))

    print("\n出口物流：")
    for stream in (vapour, liquid):
        print("  [%s]" % stream.name)
        print("    温度 = %s °C" % fmt(get_value(stream.Temperature, "C")))
        print("    压力 = %s bar" % fmt(get_value(stream.Pressure, "bar")))
        print("    摩尔流量 = %s kgmole/h" % fmt(get_value(stream.MolarFlow, "kgmole/h")))
        print_fractions("摩尔分率", names, read_fractions(stream))

    # 总体出口组成 = 两股出口按摩尔流量加权
    total_flow = 0.0
    component_flow = [0.0] * len(names)
    for stream in (vapour, liquid):
        fracs = read_fractions(stream)
        flow = get_value(stream.MolarFlow, "kgmole/h")
        if fracs is None or flow is None:
            raise RuntimeError("出口流股 %s 数据不完整，无法汇总" % stream.name)
        total_flow += flow
        for i, frac in enumerate(fracs):
            component_flow[i] += flow * frac

    overall = [c / total_flow for c in component_flow]
    print("\n总体出口（汽相 + 液相，按摩尔流量加权）：")
    print("  总摩尔流量 = %.6f kgmole/h" % total_flow)
    print_fractions("总体摩尔分率", names, overall)

    feed_fracs = read_fractions(feed)
    feed_flow = get_value(feed.MolarFlow, "kgmole/h")
    ch4_index = names.index("Methane")
    h2_index = names.index("Hydrogen")
    feed_ch4 = feed_flow * feed_fracs[ch4_index]
    out_ch4 = component_flow[ch4_index]
    actual_conversion = (feed_ch4 - out_ch4) / feed_ch4 * 100.0
    print("\nCH4 实际转化率 = %.4f %%  （进料 %.6f -> 出口 %.6f kgmole/h）"
          % (actual_conversion, feed_ch4, out_ch4))

    # 进出口质量守恒
    feed_mass = get_value(feed.MassFlow, "kg/h")
    out_mass = (get_value(vapour.MassFlow, "kg/h")
                + get_value(liquid.MassFlow, "kg/h"))
    mass_dev_pct = abs(feed_mass - out_mass) / feed_mass * 100.0
    print("质量守恒：进料 %.4f kg/h，出口合计 %.4f kg/h，偏差 %.4f %%"
          % (feed_mass, out_mass, mass_dev_pct))
    outlet_temp = get_value(vapour.Temperature, "C")

    print("\n[7] 保存")
    com("另存最终 case %s" % FINAL_CASE, lambda: case.SaveAs(FINAL_CASE))
    print("最终 case 已保存：%s" % FINAL_CASE)
    com("关闭 case（模板不保存）", lambda: case.Close(False))
    print("case 已关闭（工作副本/最终文件均已落盘，模板未修改）")

    template_hash_after = sha256(TEMPLATE)
    if template_hash_before == template_hash_after:
        print("模板校验：SHA-256 未变化，模板未被覆盖")
    else:
        print("警告：模板 SHA-256 发生变化，请检查是否被其它操作改写")

    print("\n[8] 结果硬断言")
    checks = [
        ("CH4 转化率 = 50.0 ± 0.5 %",
         actual_conversion, 50.0, 0.5, "%"),
        ("出口总体 CH4 摩尔分率 = 0.1064 ± 0.005",
         overall[ch4_index], 0.1064, 0.005, ""),
        ("出口总体 H2 摩尔分率 = 0.3191 ± 0.010",
         overall[h2_index], 0.3191, 0.010, ""),
        ("出口总摩尔流量 = 1270.27 ± 1.0 kgmole/h",
         total_flow, 1270.27, 1.0, "kgmole/h"),
        ("出口温度 = 140 ± 10 °C",
         outlet_temp, 140.0, 10.0, "°C"),
    ]
    failed = []
    for desc, actual, expect, tol, unit in checks:
        ok = actual is not None and abs(actual - expect) <= tol
        print("  [%s] %s （实测 %.6f %s）"
              % ("PASS" if ok else "FAIL", desc,
                 actual if actual is not None else float("nan"), unit))
        if not ok:
            failed.append(desc)

    mass_ok = mass_dev_pct < 0.5
    print("  [%s] 进出口质量守恒偏差 < 0.5%% （实测 %.4f %%）"
          % ("PASS" if mass_ok else "FAIL", mass_dev_pct))
    if not mass_ok:
        failed.append("进出口质量守恒偏差 < 0.5%")

    if failed:
        print("\nFAIL：以下断言未通过：")
        for desc in failed:
            print("  - %s" % desc)
        sys.exit(1)

    print("\n全部断言通过：PASS")
    print("完成。可在 HYSYS GUI 中打开：%s" % FINAL_CASE)


if __name__ == "__main__":
    main()
