# -*- coding: utf-8 -*-
"""
hysys.units —— 单位换算与组分分子量表（集中定义，禁止散落在业务代码中）。

分子量单位：kg/kmol（数值等同 g/mol）。
流量约定：摩尔流量 kgmole/h 与 kmol/h 等价（1 kgmole = 1 kmol）。
"""

# HYSYS 库组分名 -> 分子量 kg/kmol
MOLECULAR_WEIGHTS = {
    "Hydrogen": 2.016,
    "Methane": 16.043,
    "H2O": 18.015,
    "Ethylene": 28.054,
    "CO": 28.010,
    "Nitrogen": 28.014,
    "Ethane": 30.070,
    "Oxygen": 31.999,
    "Ethanol": 46.069,
    "CO2": 44.010,
    "Propane": 44.096,
    "Propylene": 42.081,
    "i-Butane": 58.123,
    "n-Butane": 58.123,
    "Benzene": 78.114,
    "Toluene": 92.141,
    "o-Xylene": 106.167,
    "m-Xylene": 106.167,
    "p-Xylene": 106.167,
    "Ethylbenzene": 106.167,
    "E-Benzene": 106.167,
    "Styrene": 104.152,
    "Cumene": 120.194,
    "Ammonia": 17.031,
    "Methanol": 32.042,
    "N2": 28.014,
    "O2": 31.999,
}

# 摩尔流量单位的别名 -> 规范单位
MOLAR_FLOW_UNITS = {"kgmole/h", "kmol/h", "kgmol/h", "lbmol/h"}
MASS_FLOW_UNITS = {"kg/h", "kg/s", "t/h", "lb/h"}


def molecular_weight(component):
    """查组分分子量；未知组分抛出带上下文的异常。"""
    try:
        return MOLECULAR_WEIGHTS[component]
    except KeyError:
        raise ValueError(
            "单位换算失败：组分 %r 不在分子量表中（hysys/units.py "
            "MOLECULAR_WEIGHTS）。请在该表中补充其分子量后重试。" % component
        )


def mixture_molecular_weight(composition):
    """按摩尔分率加权的混合物分子量 kg/kmol。composition: {组分名: 摩尔分率}"""
    if not composition:
        raise ValueError("单位换算失败：进料组成为空，无法计算混合物分子量")
    total = 0.0
    for name, frac in composition.items():
        total += molecular_weight(name) * float(frac)
    return total


def mass_to_molar_flow(mass_flow, composition):
    """kg/h -> kmol/h（按组成加权分子量）。"""
    mw = mixture_molecular_weight(composition)
    return float(mass_flow) / mw


def molar_to_mass_flow(molar_flow, composition):
    """kmol/h -> kg/h（按组成加权分子量）。"""
    mw = mixture_molecular_weight(composition)
    return float(molar_flow) * mw


def to_molar_flow(flow, flow_unit, composition):
    """把任意流量单位规范化为 kgmole/h。

    flow_unit 必须是显式单位（kg/h、kmol/h、kgmole/h 等）；质量流量
    按组成加权分子量换算，未知单位抛出带上下文的异常。
    """
    if flow is None:
        raise ValueError("单位换算失败：流量值为 None（flow_unit=%r）" % flow_unit)
    unit = (flow_unit or "").strip()
    if unit in ("kgmole/h", "kmol/h", "kgmol/h"):
        return float(flow)
    if unit == "kg/h":
        return mass_to_molar_flow(flow, composition)
    raise ValueError(
        "单位换算失败：不支持的流量单位 %r（flow=%r）。支持：kgmole/h、"
        "kmol/h、kg/h；质量流量按组成加权分子量换算。" % (flow_unit, flow)
    )
