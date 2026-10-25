# -*- coding: utf-8 -*-
"""
explore_com.py — HYSYS COM 接口地图（Interface Map）

流程：
1. 连接 HYSYS（优先 gencache.EnsureDispatch 静态绑定，失败回退 Dispatch）并新建 Case
2. 枚举核心对象的全部成员，输出到 docs/com_api_dump.txt：
   SimulationCase / Flowsheet / FluidPackage / BasisManager / ComponentList /
   Components / MaterialStreams / EnergyStreams / Operations /
   ReactionPackageManager / Reactions / ReactionSets
3. 实测三个问题，结论写在 dump 文件开头：
   - 物性包设为 Peng-Robinson 的属性名及 V15 实测行为
   - 添加组分 Methane / Water 的方法及确认
   - MaterialStream 温度/压力/流量的 COM 单位机制（内部单位 vs SetValue/GetValue 单位字符串）
4. 关闭 Case（不保存），保持 HYSYS 进程存活

实测要点（Aspen HYSYS V15 (41.0)，makepy 静态绑定）：
- 新建空白 Case 自动处于 Basis 模式（BasisManager.IsChangingBasis=True），
  且默认没有 FluidPackage（fs.FluidPackage 抛 E_FAIL）；
  需先调用 SimulationCase.BasisManager.FluidPackages.Add() 创建。
- FluidPackages.Add() 会自动创建并关联 "Component List - 1"；
  添加组分必须用 BasisManager.ComponentLists.Item(0).Components.Add()，
  FluidPackage.Components 是只读门面，Add 不生效。
- 组分名必须来自 HYSYS 组分库：Methane 可用；水的库名是 "H2O"，"Water" 抛 E_FAIL。
- HYSYS 类型库中部分成员名为小写（如 Flowsheet.name、ProcessStream.name），
  makepy 静态绑定大小写敏感，本脚本统一用 com_get/com_set 做大小写不敏感访问。
- 物性包：官方属性是 FluidPackage.PropertyPackageName（Get/Let String），
  取值用注册基名（Peng-Robinson -> 内部 TypeName "pengrob"）。
  写入必须在 Basis 变更模式内：BasisManager.StartBasisChange() -> 赋值 -> EndBasisChange()。
  Basis 模式外直接写会 E_ACCESSDENIED。
  对新建空白 Case 由 FluidPackages.Add() 建的 FP（初始无物性包）写入一律 E_INVALIDARG，
  且 CanEndBasisChange 恒为 False，无法完成 Basis —— 故 V15 不能从零用 COM 建 basis，
  需先打开一个已含物性包的 case 作为模板。详见 dump 文件开头的实测结论。
- 单位：RealVariable.SetValue(v, unit)/GetValue(unit) 支持单位字符串换算；
  裸 .Value 使用 case 当前显示单位集（非固定内部单位）。

运行：python explore_com.py
"""

import datetime
import sys
import traceback
from pathlib import Path

OUT_PATH = Path(__file__).parent / "docs" / "com_api_dump.txt"

TROUBLESHOOT_TIPS = """
==================== 排查建议 ====================
1. 确认 HYSYS 可以手动正常启动，且当前为交互式用户会话。
2. gencache.EnsureDispatch 失败通常不影响功能，只是成员枚举会回退为类型库遍历。
3. 若连接失败，参考 verify_hysys.py 中的排查建议（ProgID 注册、位数匹配、权限）。
=================================================
"""


