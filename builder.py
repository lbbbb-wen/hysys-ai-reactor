# -*- coding: utf-8 -*-
"""
builder.py —— 通用 HYSYS 反应器构建器。

输入 parser.py 产出的结构化 spec，输出建模结果 dict。
只通过 hysys/core.py 与 hysys/units.py 的公开函数操作 HYSYS，
本文件不含任何具体物质名或场景判断分支。

Keq 说明（equilibrium 反应）：
  优先尝试 HYSYS 原生 Gibbs 源（LnKSource=0）。V15 COM 实测写入后被
  强制读回（不可用），此时回退到「HYSYS 内置 Gibbs 自由能数据推导 K」：
  逐组分调用 Component.EvaluateGibbs(T) 取 HYSYS 数据库的 Gibbs 自由能，
  由 ln K = -ΔG/(R·T) 计算 K——不查文献表、不手填任何经验系数。
  出口温度已知时在该温度下定值 K（严格精确）；出口温度未知时用两个
  参考温度拟合 ln K = A + B/T（HYSYS 内建关联式形式）。
"""

import math
import os
import time

from hysys import core, units

# 反应器类型 -> HYSYS COM 内部类型名（结构映射，与具体物质无关）
REACTOR_OP_TYPES = {
    "conversion": "ConversionReactorOp",
    "equilibrium": "EquilibriumReactorOp",
    "gibbs": "GibbsReactorOp",
}
# 反应类型 -> HYSYS COM 内部类型名
REACTION_TYPES = {
    "conversion": "conversionrxn",
    "equilibrium": "equilibriumrxn",
}
REACTION_PHASE_OVERALL = 5          # Overall（汽液全相）
EQ_BASIS_PARTIAL_PRESSURE = 2       # rbPartialPressBasis
R_GAS_KJ_PER_KMOL_K = 8.314462618   # kJ/(kgmol·K)

FEED_NAME = "Feed"
VAPOUR_NAME = "Product-Vapour"
LIQUID_NAME = "Product-Liquid"
ENERGY_NAME = "Q-Reactor"
REACTOR_NAME = "RX-1"
REACTION_SET_NAME = "RS-1"


def _fail(step, detail):
    raise core.HysysError("[builder:%s] %s" % (step, detail))


def _gibbs_keq(case, stoich, T_K):
    """用 HYSYS 内置 Gibbs 自由能计算 ln K：ln K = -Σ(ν·G)/(R·T)。"""
    components = case.BasisManager.ComponentLists.Item(0).Components
    dg = 0.0
    for comp, nu in stoich.items():
        try:
            g = components.Item(comp).EvaluateGibbs(T_K)
        except Exception as exc:
            _fail("gibbs_keq", "取组分 %r 的 Gibbs 自由能失败：%s" % (comp, exc))
        dg += float(nu) * float(g)
    return -dg / (R_GAS_KJ_PER_KMOL_K * T_K)


def _set_equilibrium_keq(case, rxn, stoich, outlet_t_c, warnings, tag):
    """设置 equilibrium 反应的 Keq（优先原生 Gibbs，回退内置 Gibbs 推导）。"""
    core.com("%s：尝试 LnKSource=Gibbs(0)" % tag,
             lambda: setattr(rxn, "LnKSource", 0))
    if core.com("%s：读回 LnKSource" % tag, lambda: rxn.LnKSource) == 0:
        warnings.append("%s：Keq 使用 HYSYS 原生 Gibbs 自由能源" % tag)
        return

    if outlet_t_c is not None:
        t_k = float(outlet_t_c) + 273.15
        ln_k = _gibbs_keq(case, stoich, t_k)
        core.com("%s：写入 Gibbs 推导的定值 K" % tag,
                 lambda: setattr(rxn, "EquilibriumConstant", math.exp(ln_k)))
        warnings.append(
            "%s：V15 COM 拒绝原生 Gibbs 源（写 0 读回非 0），已改用 HYSYS "
            "内置 Gibbs 自由能数据在出口温度 %.1f °C 下推导 K=%.4g（定值 K，"
            "该温度下严格精确）" % (tag, outlet_t_c, math.exp(ln_k)))
    else:
        # 出口温度未知：两个参考温度拟合 ln K = A + B/T
        t1, t2 = 600.0, 1200.0
        a = _gibbs_keq(case, stoich, t1)
        b = _gibbs_keq(case, stoich, t2)
        coef_b = (a - b) / (1.0 / t1 - 1.0 / t2)
        coef_a = a - coef_b / t1
        def _fit():
            rxn.LnKSource = 1
            rxn.LnKEquationAParameter = coef_a
            rxn.LnKEquationBParameter = coef_b
            rxn.LnKEquationCParameter = 0.0
            rxn.LnKEquationDParameter = 0.0
        core.com("%s：写入 Gibbs 推导的 lnK 关联式" % tag, _fit)
        warnings.append(
            "%s：V15 COM 拒绝原生 Gibbs 源，且出口温度未指定；已用 HYSYS "
            "内置 Gibbs 自由能在 %.0f/%.0f K 两点拟合 lnK = %.4f %+.1f/T"
            % (tag, t1, t2, coef_a, coef_b))
    core.com("%s：Basis=PartialPressure, BasisUnits=bar" % tag,
             lambda: (setattr(rxn, "Basis", EQ_BASIS_PARTIAL_PRESSURE),
                      setattr(rxn, "BasisUnits2", "bar")))


