# -*- coding: utf-8 -*-
"""
diagnose_reactor.py —— 诊断 build_minimal.py 建模后反应器/反应集的真实状态。

只做诊断，不修改任何建模逻辑：复用 build_minimal.py 中的建模函数
（connect_hysys / add_components / build_feed / build_reaction /
build_reactor / solve），在模板上原地建模、打印诊断事实，最后关闭 case 不保存。

打印内容：
  1. 反应器 Name / TypeName / VisibleTypeName（确认是不是 Conversion Reactor）
  2. 反应器全部连接：ConnectionType + StreamName
  3. 反应器实际绑定的 ReactionSet 名称（从对象读回，非赋值时用的名字）
  4. ReactionSet 中各反应的激活状态（ActiveReactions / InactiveReactions）
  5. 反应 R-1 的相态 ReactionPhase 与基准组分 BaseComponent
  6. ReactionSet 中实际参与计算的反应列表（含相关成员枚举）
"""

import os
import sys

# 复用 build_minimal.py 的建模函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_minimal as bm  # noqa: E402

# HYSYS 反应相态枚举（HYSYS 类型库未对该 Long 属性给出具名枚举，
# 此处依据 HYSYS V15 实测与 PFRPhaseType_enum 同源约定给出映射）：
REACTION_PHASE_MAP = {
    0: "VapourOnly",
    1: "LiquidOnly",
    2: "Aqueous",
    3: "Solid",
    4: "VapourLiquid",
    5: "Overall (All Phases)",
}


def safe(label, fn):
    try:
        return fn()
    except Exception as e:
        s = type(e).__name__
        info = getattr(e, "excepinfo", None)
        if info and len(info) > 5 and info[5]:
            s += " inner=0x%08X" % (info[5] & 0xFFFFFFFF)
        return "ERROR: %s (%s)" % (s, e.args)


def stream_name(obj):
    if obj is None:
        return "<None>"
    return safe("name", lambda: obj.name)


