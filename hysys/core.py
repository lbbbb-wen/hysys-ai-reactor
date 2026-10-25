# -*- coding: utf-8 -*-
"""
hysys.core —— HYSYS V15 COM 通用基建（无业务场景、无物质名硬编码）。

V15 实测约束（详见 docs/gotchas.md 与 docs/com_api_dump.txt）：
  - 禁止 SimulationCases.Add() 从零建 case（空白 basis 写不进物性包），
    一律 Open 已含物性包的模板 templates/blank_pr.hsc，再另存工作副本；
  - 组分/物性包修改在 BasisManager.StartBasisChange()/EndBasisChange() 内；
  - 流股读写一律 SetValue(值, "单位") / GetValue("单位")，-32767 为空值；
  - V15 无 Solver.Solve()，用 CanSolve 复位 + 轮询 IsSolving；
  - 求解后必须按名称重新获取对象再读数（旧引用不刷新）。
"""

import difflib
import hashlib
import os
import time
import traceback

try:
    import win32com.client as win32
except ImportError:
    win32 = None  # 延迟到 connect() 时报错，便于无 COM 环境下做纯逻辑测试

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(PROJECT_ROOT, "templates", "blank_pr.hsc")
CASES_DIR = os.path.join(PROJECT_ROOT, "cases")

EMPTY_VALUE = -32767.0

# 常见 HYSYS 组分库名，用于 Add 失败时给出来相近名称建议
# （COM 接口无法枚举组分库全量，故在此维护常见清单；属于通用基建，
#  与具体业务场景无关）
KNOWN_LIBRARY_COMPONENTS = (
    "Hydrogen", "Nitrogen", "Oxygen", "CO", "CO2", "H2O", "H2S",
    "Methane", "Ethane", "Ethylene", "Propane", "Propylene",
    "i-Butane", "n-Butane", "1-Butene", "n-Pentane", "n-Hexane",
    "n-Heptane", "n-Octane", "Benzene", "Toluene", "Ethylbenzene",
    "o-Xylene", "m-Xylene", "p-Xylene", "Methanol", "Ethanol",
    "1-Propanol", "2-Propanol", "Acetone", "Ammonia",
)

# ---------------------------------------------------------------------------
# 异常与工具
# ---------------------------------------------------------------------------


class HysysError(RuntimeError):
    """带上下文的 HYSYS 操作异常。"""


def _context_fail(step, detail):
    raise HysysError("[%s] %s" % (step, detail))


def com(step, fn):
    """执行一次 COM 调用；失败时附加上下文（哪一步）与完整 traceback 后抛出。"""
    try:
        return fn()
    except HysysError:
        raise
    except Exception as exc:
        traceback.print_exc()
        _context_fail(step, "COM 调用失败：%s: %s" % (type(exc).__name__, exc))


def get_value(var, unit):
    """RealVariable.GetValue(单位)；空值 -32767 统一返回 None。"""
    try:
        v = var.GetValue(unit)
    except Exception:
        return None
    if v is None or abs(v - EMPTY_VALUE) < 1e-6:
        return None
    return v


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 连接与 case
# ---------------------------------------------------------------------------

_app = None


def connect():
    """连接/复用 HYSYS 实例（模块级缓存，避免重复启动多实例）。"""
    global _app
    if _app is not None:
        try:
            _ = _app.Version  # 探活：旧实例可能已被关闭
            return _app
        except Exception:
            _app = None
    if win32 is None:
        _context_fail("connect", "缺少 pywin32，请先 pip install -r requirements.txt")
    try:
        _app = win32.GetActiveObject("HYSYS.Application")
    except Exception:
        _app = com("connect：启动 HYSYS.Application",
                   lambda: win32.Dispatch("HYSYS.Application"))
    return _app