def _build_reactions(case, spec, outlet_t_c, warnings):
    """按 spec["reactions"] 创建反应并挂到 ReactionSet；gibbs 型不建方程。"""
    if spec["reactor_type"] == "gibbs":
        if spec.get("reactions"):
            warnings.append("Gibbs Reactor 按自由能最小化计算，spec 中的"
                            "反应方程式已忽略（不建反应方程）")
        return None

    reactions = spec.get("reactions") or []
    if not reactions:
        _fail("build_reactions", "spec['reactions'] 为空，无法建模")

    rpm = core.com("build_reactions：获取 ReactionPackageManager",
                   lambda: case.BasisManager.ReactionPackageManager)
    rs = core.com("build_reactions：创建 ReactionSet %s" % REACTION_SET_NAME,
                  lambda: rpm.ReactionSets.Add(REACTION_SET_NAME))
    fp = core.com("build_reactions：获取物性包",
                  lambda: rpm.BasisManager.FluidPackages.Item(0))
    core.com("build_reactions：ReactionSet 关联物性包",
             lambda: rs.AssociateFluidPackage(fp))

    for i, rspec in enumerate(reactions):
        rname = rspec.get("name") or "RXN-%d" % (i + 1)
        rtype = rspec.get("type")
        tag = "反应 %s" % rname
        if rtype not in REACTION_TYPES:
            _fail(tag, "不支持的反应类型 %r（支持：%s）"
                  % (rtype, "/".join(REACTION_TYPES)))
        stoich = rspec.get("stoichiometry") or {}
        if not stoich:
            _fail(tag, "stoichiometry 为空")
        rxn = core.com("%s：创建（%s）" % (tag, rtype),
                       lambda n=rname, t=rtype: rpm.Reactions.Add(
                           n, REACTION_TYPES[t]))
        for comp in stoich:
            core.com("%s：添加组分 %s" % (tag, comp),
                     lambda r=rxn, c=comp: r.Reactants.Add(c))
        core.com("%s：写入化学计量" % tag,
                 lambda r=rxn, s=stoich: setattr(
                     r, "ReactantStoichCoefValue",
                     tuple(float(v) for v in s.values())))
        core.com("%s：ReactionPhase=Overall" % tag,
                 lambda r=rxn: setattr(r, "ReactionPhase",
                                       REACTION_PHASE_OVERALL))
        # 先加入反应集，再设置类型相关参数（V15 顺序约束）
        core.com("%s：加入 ReactionSet" % tag,
                 lambda r=rxn: rs.ActiveReactions.Add(r))

        if rtype == "conversion":
            basis = rspec.get("basis_component")
            conv = rspec.get("conversion")
            if basis is None or conv is None:
                _fail(tag, "conversion 反应缺少 basis_component 或 conversion")
            core.com("%s：基准组分 %s" % (tag, basis),
                     lambda r=rxn, b=basis: setattr(r, "BaseComponent", b))
            core.com("%s：转化率 %.4g %%" % (tag, conv),
                     lambda r=rxn, v=conv: setattr(r, "ConversionValue",
                                                   float(v)))
        else:  # equilibrium
            _set_equilibrium_keq(case, rxn, stoich, outlet_t_c, warnings, tag)

    active = tuple(core.com("build_reactions：读取激活反应",
                            lambda: rs.ActiveReactions.Names))
    inactive = tuple(core.com("build_reactions：读取未激活反应",
                              lambda: rs.InactiveReactions.Names))
    if inactive or len(active) != len(reactions):
        _fail("build_reactions", "存在未激活反应：激活=%s 未激活=%s"
              % (active, inactive))
    return rs