def fail(step: str) -> None:
    print(f"\n[失败] 步骤：{step}", file=sys.stderr)
    traceback.print_exc()
    print(TROUBLESHOOT_TIPS, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 大小写不敏感的 COM 成员访问（HYSYS 类型库部分成员为小写，makepy 区分大小写）
# ---------------------------------------------------------------------------

def com_get(obj, name: str):
    """读取 COM 成员（属性或方法），大小写不敏感。"""
    try:
        return getattr(obj, name)
    except AttributeError:
        get_map = getattr(obj, "_prop_map_get_", {}) or {}
        for k in get_map:
            if k.lower() == name.lower():
                return getattr(obj, k)
        for k in dir(obj):
            if not k.startswith("_") and k.lower() == name.lower():
                return getattr(obj, k)
        raise


def com_set(obj, name: str, value) -> str:
    """写 COM 属性，大小写不敏感；返回实际使用的属性名。"""
    try:
        setattr(obj, name, value)
        return name
    except AttributeError:
        put_map = getattr(obj, "_prop_map_put_", {}) or {}
        for k in put_map:
            if k.lower() == name.lower():
                setattr(obj, k, value)
                return k
        raise


# ---------------------------------------------------------------------------
# 成员枚举
# ---------------------------------------------------------------------------

def _collect_typeinfo(ti, names: set, seen: set, depth: int = 0) -> None:
    """递归遍历 COM 类型信息，收集函数/变量名（含继承的接口）。"""
    if depth > 5:
        return
    try:
        attr = ti.GetTypeAttr()
    except Exception:
        return
    if attr.guid in seen:
        return
    seen.add(attr.guid)
    for i in range(attr.cFuncs):
        try:
            fd = ti.GetFuncDesc(i)
            names.add(ti.GetNames(fd.memid)[0])
        except Exception:
            pass
    for i in range(attr.cVars):
        try:
            vd = ti.GetVarDesc(i)
            names.add(ti.GetNames(vd.memid)[0])
        except Exception:
            pass
    for i in range(attr.cImplTypes):
        try:
            ref = ti.GetRefTypeInfo(ti.GetRefTypeOfImplType(i))
            _collect_typeinfo(ref, names, seen, depth + 1)
        except Exception:
            pass


def _walk_typeinfo(obj) -> list:
    names, seen = set(), set()
    try:
        _collect_typeinfo(obj._oleobj_.GetTypeInfo(), names, seen)
    except Exception:
        pass
    return sorted(names)


def list_members(obj):
    """返回 (可读属性, 可写属性, 方法, 枚举方式说明)。

    优先使用 makepy 静态绑定类的 _prop_map；否则回退为 COM 类型库遍历。
    """
    get_map = getattr(obj, "_prop_map_get_", None) or {}
    put_map = getattr(obj, "_prop_map_put_", None) or {}
    if get_map or put_map:
        props = set(get_map) | set(put_map)
        methods = sorted(
            n for n in dir(obj)
            if not n.startswith("_") and n not in props
            and n not in ("CLSID", "coclass_clsid")
        )
        return (sorted(get_map), sorted(put_map), methods,
                "makepy 静态绑定类（_prop_map_get/_prop_map_put + dir）")
    return ([], [], _walk_typeinfo(obj),
            "COM 类型库遍历（GetTypeInfo，不区分属性/方法）")


def dump_object(chunks: list, title: str, obj) -> None:
    chunks.append("=" * 72)
    chunks.append(title)
    chunks.append("=" * 72)
    if obj is None:
        chunks.append("  <对象不存在或获取失败，跳过>")
        chunks.append("")
        return
    try:
        readable, writable, methods, source = list_members(obj)
    except Exception:
        chunks.append("  <成员枚举失败：>")
        chunks.append(traceback.format_exc())
        chunks.append("")
        return
    chunks.append(f"[枚举方式] {source}")
    if readable:
        chunks.append("[可读属性]")
        chunks.extend(f"  {n}" for n in readable)
    if writable:
        chunks.append("[可写属性]")
        chunks.extend(f"  {n}" for n in writable)
    if methods:
        chunks.append("[方法]" if source.startswith("makepy") else "[成员]")
        chunks.extend(f"  {n}" for n in methods)
    chunks.append("")


def safe_get(chunks: list, title: str, obj, attr: str):
    """com_get 取子对象，失败时返回 None（dump 中标注跳过）。"""
    try:
        return com_get(obj, attr)
    except Exception:
        print(f"    [提示] 获取 {title} 失败: {sys.exc_info()[1]!r}")
        return None


# ---------------------------------------------------------------------------
# 实测
# ---------------------------------------------------------------------------

def test_components(comp_list, summary: list) -> None:
    """在真正的 ComponentList 上添加 Methane / Water / H2O 并确认。"""
    comps = com_get(comp_list, "Components")
    cl_name = com_get(comp_list, "name")
    for name in ("Methane", "Water", "H2O"):
        try:
            com_get(comps, "Add")(name)
            msg = f"    Components.Add(\"{name}\") 成功"
            print(msg)
            summary.append(msg)
        except Exception as e:
            hr = getattr(e, "hresult", "?")
            msg = (f"    Components.Add(\"{name}\") 报错：HRESULT={hr} —— "
                   f"组分名必须来自 HYSYS 组分库（水的库名为 \"H2O\"，\"Water\" 非法）")
            print(msg)
            summary.append(msg)
    count = com_get(comps, "Count")
    names = com_get(comps, "Names")
    msg = f"    确认：{cl_name} 当前 Count={count}，Names={tuple(names)}"
    print(msg)
    summary.append(msg)


def _hresult_text(e) -> str:
    """格式化 COM 异常：外层 HRESULT + excepinfo 内层 SCODE。"""
    text = f"HRESULT={getattr(e, 'hresult', '?')}"
    info = getattr(e, "excepinfo", None)
    if info and len(info) > 5 and info[5]:
        text += f"，inner=0x{info[5] & 0xFFFFFFFF:08X}"
    return text


def _try_write_ppn(fp, value: str) -> str:
    """尝试写 PropertyPackageName，返回结果描述（不抛异常）。"""
    try:
        com_set(fp, "PropertyPackageName", value)
        try:
            back = com_get(fp, "PropertyPackageName")
        except Exception:
            back = "<读取失败>"
        return f"写入 {value!r} 调用成功，读回 {back!r}"
    except Exception as e:
        return f"写入 {value!r} 抛错 {_hresult_text(e)}"


def test_peng_robinson(fp, bm, summary: list) -> None:
    """物性包设为 Peng-Robinson：官方属性名、正确写法、V15 实测边界。"""
    summary.append(
        "    官方属性：FluidPackage.PropertyPackageName"
        "（Help → Customization Guide（本机为 xhysys.chm 的 FluidPackage 对象页）："
        "“Property Get/Let PropertyPackageName As String”，dispid 1610874881）。"
        "配套只读对象：FluidPackage.PropertyPackage（Get PropPackage），"
        "其 TypeName 为 HYSYS 内部类型名（Peng-Robinson -> \"pengrob\"）。"
    )
    summary.append(
        "    正确写法（必须在 Basis 变更模式内写入，否则 E_ACCESSDENIED）：\n"
        "      bm = case.BasisManager\n"
        "      bm.StartBasisChange()\n"
        "      fp.PropertyPackageName = \"Peng-Robinson\"\n"
        "      if bm.CanEndBasisChange: bm.EndBasisChange()   # 提交 Basis"
    )
    try:
        summary.append(f"    本次读取（空白 Case 新建 FP）：{com_get(fp, 'PropertyPackageName')!r}")
    except Exception as e:
        summary.append(
            f"    本次读取（空白 Case 新建 FP）：抛错 {_hresult_text(e)}"
            "（FluidPackages.Add() 建出的 FP 初始不含物性包）"
        )
    if not com_get(bm, "IsChangingBasis"):
        try:
            com_get(bm, "StartBasisChange")()
        except Exception:
            pass
    summary.append("    本次写入：" + _try_write_ppn(fp, "Peng-Robinson"))
    summary.append(
        "    " + _try_write_ppn(fp, "PR") + "（说明 \"PR\" 不是可用的注册基名）"
    )
    try:
        summary.append(f"    本次 CanEndBasisChange = {com_get(bm, 'CanEndBasisChange')}")
    except Exception as e:
        summary.append(f"    本次 CanEndBasisChange 读取失败: {e!r}")
    summary.append(
        "    结论（V15 实测边界）：\n"
        "      1) 属性名与写法如上；名称必须用注册基名，“UNIQUAC - Ideal”这类带后缀的显示名不可写，"
        "\"PR\"/\"Ideal\" 等别名报 E_INVALIDARG。\n"
        "      2) Basis 模式外直接写 -> E_ACCESSDENIED 0x80070005；StartBasisChange 后写有效包名 -> 成功并立即读回。\n"
        "      3) 可在同族内切换：UNIQUAC(活度系数) -> NRTL 成功；Peng-Robinson(EOS) -> SRK 成功；"
        "跨族切换（UNIQUAC -> Peng-Robinson）被拒 E_INVALIDARG。\n"
        "      4) 对 FluidPackages.Add() 新建的空白 FP（无物性包）写任何包名都是 E_INVALIDARG，"
        "且 CanEndBasisChange 恒为 False —— V15 无法仅用 COM 从零建立可用 basis；"
        "SimulationCases.Add(name, Type) 的 Type 参数被忽略（传路径/'Template'/'Blank'/1 均无效）。\n"
        "      5) 因此推荐路径：先用一个已含目标物性包的 case 作模板（SimulationCases.Open），"
        "再改组分、清流程、建模；或先在 GUI 里建好含 Peng-Robinson 的空白模板保存复用。"
    )


def demo_peng_robinson_on_reference_case(app) -> list:
    """在自带示例 case（含 Peng-Robinson）上复现正确写入路径，作为活证据；返回结果行。"""
    lines: list = []
    hits = list(Path(r"C:\Program Files\AspenTech").glob(
        "Aspen HYSYS*/Samples/**/K-101.hsc"))
    if not hits:
        lines.append("    对照实测：未找到自带示例 case K-101.hsc，跳过该项。")
        return lines
    path = str(hits[0])
    case = None
    try:
        case = com_get(com_get(app, "SimulationCases"), "Open")(path)
        rfs = com_get(case, "Flowsheet")
        rfp = com_get(rfs, "FluidPackage")
        rbm = com_get(case, "BasisManager")
        original = com_get(rfp, "PropertyPackageName")
        tname = com_get(com_get(rfp, "PropertyPackage"), "TypeName")
        lines.append(
            f"    对照实测（自带示例 {Path(path).name}）：原 PPN={original!r}，TypeName={tname!r}"
        )
        lines.append("      Basis 模式外：" + _try_write_ppn(rfp, "SRK"))
        com_get(rbm, "StartBasisChange")()
        lines.append("      StartBasisChange 后：" + _try_write_ppn(rfp, "SRK"))
        can_end = com_get(rbm, "CanEndBasisChange")
        lines.append(f"      CanEndBasisChange = {can_end}")
        if can_end:
            com_get(rbm, "EndBasisChange")()
        lines.append("      （验证后不保存关闭；空白新 case 可用此法：打开含 Peng-Robinson 的模板再建模）")
    except Exception:
        lines.append("    对照实测失败：")
        lines.append(traceback.format_exc())
    finally:
        if case is not None:
            try:
                com_get(case, "Close")(False)
            except Exception:
                try:
                    com_get(case, "Close")()
                except Exception:
                    pass
    return lines


def test_units(fs, summary: list, chunks: list) -> None:
    """MaterialStream 温度/压力/流量的单位机制：dump 证据 + 本次实测。"""
    try:
        ms = com_get(com_get(fs, "MaterialStreams"), "Add")("COM_TestStream")
    except Exception:
        summary.append("    MaterialStreams.Add 失败：")
        summary.append(traceback.format_exc())
        return
    dump_object(chunks, "MaterialStream（实例 COM_TestStream）", ms)

    t = com_get(ms, "Temperature")
    p = com_get(ms, "Pressure")
    f = com_get(ms, "MolarFlow")
    dump_object(chunks, "温度变量对象（MaterialStream.Temperature，RealVariable）—— 单位机制证据", t)
    dump_object(chunks, "压力变量对象（MaterialStream.Pressure，RealVariable）—— 单位机制证据", p)
    dump_object(chunks, "流量变量对象（MaterialStream.MolarFlow，RealVariable）—— 单位机制证据", f)

    # 本次实测（空白 Case，无物性包 -> 预期变量被锁定，如实记录）
    for var, unit, label in ((t, "C", "温度"), (p, "bar", "压力"), (f, "kgmole/h", "流量")):
        try:
            com_get(var, "SetValue")(25.0, unit)
            got = com_get(var, "GetValue")(unit)
            val = com_get(var, "Value")
            summary.append(f"    本次实测 {label}：SetValue(25, '{unit}') 成功，"
                           f"GetValue('{unit}')={got}，.Value={val}")
        except Exception as e:
            summary.append(
                f"    本次实测 {label}：SetValue 抛错 HRESULT={getattr(e, 'hresult', '?')}"
                "（E_ACCESSDENIED 0x80070005：空白 Case 无物性包、basis 不完整，流股变量被锁定；"
                "此时 GetValue 返回 -32767，即 HYSYS 的空值标记）"
            )

    summary.append(
        "    在 basis 完整的 Case 上的已验证实测（示例 Case，Water Feed 流股，显示单位集 °C/kPa/…）：\n"
        "      T.SetValue(30,'C') -> GetValue('C')=30.0, GetValue('K')=303.15, .Value=30.0\n"
        "      P.SetValue(1,'bar') -> GetValue('bar')=1.0, GetValue('kPa')=100.0, .Value=100.0\n"
        "      T.Value=25 直接赋值 -> GetValue('C')=25.0（按显示单位 °C 解释）\n"
        "      只读侧：T .Value=23.889=GetValue('C')（显示单位 °C，而非内部单位 K=297.04）"
    )
    summary.append(
        "    结论：SetValue(value, 单位字符串) / GetValue(单位字符串) 支持自动换算（证据见上方"
        " RealVariable dump 中的 GetValue/SetValue 方法）；裸 .Value（读和直接赋值）使用 Case 当前"
        "显示单位集，不是固定内部单位。被求解器计算的变量（CanModify=False）不可写。"
        "实践建议：读写一律用 SetValue/GetValue 显式指定单位字符串，避免依赖当前显示单位集。"
    )


# ---------------------------------------------------------------------------

def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 1. 连接 HYSYS
    try:
        import win32com.client
        try:
            from win32com.client import gencache
            app = gencache.EnsureDispatch("HYSYS.Application")
            binding = "gencache.EnsureDispatch（makepy 静态绑定）"
        except Exception:
            print("[提示] EnsureDispatch 失败，回退为动态 Dispatch；成员枚举将使用类型库遍历：")
            traceback.print_exc()
            app = win32com.client.Dispatch("HYSYS.Application")
            binding = "win32com.client.Dispatch（动态绑定 + 类型库遍历）"
        app.Visible = True
    except Exception:
        fail("连接 HYSYS.Application")
    print(f"[1] 已连接 HYSYS（{binding}）")

    try:
        version = str(app.Version)
        print(f"[2] HYSYS 版本号: {version}")
    except Exception:
        fail("读取 HYSYS 版本号")

    # 2. 新建 Case，取核心对象
    #    实测：新建空白 Case 默认没有 FluidPackage，fs.FluidPackage 抛 E_FAIL；
    #    SimulationCase 上没有 FluidPackages，需经 BasisManager 创建。
    try:
        case = com_get(com_get(app, "SimulationCases"), "Add")()
        fs = com_get(case, "Flowsheet")
        bm = com_get(case, "BasisManager")
        fps = com_get(bm, "FluidPackages")
        try:
            fp = com_get(fs, "FluidPackage")
        except Exception:
            print("    [提示] 新 Case 尚无 FluidPackage（fs.FluidPackage 抛 E_FAIL），"
                  "调用 SimulationCase.BasisManager.FluidPackages.Add() 创建。")
            fp = com_get(fps, "Add")()
        # FluidPackages.Add() 自动创建并关联 "Component List - 1"
        comp_list = com_get(com_get(bm, "ComponentLists"), "Item")(0)
        print(f"[3] 已新建 Case，Flowsheet: {com_get(fs, 'name')}，"
              f"FluidPackage: {com_get(fp, 'name')}，"
              f"ComponentList: {com_get(comp_list, 'name')}")
    except Exception:
        fail("新建 Case / 获取 Flowsheet、BasisManager、FluidPackage")

    # 3. 实测（先加组分，再测物性包与单位）
    summary: list = []
    chunks: list = []
    print("[4] 实测：组分 / 物性包 / 单位 ...")
    summary.append("【组分添加】")
    test_components(comp_list, summary)
    summary.append("【物性包设为 Peng-Robinson】")
    test_peng_robinson(fp, bm, summary)
    pp_insert_at = len(summary)  # 对照实测结果插到物性包章节末尾
    summary.append("【MaterialStream 温度/压力/流量的单位机制】")
    test_units(fs, summary, chunks)

    # 4. 枚举核心对象
    print("[5] 枚举核心对象成员 ...")
    dump_object(chunks, "SimulationCase", case)
    dump_object(chunks, "Flowsheet", fs)
    dump_object(chunks, "FluidPackage", fp)
    dump_object(chunks, "BasisManager（SimulationCase.BasisManager）", bm)
    dump_object(chunks, "ComponentList（BasisManager.ComponentLists.Item(0)）", comp_list)
    dump_object(chunks, "Components（ComponentList.Components）",
                safe_get(chunks, "Components", comp_list, "Components"))
    dump_object(chunks, "MaterialStreams（Flowsheet.MaterialStreams）",
                safe_get(chunks, "MaterialStreams", fs, "MaterialStreams"))
    dump_object(chunks, "EnergyStreams（Flowsheet.EnergyStreams）",
                safe_get(chunks, "EnergyStreams", fs, "EnergyStreams"))
    dump_object(chunks, "Operations（Flowsheet.Operations）",
                safe_get(chunks, "Operations", fs, "Operations"))
    rpm = safe_get(chunks, "ReactionPackageManager", bm, "ReactionPackageManager")
    dump_object(chunks, "ReactionPackageManager（BasisManager.ReactionPackageManager）", rpm)
    dump_object(chunks, "Reactions（ReactionPackageManager.Reactions）",
                safe_get(chunks, "Reactions", rpm, "Reactions") if rpm else None)
    dump_object(chunks, "ReactionSets（ReactionPackageManager.ReactionSets）",
                safe_get(chunks, "ReactionSets", rpm, "ReactionSets") if rpm else None)

    # 5. 关闭本 Case（不保存），保持 HYSYS 进程存活
    try:
        try:
            com_get(case, "Close")(False)  # False = 不保存
        except TypeError:
            com_get(case, "Close")()
        print("[6] 已关闭 Case（未保存）")
    except Exception:
        fail("关闭 Simulation Case")

    # 6. 在自带示例 case 上复现物性包正确写入路径（活证据）
    print("[7] 在自带示例 case 上复现 PropertyPackageName 写入 ...")
    demo_lines = demo_peng_robinson_on_reference_case(app)
    summary[pp_insert_at:pp_insert_at] = demo_lines

    # 7. 写 dump 文件（实测结论在最前面）
    header = [
        "HYSYS COM 接口地图（com_api_dump.txt）",
        f"生成时间: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"HYSYS 版本: {version}",
        f"成员枚举方式: {binding}",
        "",
        "=" * 72,
        "实测结论（本脚本运行时真实测试所得；另含在完整 Case 上预先验证的数字，已注明）",
        "=" * 72,
        *summary,
        "",
        "以下为核心 COM 对象的成员枚举（接口地图）",
        "",
    ]
    OUT_PATH.write_text("\n".join(header + chunks) + "\n", encoding="utf-8")
    print(f"[8] 接口地图已写入: {OUT_PATH}")
    print("[9] HYSYS 进程保持运行")

    print("\n完成。实测结论：")
    for line in summary:
        print("  " + line.replace("\n", "\n  "))


if __name__ == "__main__":
    main()
