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
import re
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
REACTION_PHASE_VAPOUR = 0           # VapourOnly（GUI: Phase=Vapor）
EQ_BASIS_PARTIAL_PRESSURE = 2       # rbPartialPressBasis
R_GAS_KJ_PER_KMOL_K = 8.314462618   # kJ/(kgmol·K)

FEED_NAME = "Feed"
VAPOUR_NAME = "Product-Vapour"
LIQUID_NAME = "Product-Liquid"
ENERGY_NAME = "Q-Reactor"
REACTOR_NAME = "RX-1"
REACTION_SET_NAME = "RS-1"


def _reaction_name(rspec, index):
    """反应名清洗为 HYSYS 安全的 ASCII 短名（中文/长名会显示成问号）。

    parser 已要求 LLM 直接输出通用英文缩写（SMR/WGS/TDP-1 等），
    这里只做兜底：紧凑缩写（无空格、ASCII）原样大写采用；英文全名
    取首字母缩写（Steam Methane Reforming -> SMR）；纯中文名等
    无英文可用的情形退化为 RXN{序号}。"""
    raw = (rspec.get("name") or "").strip()
    if raw and " " not in raw and all(ord(c) < 128 for c in raw):
        compact = re.sub(r"[^A-Za-z0-9_-]", "", raw).upper()[:16]
        if compact:
            return compact
    words = re.findall(r"[A-Za-z]+", raw)
    abbr = "".join(w[0].upper() for w in words)[:10]
    return abbr or "RXN%d" % (index + 1)


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
        k_rb = core.com("%s：读回 Keq" % tag,
                        lambda: rxn.EquilibriumConstant)
        if not (k_rb and math.isfinite(float(k_rb))):
            warnings.append("%s：Keq 读回异常（%r），已记录" % (tag, k_rb))
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
    rb = core.com("%s：读回 Basis" % tag, lambda: rxn.Basis)
    if rb != EQ_BASIS_PARTIAL_PRESSURE:
        warnings.append("%s：Basis 读回 %r != PartialPress(%d)，已记录"
                        % (tag, rb, EQ_BASIS_PARTIAL_PRESSURE))


def _set_base_component(case, rxn, comp_name, tag):
    """设置 Conversion 反应基准组分。

    V15 实测（docs/conversion_base_component.txt）：BaseComponent 是
    Component 对象属性，赋字符串报 Type mismatch (-2147352571)；
    必须从 FluidPackage.Components / ComponentLists 取 Component 对象。
    优先对象写法，回退字符串，两条都失败才抛带两类错误的异常。"""
    fp = core.com("%s：获取物性包" % tag,
                  lambda: case.BasisManager.FluidPackages.Item(0))
    errors = []
    comp_sources = [
        ("FluidPackage.Components.Item",
         lambda: fp.Components.Item(comp_name)),
        ("ComponentLists[0].Components.Item",
         lambda: case.BasisManager.ComponentLists.Item(0)
         .Components.Item(comp_name)),
    ]
    for label, get_comp in comp_sources:
        try:
            comp = get_comp()
            rxn.BaseComponent = comp
            rb = rxn.BaseComponent
            if getattr(rb, "name", None) != comp_name:
                raise core.HysysError(
                    "%s：BaseComponent 读回组分 %r != %r"
                    % (tag, getattr(rb, "name", rb), comp_name))
            return
        except Exception as exc:
            errors.append("%s 对象写法失败：%s" % (label, str(exc)[:120]))
    try:  # 最后兜底：字符串（已知会 Type mismatch，但保留以防版本差异）
        rxn.BaseComponent = comp_name
        return
    except Exception as exc:
        errors.append("字符串写法失败：%s" % str(exc)[:120])
    _fail(tag, "BaseComponent 设置失败（组分 %s）：%s"
          % (comp_name, " | ".join(errors)))