def create_case(property_package="Peng-Robinson", save_as=None):
    """从模板创建 case（另存工作副本，绝不覆盖模板）。

    property_package：目标物性包的注册基名（默认 Peng-Robinson，即模板
    自带物性包；指定其它包名时在 Basis 变更事务内切换）。
    save_as：工作副本路径；None 则只在内存中操作模板（调用方负责保存，
    关闭时务必 Close(False) 以免污染模板）。
    """
    if not os.path.isfile(TEMPLATE):
        _context_fail("create_case", "模板不存在：%s" % TEMPLATE)
    app = connect()
    case = com("create_case：打开模板", lambda: app.SimulationCases.Open(TEMPLATE))
    if save_as:
        os.makedirs(os.path.dirname(save_as), exist_ok=True)
        if os.path.exists(save_as):
            os.remove(save_as)
        com("create_case：另存工作副本", lambda: case.SaveAs(save_as))

    if property_package and property_package != "Peng-Robinson":
        bm = case.BasisManager
        com("create_case：进入 Basis 变更模式",
            lambda: bm.StartBasisChange() if not bm.IsChangingBasis else None)
        fp = com("create_case：获取物性包", lambda: bm.FluidPackages.Item(0))
        com("create_case：切换物性包为 %s" % property_package,
            lambda: setattr(fp, "PropertyPackageName", property_package))
    return case


# ---------------------------------------------------------------------------
# 组分
# ---------------------------------------------------------------------------


def add_components(case, names):
    """在 Basis 变更事务内逐个添加组分；失败时打印组分库相近名称。

    返回按 HYSYS 顺序排列的组分名元组（后续组成数组按下标对应）。
    """
    if not names:
        _context_fail("add_components", "组分列表为空")
    bm = case.BasisManager
    if not com("add_components：读取 IsChangingBasis",
               lambda: bm.IsChangingBasis):
        com("add_components：进入 Basis 变更模式", lambda: bm.StartBasisChange())

    components = com("add_components：获取组分列表",
                     lambda: bm.ComponentLists.Item(0).Components)
    existing = set(com("add_components：读取现有组分", lambda: components.Names))

    def _rollback_then_fail(detail):
        # 实测：CanEndBasisChange=False 时 EndBasisChange 等价于取消本次变更，
        # 可退出变更模式（IsChangingBasis 回到 False），case 继续可用。
        try:
            bm.EndBasisChange()
        except Exception:
            pass
        _context_fail("add_components", detail)

    for name in names:
        if name in existing:
            continue
        try:
            components.Add(name)
        except Exception as exc:
            suggestion = difflib.get_close_matches(
                name, KNOWN_LIBRARY_COMPONENTS, n=5, cutoff=0.4)
            hint = ("组分库中与之相近的名称：%s" % ", ".join(suggestion)
                    if suggestion else "未找到相近名称，请查阅 HYSYS 组分库")
            _rollback_then_fail(
                "添加组分 %r 失败（%s: %s）。组分名必须是 HYSYS 库名；%s"
                % (name, type(exc).__name__, exc, hint))

    current = tuple(com("add_components：读取组分", lambda: components.Names))
    missing = [n for n in names if n not in current]
    if missing:
        _rollback_then_fail("以下组分添加后仍未出现：%s" % missing)

    if com("add_components：读取 CanEndBasisChange",
           lambda: bm.CanEndBasisChange):
        com("add_components：提交 Basis 变更", lambda: bm.EndBasisChange())
    else:
        _rollback_then_fail("CanEndBasisChange=False，物性包/组分信息不完整")
    return current


# ---------------------------------------------------------------------------
# 流股
# ---------------------------------------------------------------------------


