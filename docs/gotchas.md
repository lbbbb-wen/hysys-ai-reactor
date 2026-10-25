# HYSYS V15 COM 自动化踩坑记录（gotchas）

本文记录本项目实测确认的坑。每条包含：现象、原因、正确做法。
测试环境：Windows + Aspen HYSYS V15 (41.0) + Python 3.12 + pywin32。

---

## 1. V15 类型库无 Solver.Solve() 方法

- **现象**：调用 `case.Solver.Solve()` 抛 `AttributeError`；底层 `GetIDsOfNames("Solve")` 返回 `Unknown name` (0x80020006)。
- **原因**：HYSYS V15 类型库中 Solver 对象只暴露 `CanSolve`（读/写）、`IsSolving`、`IsForgetting`、`Mode` 等属性，没有 `Solve()` 方法。makepy 生成的静态绑定里没有，动态 IDispatch 查询也找不到。
- **正确做法**：先 `hasattr(solver, "Solve")` 尝试调用（兼容其它版本）；不存在时复位求解器强制完整重算，再轮询直到空闲：

```python
solver = case.Solver
solver.CanSolve = False
solver.CanSolve = True          # 触发完整重算
while solver.IsSolving:         # 轮询直到求解结束
    time.sleep(0.2)
```

参考：[build_minimal.py](../build_minimal.py) 的 `solve()`。

## 2. 求解前持有的流股/反应器 COM 引用在求解后不刷新

- **现象**：求解明明收敛（IsSolving=False、反应器显示已计算），但从求解前保存的流股对象读数，拿到的仍是求解前的旧值（进料温度 520 °C、进料组成、1000 kgmole/h）。
- **原因**：HYSYS 求解器在重算后会替换/失效旧的变量对象，先前取得的 COM 代理引用不自动指向新结果。
- **正确做法**：求解完成后，一律按名称从集合重新获取对象再读数，禁止使用求解前保存的引用：

```python
vapour  = flowsheet.MaterialStreams.Item("Outlet-Vapour")
liquid  = flowsheet.MaterialStreams.Item("Outlet-Liquid")
reactor = flowsheet.Operations.Item("Reactor-1")
```

## 3. 未求解时出口值等于进料（"假收敛"判据）

- **现象**：Solver 报已收敛，但出口温度=进料温度、出口组成=进料组成、CH4 转化率 0%。
- **原因**：反应器未真正参与计算（反应集未绑定、反应未激活、产物未连全等）时，HYSYS 把反应器当直通容器，出口直接等于进料。
- **正确做法**：把"出口 == 进料"当作未求解/假收敛的判据。用硬断言兜底：转化率、出口组成、总流量、出口温度、质量守恒全部落在实测区间内才判 PASS（见 build_minimal.py 的 [8] 断言段）。诊断时用 diagnose_reactor.py 核对反应器类型、ReactionSet 绑定与反应激活状态。

## 4. Conversion Reactor 固定有汽/液两个产物出口，必须同时连接

- **现象**：只连 `VapourProduct` 时反应器不求解，出口全部为空值（-32767）；读 `AttachedProducts` 恒为两条。
- **原因**：HYSYS 的 Conversion Reactor 结构上是一个带反应的两相分离器，固有限定 `VapourProduct` 和 `LiquidProduct` 两个产物连接，少一个就无法构成可求解的自由度。
- **正确做法**：两条出口都创建流股并连接：

```python
reactor.VapourProduct = vapour_stream
reactor.LiquidProduct = liquid_stream
```

出口总组成 = 两股出口按摩尔流量加权。

## 5. 禁止 SimulationCases.Add() 从零建 case，必须用模板

- **现象**：`SimulationCases.Add()` 新建的空白 case 中，`FluidPackages.Add()` 建出的物性包不含任何 PropertyPackage；写 `PropertyPackageName` 报 `E_INVALIDARG (0x80070057)`，`CanEndBasisChange` 恒为 False，永远无法进入模拟环境。
- **原因**：V15 的 COM 接口无法从零建立可用的 Basis（`SimulationCases.Add(name, Type)` 的 Type 参数被完全忽略）。
- **正确做法**：`SimulationCases.Open()` 打开已含 Peng-Robinson 的模板 `templates/blank_pr.hsc`，在其上建模；先 `SaveAs()` 另存工作副本再操作，绝不覆盖模板（脚本用 SHA-256 校验模板未被改写）。

## 6. 组分名必须用 HYSYS 库名

- **现象**：`Components.Add("Water")` 报 `HRESULT=-2147352567`；`Components.Add("H2O")` 成功。
- **原因**：COM 接口只接受 HYSYS 组分库的内部名称，不接受俗名/别名。
- **正确做法**：使用库名映射——Methane / H2O / CO / CO2 / Hydrogen / Nitrogen / Oxygen / Benzene / Toluene / o-Xylene / m-Xylene / p-Xylene。添加失败时打印报错并停下来让人确认，不要静默重试其它名字。另外：组分与物性包的修改必须在 `bm.StartBasisChange()` / `bm.EndBasisChange()` 事务内（模式外写物性包报 `E_ACCESSDENIED`）。

## 7. PowerShell 5.1 下 Add-Content 写中文会乱码

- **现象**：用 `Add-Content` / `Out-File`（不带 `-Encoding utf8`）写含中文的文本，再读回来变成乱码。
- **原因**：PowerShell 5.1 默认用系统 ANSI 代码页写文件；而很多工具按 UTF-8 读。反向地，PS 5.1 的 `Get-Content` 对无 BOM 的 UTF-8 文件也按 ANSI 解析，同样乱码。
- **正确做法**：文档写入一律用 Python（`open(..., encoding="utf-8")`）或编辑器/Write 工具，不要用 PowerShell 重定向/Add-Content 写中文。若要在 PS 5.1 里 `Get-Content` 正常显示，保存为带 BOM 的 UTF-8。

---

## 附：其它实测要点

- 流股变量读写一律 `SetValue(值, "单位")` / `GetValue("单位")`；`GetValue` 返回 `-32767` 表示空值；`CanModify=False` 的变量不可写。
- 反应类型字符串用内部名 `"conversionrxn"`，反应器用 `"ConversionReactorOp"`（不是显示名）。
- 反应必须先 `ReactionSet.ActiveReactions.Add(rxn)` 才能设置 `BaseComponent`；`ReactionSet` 必须先 `AssociateFluidPackage(fp)` 才能赋给反应器的 `ReactionSet`。
- COM 异常的外层 HRESULT 恒为 `-2147352567 (DISP_E_EXCEPTION)`，真实错误码在 `e.excepinfo[5]`。