def _build_reactions(case, spec, outlet_t_c, warnings, comp_map=None):
    """按 spec["reactions"] 创建反应并挂到 ReactionSet；gibbs 型不建方程。

    comp_map: canonical 名 -> HYSYS 实际组分名（别名解析结果），
    用于翻译 stoichiometry 键与 basis_component。"""
    comp_map = comp_map or {}
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
        rname = _reaction_name(rspec, i)
        rtype = rspec.get("type")
        tag = "反应 %s" % rname
        if rtype not in REACTION_TYPES:
            _fail(tag, "不支持的反应类型 %r（支持：%s）"
                  % (rtype, "/".join(REACTION_TYPES)))
        stoich = {comp_map.get(k, k): v
                  for k, v in (rspec.get("stoichiometry") or {}).items()}
        if not stoich:
            _fail(tag, "stoichiometry 为空")
        rxn = core.com("%s：创建（%s）" % (tag, rtype),
                       lambda n=rname, t=rtype: rpm.Reactions.Add(
                           n, REACTION_TYPES[t]))
        for comp in stoich:
            core.com("%s：添加组分 %s" % (tag, comp),
                     lambda r=rxn, c=comp: r.Reactants.Add(c))
        # 实测（docs/conversion_base_component.txt）：HYSYS 接受分数系数
        # （如 (-2, 1, 0.25)），无需为满足输入限制拆分多个平行反应
        core.com("%s：写入化学计量" % tag,
                 lambda r=rxn, s=stoich: setattr(
                     r, "ReactantStoichCoefValue",
                     tuple(float(v) for v in s.values())))
        # Phase：GUI 实测 Phase 为空会导致反应器 Unknown Duty。
        # equilibrium 气相体系显式设 Vapor（液相体系暂无 spec 字段，
        # 默认 Vapor 并写 warning）；conversion 沿用已验证的 Overall。
        if rtype == "equilibrium":
            phase = REACTION_PHASE_VAPOUR
            phase_name = "Vapor"
            warnings.append("%s：反应相态按气相处理（Phase=Vapor）；"
                            "液相体系需显式指定" % tag)
        else:
            phase = REACTION_PHASE_OVERALL
            phase_name = "Overall"
        core.com("%s：ReactionPhase=%s" % (tag, phase_name),
                 lambda r=rxn, p=phase: setattr(r, "ReactionPhase", p))
        rb = core.com("%s：读回 ReactionPhase" % tag,
                      lambda r=rxn: r.ReactionPhase)
        if rb != phase:
            warnings.append("%s：Phase 读回 %r != %s(%d)，已记录"
                            % (tag, rb, phase_name, phase))
        # 先加入反应集，再设置类型相关参数（V15 顺序约束）
        core.com("%s：加入 ReactionSet" % tag,
                 lambda r=rxn: rs.ActiveReactions.Add(r))

        if rtype == "conversion":
            basis = comp_map.get(rspec.get("basis_component"),
                                 rspec.get("basis_component"))
            conv = rspec.get("conversion")
            if basis is None or conv is None:
                _fail(tag, "conversion 反应缺少 basis_component 或 conversion")
            _set_base_component(case, rxn, basis, tag)
            # 类型库属性为 Conversion（float，百分比）；ConversionValue 不存在
            core.com("%s：转化率 %.4g %%" % (tag, conv),
                     lambda r=rxn, v=conv: setattr(r, "Conversion", float(v)))
            conv_rb = core.com("%s：读回转化率" % tag, lambda r=rxn: r.Conversion)
            if conv_rb is None or abs(float(conv_rb) - float(conv)) > 1e-6:
                warnings.append("%s：转化率读回 %r != %.4g，已记录"
                                % (tag, conv_rb, conv))
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


def _check_shared_basis(spec):
    """永久防线：多个 conversion 反应共享同一 basis_component 时，
    HYSYS 会按顺序独立结算每条反应的转化率，总转化率叠加为
    1-(1-x)^n（如三条 50% 变 87.5%），结果错误。命中即拒绝建模。"""
    seen = {}
    for i, r in enumerate(spec.get("reactions") or []):
        if (r.get("type") or "").strip().lower() != "conversion":
            continue
        bc = (r.get("basis_component") or "").strip().lower()
        if not bc:
            continue
        rname = r.get("name") or "RXN-%d" % (i + 1)
        if bc in seen:
            return ("检测到多个 conversion 反应共享基组分 %s"
                    "（反应 %s 与 %s），会导致转化率叠加。"
                    "请合并为单反应+分数系数，或在 builder 中启用均摊模式"
                    % (r.get("basis_component"), seen[bc], rname))
        seen[bc] = rname
    return None