def add_material_stream(flowsheet, name, T_C=None, P_bar=None,
                        flow=None, flow_unit=None, composition=None,
                        component_names=None):
    """创建物流并按给定条件写入（给什么写什么，不给的保持未指定）。

    composition：{组分名: 摩尔分率}；component_names：case 的完整组分顺序
    （写分率数组必需，通常取 add_components 的返回值）。
    flow_unit：显式单位，摩尔流量（kgmole/h/kmol/h）写 MolarFlow，
    质量流量（kg/h）写 MassFlow；单位换算由 hysys.units 完成的调用方
    可直接传摩尔流量。
    """
    stream = com("add_material_stream：创建物流 %s" % name,
                 lambda: flowsheet.MaterialStreams.Add(name))
    if T_C is not None:
        com("add_material_stream：%s 温度 %s °C" % (name, T_C),
            lambda: stream.Temperature.SetValue(float(T_C), "C"))
    if P_bar is not None:
        com("add_material_stream：%s 压力 %s bar" % (name, P_bar),
            lambda: stream.Pressure.SetValue(float(P_bar), "bar"))
    if composition:
        if not component_names:
            _context_fail("add_material_stream",
                          "写物流 %s 组成时必须提供 component_names 顺序" % name)
        idx = {n: i for i, n in enumerate(component_names)}
        unknown = [c for c in composition if c not in idx]
        if unknown:
            _context_fail("add_material_stream",
                          "物流 %s 的组成含有不在 case 中的组分：%s"
                          % (name, unknown))
        x = [0.0] * len(component_names)
        for comp, frac in composition.items():
            x[idx[comp]] = float(frac)
        com("add_material_stream：%s 写入摩尔分率" % name,
            lambda: setattr(stream, "ComponentMolarFractionValue", tuple(x)))
    if flow is not None:
        unit = (flow_unit or "").strip()
        if unit in ("kgmole/h", "kmol/h", "kgmol/h"):
            com("add_material_stream：%s 摩尔流量 %s %s" % (name, flow, unit),
                lambda: stream.MolarFlow.SetValue(float(flow), "kgmole/h"))
        elif unit == "kg/h":
            com("add_material_stream：%s 质量流量 %s kg/h" % (name, flow),
                lambda: stream.MassFlow.SetValue(float(flow), "kg/h"))
        else:
            _context_fail("add_material_stream",
                          "物流 %s 的流量单位 %r 不支持，请显式给出 "
                          "kgmole/h、kmol/h 或 kg/h" % (name, flow_unit))
    return stream


def add_energy_stream(flowsheet, name):
    """创建能量流股。"""
    return com("add_energy_stream：创建 %s" % name,
               lambda: flowsheet.EnergyStreams.Add(name))


def get_stream_props(flowsheet, name):
    """按名称重新获取物流并读出物性（求解后必须走这里，旧引用不刷新）。

    返回 {"name", "T_C", "P_bar", "molar_flow_kgmol_h",
          "mass_flow_kg_h", "composition_molfrac": {组分名: 分率}}；
    未计算出的字段为 None。
    """
    stream = com("get_stream_props：获取物流 %s" % name,
                 lambda: flowsheet.MaterialStreams.Item(name))
    composition = None
    try:
        comps = tuple(stream.Flowsheet.FluidPackage.Components.Names)
    except Exception:
        comps = None
    try:
        fracs = tuple(stream.ComponentMolarFractionValue)
        if any(abs(x - EMPTY_VALUE) < 1e-6 for x in fracs):
            fracs = None
    except Exception:
        fracs = None
    if fracs is not None and comps is not None and len(comps) == len(fracs):
        composition = dict(zip(comps, fracs))
    return {
        "name": name,
        "T_C": get_value(stream.Temperature, "C"),
        "P_bar": get_value(stream.Pressure, "bar"),
        "molar_flow_kgmol_h": get_value(stream.MolarFlow, "kgmole/h"),
        "mass_flow_kg_h": get_value(stream.MassFlow, "kg/h"),
        "composition_molfrac": composition,
    }


# ---------------------------------------------------------------------------
# 求解
# ---------------------------------------------------------------------------


def solve(case, timeout=120.0):
    """触发完整重算并轮询直到求解器空闲。

    返回 (converged: bool, status: dict)。converged 以 IsSolving 归零为准；
    status 携带 CanSolve/IsSolving/IsForgetting 供上层附在返回值里，
    不收敛时不抛异常（由上层决定）。
    """
    solver = com("solve：获取 Solver", lambda: case.Solver)
    if hasattr(solver, "Solve"):
        com("solve：Solver.Solve()", solver.Solve)
    else:
        # V15 类型库无 Solver.Solve()：CanSolve 复位强制完整重算
        com("solve：CanSolve=False", lambda: setattr(solver, "CanSolve", False))
        com("solve：CanSolve=True（触发完整重算）",
            lambda: setattr(solver, "CanSolve", True))

    time.sleep(0.5)  # 给异步求解线程一个启动窗口
    deadline = time.time() + timeout
    converged = False
    while time.time() < deadline:
        if not com("solve：读取 IsSolving", lambda: solver.IsSolving):
            converged = True
            break
        time.sleep(0.2)
    status = {
        "CanSolve": com("solve：读取 CanSolve", lambda: solver.CanSolve),
        "IsSolving": com("solve：读取 IsSolving", lambda: solver.IsSolving),
        "IsForgetting": com("solve：读取 IsForgetting",
                            lambda: solver.IsForgetting),
    }
    return converged, status