def validate_spec(spec):
    """建模前置校验：返回缺失字段的中文说明列表（空列表 = 齐全）。

    只检查字段是否存在，不检查数值合理性；任何缺失都应先澄清，
    不允许进入 HYSYS 后再抛裸异常。
    """
    problems = []
    if not spec.get("components"):
        problems.append("组分列表（components）为空——需要进料/产物涉及"
                        "的所有 HYSYS 库组分名")
    if spec.get("reactor_type") != "gibbs" and not spec.get("reactions"):
        problems.append("反应列表（reactions）为空——需要反应方程式、"
                        "化学计量与类型（conversion/equilibrium）")
    feed = spec.get("feed") or {}
    if feed.get("flow") is None:
        problems.append("进料流量（feed.flow）未给出——需要数值与单位"
                        "（如 1000 kgmole/h 或 10000 kg/h）")
    if feed.get("T_C") is None:
        problems.append("进料温度（feed.T_C）未给出（°C）")
    if feed.get("P_bar") is None:
        problems.append("进料压力（feed.P_bar）未给出（bar）")
    if not feed.get("composition"):
        problems.append("进料组成（feed.composition）为空——需要各组分"
                        "摩尔分率")
    return problems


def build_from_spec(spec):
    """spec -> 建模 -> 求解 -> 结果 dict（契约见模块文档与用户约定）。"""
    problems = validate_spec(spec)
    if problems:
        return {"status": "need_clarification", "questions": problems}
    holder = {}
    try:
        return _build_from_spec_impl(spec, holder)
    except Exception:
        # 建模中途失败时关闭未保存的 case，避免在 HYSYS 会话中残留
        # （崩溃遗留 case 会污染求解器状态，导致后续写入 E_ACCESSDENIED）
        broken = holder.get("case")
        if broken is not None:
            try:
                broken.Close(False)
            except Exception:
                pass
        raise