def validate_spec(spec):
    """建模前置校验：返回缺失字段的中文说明列表（空列表 = 齐全）。

    只检查字段是否存在，不检查数值合理性；任何缺失都应先澄清，
    不允许进入 HYSYS 后再抛裸异常。
    """
    problems = []
    rt = spec.get("reactor_type")
    if rt is not None and rt not in REACTOR_OP_TYPES:
        problems.append("反应器类型 %r 暂未实现建模（当前支持：%s）；"
                        "cstr/pfr 需要动力学模型，后续版本再支持"
                        % (rt, "/".join(sorted(REACTOR_OP_TYPES))))
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


def _verify_reactions(case, spec):
    """反应完整性检查：逐反应重取对象，校验 Phase/Basis/Keq 非空。
    返回问题列表（空 = 完整）。任一缺失都不得进入求解。"""
    problems = []
    rpm = core.com("完整性检查：获取 ReactionPackageManager",
                   lambda: case.BasisManager.ReactionPackageManager)
    for i, rspec in enumerate(spec.get("reactions") or []):
        rname = _reaction_name(rspec, i)
        rtype = rspec.get("type")
        try:
            rxn = rpm.Reactions.Item(rname)
        except Exception as exc:
            problems.append("反应 %s：创建后按名称取回失败（%s）"
                            % (rname, str(exc)[:80]))
            continue
        try:
            if rxn.ReactionPhase is None:
                problems.append("反应 %s：Phase 为空" % rname)
        except Exception as exc:
            problems.append("反应 %s：Phase 读取失败（%s）"
                            % (rname, str(exc)[:80]))
        if rtype == "equilibrium":
            try:
                if rxn.Basis is None:
                    problems.append("反应 %s：Basis 为空" % rname)
            except Exception as exc:
                problems.append("反应 %s：Basis 读取失败（%s）"
                                % (rname, str(exc)[:80]))
            keq_ok = False
            try:
                k = rxn.EquilibriumConstant
                keq_ok = (k is not None and math.isfinite(float(k))
                          and float(k) > 0)
            except Exception:
                pass
            if not keq_ok:
                try:
                    a = rxn.LnKEquationAParameter
                    b = rxn.LnKEquationBParameter
                    keq_ok = bool(a) or bool(b)
                except Exception:
                    pass
            if not keq_ok:
                problems.append("反应 %s：Keq 未设置或非法" % rname)
        elif rtype == "conversion":
            try:
                bc = rxn.BaseComponent
                if bc is None or not getattr(bc, "name", None):
                    problems.append("反应 %s：BaseComponent 为空" % rname)
            except Exception as exc:
                problems.append("反应 %s：BaseComponent 读取失败（%s）"
                                % (rname, str(exc)[:80]))
            try:
                c = rxn.Conversion
                if c is None or not math.isfinite(float(c)):
                    problems.append("反应 %s：Conversion 为空" % rname)
            except Exception as exc:
                problems.append("反应 %s：Conversion 读取失败（%s）"
                                % (rname, str(exc)[:80]))
    return problems