def main():
    print("=" * 72)
    print("diagnose_reactor.py — 反应器/反应集真实状态诊断")
    print("=" * 72)

    if not os.path.isfile(bm.TEMPLATE):
        sys.exit("模板不存在：%s" % bm.TEMPLATE)

    app = bm.connect_hysys()
    # 诊断：直接打开模板在内存中建模，不另存、不保存
    case = safe("打开模板", lambda: app.SimulationCases.Open(bm.TEMPLATE))
    if isinstance(case, str) and case.startswith("ERROR"):
        sys.exit("打开模板失败：%s" % case)

    print("\n复用 build_minimal.py 建模步骤（不保存）...")
    components, names = bm.add_components(case)
    flowsheet = bm.com("获取 Flowsheet", lambda: case.Flowsheet)
    feed = bm.build_feed(flowsheet, names)
    rxn, reaction_set = bm.build_reaction(case.BasisManager, components)
    reactor, vapour, liquid = bm.build_reactor(flowsheet, feed, reaction_set)
    converged = bm.solve(case, (vapour, liquid))

    print("\n" + "=" * 72)
    print("[1] 反应器类型确认")
    print("=" * 72)
    print("  Name           : %s" % safe("name", lambda: reactor.name))
    print("  TypeName       : %s" % safe("TypeName", lambda: reactor.TypeName))
    print("  VisibleTypeName: %s" % safe("VisibleTypeName", lambda: reactor.VisibleTypeName))
    is_conv = safe("TypeName", lambda: reactor.TypeName) == "conversionreactorop"
    print("  => 是否 Conversion Reactor: %s" % ("是" if is_conv else "否"))

    print("\n" + "=" * 72)
    print("[2] 反应器全部连接（ConnectionType + StreamName）")
    print("=" * 72)
    print("  说明：HYSYS ConversionReactor 没有统一的 Connections 集合，")
    print("  连接通过 Feeds / AttachedFeeds / AttachedProducts 集合以及")
    print("  VapourProduct / LiquidProduct 两个属性暴露。")

    # Feeds 集合（可添加流股的附件集合）
    feeds = safe("Feeds", lambda: reactor.Feeds)
    if not isinstance(feeds, str):
        for i in range(safe("Feeds.Count", lambda: feeds.Count)):
            item = feeds.Item(i)
            print("  [Feeds]              StreamName = %s" % stream_name(item))

    # AttachedFeeds / AttachedProducts（固定附件）
    af = safe("AttachedFeeds", lambda: reactor.AttachedFeeds)
    if not isinstance(af, str):
        for i in range(safe("AttachedFeeds.Count", lambda: af.Count)):
            item = af.Item(i)
            print("  [AttachedFeeds]      StreamName = %s" % stream_name(item))

    ap = safe("AttachedProducts", lambda: reactor.AttachedProducts)
    if not isinstance(ap, str):
        for i in range(safe("AttachedProducts.Count", lambda: ap.Count)):
            item = ap.Item(i)
            print("  [AttachedProducts]   StreamName = %s" % stream_name(item))

    # 直接的产物连接属性
    vp = safe("VapourProduct", lambda: reactor.VapourProduct)
    print("  [VapourProduct]      StreamName = %s" % stream_name(vp))
    lp = safe("LiquidProduct", lambda: reactor.LiquidProduct)
    print("  [LiquidProduct]      StreamName = %s" % stream_name(lp))

    feeds_count = safe("Feeds.Count", lambda: reactor.Feeds.Count)
    prod_count = safe("AttachedProducts.Count", lambda: reactor.AttachedProducts.Count)
    print("  => 进料连接数 = %s，产物连接数 = %s" % (feeds_count, prod_count))

    print("\n" + "=" * 72)
    print("[3] 反应器实际绑定的 ReactionSet（从对象读回）")
    print("=" * 72)
    bound = safe("ReactionSet", lambda: reactor.ReactionSet)
    if isinstance(bound, str) and bound.startswith("ERROR"):
        print("  读取 ReactionSet 失败: %s" % bound)
    else:
        print("  绑定 ReactionSet.name = %s" % safe("name", lambda: bound.name))
        print("  绑定 ReactionSet.TypeName = %s" % safe("TypeName", lambda: bound.TypeName))
        rs_match = (safe("name", lambda: bound.name) == bm.REACTION_SET_NAME)
        print("  => 是否为赋值时使用的 '%s' : %s" % (bm.REACTION_SET_NAME,
                                                      "是" if rs_match else "否"))

    print("\n" + "=" * 72)
    print("[4] ReactionSet 中各反应的激活状态")
    print("=" * 72)
    active_n = safe("ActiveReactions.Count", lambda: reaction_set.ActiveReactions.Count)
    inactive_n = safe("InactiveReactions.Count", lambda: reaction_set.InactiveReactions.Count)
    print("  ActiveReactions.Count   = %s" % active_n)
    print("  InactiveReactions.Count = %s" % inactive_n)
    active_names = safe("ActiveReactions.Names", lambda: tuple(reaction_set.ActiveReactions.Names))
    inactive_names = safe("InactiveReactions.Names",
                          lambda: tuple(reaction_set.InactiveReactions.Names))
    print("  ActiveReactions.Names   = %s" % (active_names,))
    print("  InactiveReactions.Names = %s" % (inactive_names,))
    # 逐个打印反应对象，确认是否存在 IsActive / Active 属性
    for label, col in (("ActiveReactions", reaction_set.ActiveReactions),
                       ("InactiveReactions", reaction_set.InactiveReactions)):
        for i in range(safe("%s.Count" % label, lambda c=col: c.Count)):
            r = col.Item(i)
            print("  [%s][%d] name=%s  TypeName=%s" % (
                label, i,
                safe("name", lambda: r.name),
                safe("TypeName", lambda: r.TypeName)))
            for attr in ("IsActive", "Active"):
                v = safe(attr, lambda a=attr, o=r: getattr(o, a))
                print("        %s = %s" % (attr, v))
    print("  说明：HYSYS 反应对象本身没有 IsActive/Active 属性；")
    print("  激活状态由其位于 ActiveReactions 还是 InactiveReactions 决定。")

    print("\n" + "=" * 72)
    print("[5] 反应 R-1 的相态设置与基准组分")
    print("=" * 72)
    print("  name           = %s" % safe("name", lambda: rxn.name))
    print("  TypeName       = %s" % safe("TypeName", lambda: rxn.TypeName))
    bc = safe("BaseComponent", lambda: rxn.BaseComponent)
    print("  BaseComponent  = %s" % (safe("name", lambda: bc.name)
                                if not (isinstance(bc, str) and bc.startswith("ERROR")) else bc))
    phase = safe("ReactionPhase", lambda: rxn.ReactionPhase)
    phase_label = REACTION_PHASE_MAP.get(phase, "未知值")
    print("  ReactionPhase  = %s  => %s" % (phase, phase_label))
    print("  Conversion     = %s" % safe("Conversion", lambda: rxn.Conversion))
    print("  ReactantName   = %s" % (safe("ReactantName.Values",
                                        lambda: tuple(rxn.ReactantName.Values)),))
    print("  StoichCoefValue= %s" % (safe("ReactantStoichCoefValue",
                                        lambda: tuple(rxn.ReactantStoichCoefValue)),))

    print("\n" + "=" * 72)
    print("[6] ReactionSet 中实际参与计算的反应列表")
    print("=" * 72)
    print("  ReactionSet.name = %s" % safe("name", lambda: reaction_set.name))
    print("  参与计算的反应（ActiveReactions）:")
    an = safe("ActiveReactions.Names", lambda: tuple(reaction_set.ActiveReactions.Names))
    if isinstance(an, tuple):
        for n in an:
            print("    - %s" % n)
    else:
        print("    %s" % an)
    # 打印 ReactionSet 上与反应/计算相关的成员名，便于判断是否有其它过滤属性
    rs_members = safe("dir(reaction_set)", lambda: [m for m in dir(reaction_set)
                                                     if not m.startswith("_")])
    relevant = [m for m in rs_members if any(k in m for k in
                ("React", "Active", "Calc", "Solv", "Phase"))]
    print("  ReactionSet 相关成员（dir 过滤）: %s" % relevant)

    print("\n" + "=" * 72)
    print("[附加] 求解后反应器的实际转化结果")
    print("=" * 72)
    print("  求解收敛: %s" % ("是" if converged else "否"))
    print("  RxnPercentConversionValue = %s" % (
        safe("RxnPercentConversionValue",
             lambda: tuple(reactor.RxnPercentConversionValue)),))
    print("  ConversionValue           = %s" % (
        safe("ConversionValue", lambda: tuple(reactor.ConversionValue)),))
    print("  ComponentTotalReactedValue= %s" % (
        safe("ComponentTotalReactedValue",
             lambda: tuple(reactor.ComponentTotalReactedValue)),))
    print("  汽相出口温度 = %s °C" % bm.fmt(bm.get_value(vapour.Temperature, "C")))
    print("  液相出口温度 = %s °C" % bm.fmt(bm.get_value(liquid.Temperature, "C")))

    print("\n" + "=" * 72)
    print("诊断完成，关闭 case（不保存模板）")
    print("=" * 72)
    bm.com("关闭 case", lambda: case.Close(False))


if __name__ == "__main__":
    main()