def _build_from_spec_impl(spec, holder):
    warnings = []
    if spec.get("missing_info"):
        return {"status": "need_clarification",
                "questions": list(spec["missing_info"])}

    reactor_type = spec.get("reactor_type")
    if reactor_type not in REACTOR_OP_TYPES:
        return {"status": "error",
                "message": "builder 暂不支持反应器类型 %r（支持：%s）"
                           % (reactor_type, "/".join(REACTOR_OP_TYPES))}

    feed_spec = spec.get("feed") or {}
    outlet_spec = spec.get("outlet_spec") or {}
    composition = feed_spec.get("composition") or {}
    if not composition:
        _fail("feed", "进料组成为空（spec['feed']['composition']）")

    # --- case 与组分 ---
    ts = time.strftime("%Y%m%d_%H%M%S")
    case_path = os.path.join(core.CASES_DIR,
                             "gen_%s_%s.hsc" % (ts, reactor_type))
    case = core.create_case(spec.get("property_package") or "Peng-Robinson",
                            save_as=case_path)
    holder["case"] = case
    names = core.add_components(case, list(spec.get("components") or []))
    flowsheet = core.com("获取 Flowsheet", lambda: case.Flowsheet)

    # --- 反应 ---
    reaction_set = _build_reactions(case, spec, outlet_spec.get("T_C"),
                                    warnings)

    # --- 进料物流（flow_basis 语义 -> 总流量，统一换算为 kgmole/h）---
    flow_unit = (feed_spec.get("flow_unit") or "").strip()
    if feed_spec.get("flow") is None:
        _fail("feed", "进料流量为空（spec['feed']['flow']）")
    flow_basis = (feed_spec.get("flow_basis") or "total").strip()
    if flow_basis.startswith("component:"):
        basis_comp = flow_basis.split(":", 1)[1]
        x_basis = composition.get(basis_comp)
        if not x_basis:
            _fail("feed", "flow_basis=%s 但进料组成中没有该组分"
                  % flow_basis)
        # 单组分流量 -> 摩尔流量 -> 除以摩尔分率得总流量
        comp_molar = units.to_molar_flow(feed_spec["flow"], flow_unit,
                                         {basis_comp: 1.0})
        molar_flow = comp_molar / float(x_basis)
        warnings.append("进料流量按组分流量解释：%s = %s %s -> 总摩尔流量 "
                        "%.4g kgmole/h" % (basis_comp, feed_spec["flow"],
                                           flow_unit, molar_flow))
    elif flow_basis == "total":
        molar_flow = units.to_molar_flow(feed_spec["flow"], flow_unit,
                                         composition)
    else:
        _fail("feed", "未知 flow_basis %r（支持 total / component:<组分名>）"
              % flow_basis)
    feed = core.add_material_stream(
        flowsheet, FEED_NAME, T_C=feed_spec.get("T_C"),
        P_bar=feed_spec.get("P_bar"), flow=molar_flow, flow_unit="kgmole/h",
        composition=composition, component_names=names)

    # --- 反应器与出口 ---
    reactor = core.com("创建反应器 %s（%s）" % (REACTOR_NAME, reactor_type),
                       lambda: flowsheet.Operations.Add(
                           REACTOR_NAME, REACTOR_OP_TYPES[reactor_type]))
    vapour = core.add_material_stream(flowsheet, VAPOUR_NAME)
    liquid = core.add_material_stream(flowsheet, LIQUID_NAME)
    energy = core.add_energy_stream(flowsheet, ENERGY_NAME)
    core.com("反应器：连接进料", lambda: reactor.Feeds.Add(feed))
    core.com("反应器：连接汽相出口",
             lambda: setattr(reactor, "VapourProduct", vapour))
    core.com("反应器：连接液相出口",
             lambda: setattr(reactor, "LiquidProduct", liquid))
    core.com("反应器：连接能量流股",
             lambda: setattr(reactor, "EnergyStream", energy))
    if reaction_set is not None:
        core.com("反应器：关联 ReactionSet",
                 lambda: setattr(reactor, "ReactionSet", reaction_set))

    # 出口压力：连接后产物流股压力被反应器锁定（E_ACCESSDENIED），
    # 只能通过 PressureDrop 实现：ΔP = 进料P - 出口P
    if outlet_spec.get("P_bar") is not None:
        feed_p = feed_spec.get("P_bar")
        if feed_p is None:
            _fail("反应器", "指定了出口压力但进料压力为空，无法计算压降")
        dp = float(feed_p) - float(outlet_spec["P_bar"])
        if dp < 0:
            _fail("反应器", "出口压力（%s bar）高于进料压力（%s bar）"
                  % (outlet_spec["P_bar"], feed_p))
        core.com("反应器：压降 %.4f bar（出口 %s bar）"
                 % (dp, outlet_spec["P_bar"]),
                 lambda: reactor.PressureDrop.SetValue(dp, "bar"))
    else:
        core.com("反应器：压降=0",
                 lambda: setattr(reactor, "PressureDropValue", 0.0))
    # 出口温度：指定则写出口温度（热负荷由 HYSYS 反算），否则绝热
    if outlet_spec.get("T_C") is not None:
        core.com("反应器：指定出口温度 %s °C" % outlet_spec["T_C"],
                 lambda: vapour.Temperature.SetValue(float(outlet_spec["T_C"]),
                                                     "C"))
    else:
        core.com("反应器：未指定出口温度，按绝热（热负荷=0）",
                 lambda: energy.HeatFlow.SetValue(0.0, "kJ/h"))
        warnings.append("未指定出口温度，反应器按绝热操作（热负荷=0）")

    # --- 求解（不收敛不抛异常）---
    converged, solver_status = core.solve(case)
    if not converged:
        warnings.append("求解超时或未收敛，Solver 状态：%s" % solver_status)
        try:
            case.Close(False)
        except Exception:
            pass
        return {"status": "ok", "reactor_type": reactor_type,
                "selection_reason": spec.get("selection_reason", ""),
                "assumptions": spec.get("assumptions", []),
                "total_molar_flow_kgmol_h": molar_flow,
                "outlet": {"T_C": None, "P_bar": None,
                           "composition_molfrac": {}},
                "heat_duty": None, "converged": False,
                "case_file": case_path, "warnings": warnings}

    # --- 求解后按名称重新获取对象读数（旧引用不刷新）---
    pv = core.get_stream_props(flowsheet, VAPOUR_NAME)
    pl = core.get_stream_props(flowsheet, LIQUID_NAME)
    reactor2 = core.com("重新获取反应器 %s" % REACTOR_NAME,
                        lambda: flowsheet.Operations.Item(REACTOR_NAME))

    fv = pv["molar_flow_kgmol_h"] or 0.0
    fl = pl["molar_flow_kgmol_h"] or 0.0
    total = fv + fl
    outlet_comp = {}
    if total > 0 and pv["composition_molfrac"] and pl["composition_molfrac"]:
        for comp in names:
            xv = pv["composition_molfrac"].get(comp, 0.0)
            xl = pl["composition_molfrac"].get(comp, 0.0)
            outlet_comp[comp] = (fv * xv + fl * xl) / total

    duty = core.com("读取反应器热负荷", lambda: reactor2.HeatFlowValue)
    heat_duty_mw = duty / 1000.0 if duty is not None else None  # kJ/s -> MW

    core.com("保存 case", lambda: case.Save())

    return {"status": "ok", "reactor_type": reactor_type,
            "selection_reason": spec.get("selection_reason", ""),
            "assumptions": spec.get("assumptions", []),
            "total_molar_flow_kgmol_h": molar_flow,
            "outlet": {"T_C": pv["T_C"], "P_bar": pv["P_bar"],
                       "composition_molfrac": outlet_comp},
            "heat_duty": heat_duty_mw, "converged": True,
            "case_file": case_path, "warnings": warnings}