def build_from_spec(spec):
    """spec -> 建模 -> 求解 -> 结果 dict（契约见模块文档与用户约定）。"""
    problems = validate_spec(spec)
    if problems:
        return {"status": "need_clarification", "questions": problems}
    shared = _check_shared_basis(spec)
    if shared:
        return {"status": "error", "message": shared}
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
    # 别名解析：canonical 名 -> HYSYS Simulation Name（含分子量读回校验）
    comp_records = []
    names, comp_map = core.add_components_resolved(
        case, list(spec.get("components") or []), records=comp_records)
    for rec in comp_records:
        if rec["requested"] != rec["actual"]:
            warnings.append("组分 %r 经别名解析为 %r（%s, MW=%.2f）"
                            % (rec["requested"], rec["actual"],
                               rec["formula"], rec["mw"]))
    # 白名单比较用实际（解析后）组分名
    spec_actual = set(comp_map.values())
    # spec 内所有组分引用（进料组成 / flow_basis）翻译为实际组分名
    composition = {comp_map.get(k, k): v for k, v in composition.items()}
    if (feed_spec.get("flow_basis") or "").startswith("component:"):
        bcomp = feed_spec["flow_basis"].split(":", 1)[1]
        feed_spec = dict(feed_spec,
                         flow_basis="component:%s" % comp_map.get(bcomp, bcomp))
    # 组分白名单：case 组分集合必须严格等于 spec["components"]（顺序无关）。
    # 多余的先尝试移除，清不掉则 fail fast，不带病出结果。
    extra = [n for n in names if n not in spec_actual]
    if extra:
        warnings.append("检测到白名单外组分 %s，尝试移除" % extra)
        cl = core.com("白名单：获取组分列表",
                      lambda: case.BasisManager.ComponentLists.Item(0))
        bm = core.com("白名单：进入 Basis 变更模式",
                      lambda: case.BasisManager)
        core.com("白名单：StartBasisChange",
                 lambda: bm.StartBasisChange())
        try:
            for n in extra:
                core.com("白名单：移除组分 %s" % n,
                         lambda c=n: cl.Components.Remove(c))
        finally:
            core.com("白名单：EndBasisChange",
                     lambda: bm.EndBasisChange())
        names = tuple(core.com("白名单：读回组分名",
                               lambda: tuple(cl.Components.Names)))
        still = [n for n in names if n not in spec_actual]
        if still:
            _fail("白名单", "无法清除白名单外组分 %s，拒绝生成 case" % still)
    flowsheet = core.com("获取 Flowsheet", lambda: case.Flowsheet)

    # --- 反应 ---
    reaction_set = _build_reactions(case, spec, outlet_spec.get("T_C"),
                                    warnings, comp_map)
    if reaction_set is not None:
        rxn_problems = _verify_reactions(case, spec)
        if rxn_problems:
            core.com("保存 case（完整性检查未通过，未求解）",
                     lambda: case.Save())
            core.show_case(case)  # 演示：弹出最终 case 窗口
            warnings.append("反应完整性检查未通过，已跳过求解："
                            + "；".join(rxn_problems))
            return {"status": "ok", "reactor_type": reactor_type,
                    "selection_reason": spec.get("selection_reason", ""),
                    "assumptions": spec.get("assumptions", []),
                    "total_molar_flow_kgmol_h": None,
                    "outlet": {"T_C": None, "P_bar": None,
                               "composition_molfrac": {}},
                    "heat_duty": None, "converged": False,
                    "case_file": case_path, "warnings": warnings}

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
    # 非白名单组分只要携带流量即报错（防串味）；白名单用实际组分名
    whitelist = spec_actual
    for label, p, f in (("汽相出口", pv, fv), ("液相出口", pl, fl)):
        for comp, x in (p["composition_molfrac"] or {}).items():
            if comp not in whitelist and x is not None and x * f > 1e-9:
                _fail("出口检查", "%s存在非白名单组分 %s（分率 %.3g）且流量>0"
                      % (label, comp, x))
    outlet_comp = {}
    if total > 0 and pv["composition_molfrac"] and pl["composition_molfrac"]:
        for comp in spec["components"]:  # 只输出白名单组分（按 canonical 名输出）
            actual = comp_map.get(comp, comp)
            xv = pv["composition_molfrac"].get(actual, 0.0) or 0.0
            xl = pl["composition_molfrac"].get(actual, 0.0) or 0.0
            outlet_comp[comp] = (fv * xv + fl * xl) / total

    # 热负荷多路径容错：能量流股 HeatFlow 优先，反应器 HeatFlowValue 兜底；
    # 结果字段读取失败只降级为 warning，不中断流程
    duty = None
    duty_paths = [
        ("能量流股 %s.HeatFlow" % ENERGY_NAME,
         lambda: core.com("重新获取能量流股 %s" % ENERGY_NAME,
                          lambda: flowsheet.EnergyStreams.Item(ENERGY_NAME))
         .HeatFlow.GetValue("kJ/s")),
        ("反应器 %s.HeatFlowValue" % REACTOR_NAME,
         lambda: reactor2.HeatFlowValue),
    ]
    for label, fn in duty_paths:
        try:
            v = fn()
        except Exception as exc:
            warnings.append("热负荷读取失败（%s）：%s" % (label, str(exc)[:120]))
            continue
        if v is not None and abs(float(v) - (-32767.0)) > 1e-6:
            # -32767 为 HYSYS 空值哨兵（精确匹配；大数值放热是合法值）
            duty = float(v)
            break
        warnings.append("热负荷为空值（%s 返回 %r）" % (label, v))
    if duty is None:
        warnings.append("热负荷不可用：所有读取路径均失败，duty=null")
    heat_duty_mw = duty / 1000.0 if duty is not None else None  # kJ/s -> MW

    # --- 转化率：一律按基准组分摩尔流量 1 - F_out/F_in 计算 ---
    # （不能用 1 - 出口摩尔分率：增摩尔反应中分率口径会高估转化率，
    #   实测案例：乙苯脱氢分率口径得 0.722，摩尔流量口径得 0.566）
    conversions = {}
    feed_x = composition  # 已翻译为实际组分名
    for i, rspec in enumerate(spec.get("reactions") or []):
        rname = _reaction_name(rspec, i)
        stoich = {comp_map.get(k, k): v
                  for k, v in (rspec.get("stoichiometry") or {}).items()}
        if not stoich:
            continue
        basis = comp_map.get(rspec.get("basis_component"),
                             rspec.get("basis_component"))
        if not basis:
            # equilibrium 等无显式基准：取第一个反应物（系数为负）
            basis = next((c for c, v in stoich.items() if v < 0), None)
        if not basis:
            continue
        f_in = (feed_x.get(basis) or 0.0) * molar_flow
        if f_in <= 0:
            warnings.append("反应 %s：基准组分 %s 进料流量为 0，转化率不可算"
                            % (rname, basis))
            continue
        f_out = ((pv["composition_molfrac"] or {}).get(basis, 0.0) or 0.0) * fv \
            + ((pl["composition_molfrac"] or {}).get(basis, 0.0) or 0.0) * fl
        conversions[rname] = 1.0 - f_out / f_in

        # 自检（单反应）：各组分出口摩尔流量应符合化学计量
        # F_out_i = F_in_i + ν_i/ν_basis × (F_in_b - F_out_b)，偏差 >1% 告警
        if len(spec.get("reactions") or []) == 1 and abs(f_in - f_out) > 1e-12:
            nu_b = stoich[basis]
            extent = (f_out - f_in) / nu_b  # ν_b < 0
            for comp, nu in stoich.items():
                if comp == basis:
                    continue
                f_in_i = (feed_x.get(comp) or 0.0) * molar_flow
                pred = f_in_i + nu * extent
                act = ((pv["composition_molfrac"] or {})
                       .get(comp, 0.0) or 0.0) * fv \
                    + ((pl["composition_molfrac"] or {})
                       .get(comp, 0.0) or 0.0) * fl
                denom = max(abs(pred), 1e-9)
                if abs(act - pred) / denom > 0.01:
                    warnings.append(
                        "化学计量自检：%s 出口摩尔流量实测 %.4g 与理论 %.4g "
                        "偏差 %.1f%% > 1%%（口径可能不一致）"
                        % (comp, act, pred, abs(act - pred) / denom * 100))

    # --- 结果完整性断言：出口各物流组成合计 1.000±0.001、duty 有限 ---
    integrity = []
    for label, p in (("汽相出口", pv), ("液相出口", pl)):
        comp = p["composition_molfrac"]
        vals = [v for v in (comp or {}).values() if v is not None]
        flow = p["molar_flow_kgmol_h"] or 0.0
        if flow <= 1e-6 and (not vals or abs(sum(vals)) <= 1e-6):
            continue  # 零流量空流股：无组成可言，跳过断言
        if not vals:
            integrity.append("%s 组成为空" % label)
        elif abs(sum(vals) - 1.0) > 0.001:
            integrity.append("%s 组成合计 %.4f ≠ 1.000" % (label, sum(vals)))
    if heat_duty_mw is None or not math.isfinite(heat_duty_mw):
        integrity.append("热负荷不是有限数值")
    if integrity:
        converged = False
        warnings.append("结果完整性断言未通过：" + "；".join(integrity))

    core.com("保存 case", lambda: case.Save())
    core.show_case(case)  # 演示：求解结束后弹出最终 case 窗口

    return {"status": "ok", "reactor_type": reactor_type,
            "selection_reason": spec.get("selection_reason", ""),
            "assumptions": spec.get("assumptions", []),
            "total_molar_flow_kgmol_h": molar_flow,
            "outlet": {"T_C": pv["T_C"], "P_bar": pv["P_bar"],
                       "composition_molfrac": outlet_comp},
            "conversions": conversions,
            "heat_duty": heat_duty_mw, "converged": converged,
            "case_file": case_path, "warnings": warnings}
