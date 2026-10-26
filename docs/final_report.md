# 项目报告：AI 驱动化工反应器智能建模

## 一、项目目标与完成情况

**目标**：用自然语言描述反应体系，系统自动完成反应器选型，并在 Aspen HYSYS V15 中完成真实建模仿真，返回收敛的、可复算的工程结果。

已实现能力：

- 三类反应器真实建模并收敛验证：conversion（转化率）、equilibrium（平衡）、gibbs（自由能最小化）
- LLM 选型与结构化：自然语言 → 严格 JSON spec（含选型理由、假设清单、缺口澄清）
- 双入口：CLI（main.py）+ Web 演示界面（FastAPI + 单文件 HTML，端口 8000）
- 信息不足时输出中文澄清问题列表，不臆造参数、不报错堆栈
- 组分别名解析 + 分子式/分子量读回校验（防同名异物误配）
- 每次建模另存 case 文件（cases/gen_*.hsc），可在 HYSYS GUI 直接打开复查

## 二、技术选型理由

**1. 为什么用 HYSYS COM（pywin32）**

| 备选 | 为什么没选 |
|---|---|
| UI 自动化（模拟键鼠/图像识别） | 依赖分辨率、界面语言、窗口焦点，脆弱；无法读回数值做断言，"跑没跑对"不可验证 |
| Aspen EDR | 换热器设计评级工具，对象模型面向换热设备，不支持反应器流程建模，与本任务无关 |
| HYSYS 内嵌脚本宏（VBScript） | 必须运行在 HYSYS 进程内，无法接 LLM 和 Web 服务，无现代语言生态与测试框架 |

COM 是 HYSYS 官方自动化接口：操作对象是组件/流股/反应器模型对象，带单位读写
（SetValue/GetValue），每个属性可写后读回校验，异常带上下文可定位，支持脱离界面的
回归测试。代价是要处理 V15 的 COM  quirks（见第五节），这些坑一旦固化进封装层
（hysys/core.py）就不再重复付出。

**2. 为什么用 LLM 做选型+结构化**

- 备选：规则引擎 / 关键词匹配。输入是自由自然语言，规则枚举不尽表述方式；
  关键词匹配无法处理歧义与缺省（"帮我模拟一个反应"该澄清而非硬猜），也无法
  生成嵌套的化学计量 JSON。
- LLM 的风险（臆造、格式漂移）用三道工程手段锁死：严格 JSON schema 约束输出；
  builder.validate_spec 前置校验（缺字段→中文澄清，不支持类型→明确报错）；
  missing_info/assumptions 分离规则 + test_parser.py 一致性断言回归。
  选型错误从未进入建模阶段：校验层在调 HYSYS 之前拦截。

**3. 为什么默认物性包选 Peng-Robinson**

PR 是烃类/气体体系的标准状态方程，覆盖全部验证场景（SMR、燃烧、歧化、脱氢均为
气相烃体系）；也是 HYSYS 最常用默认包。备选：SRK（与 PR 同族，无额外收益）、
活度系数模型 NRTL（面向极性液相体系，本验证集用不到）。spec 保留
property_package 字段，需要时按场景切换。

**4. 为什么用 FastAPI + 单文件 HTML**

需求定位是演示录屏，不是产品前端。单文件 HTML 零构建链、零框架依赖；
FastAPI 与建模代码同进程复用 main.run_pipeline（CLI 与 Web 共用同一条管线，
不复制建模逻辑），pydantic 自带请求校验。备选：Flask（同等可行，FastAPI 的
请求模型声明更省代码）；Vue/React 前端（需要构建工具链，对演示是过度工程）。

## 三、系统架构

```
用户自然语言输入（CLI 参数 / Web 文本框）
        │
        ▼
┌──────────────────────────────────────────────┐
│ LLM 选型层  parser.py                         │
│ 职责：自然语言 → 严格 JSON spec；              │
│ 体系可识别必须选型，不可识别才置 null 进澄清     │
└──────────────────────────────────────────────┘
        │  spec（JSON）
        ▼
┌──────────────────────────────────────────────┐
│ 通用 HYSYS 构建层  builder.py                 │
│ 职责：validate_spec 前置校验 → 组分别名解析 →   │
│ 建反应/流股/反应器 → 求解 → 转化率与完整性断言； │
│ 无任何场景/物质名硬编码                         │
└──────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────┐
│ HYSYS COM 自动化层  hysys/core.py + units.py  │
│ 职责：连接/模板建 case/别名解析/求解轮询/       │
│ 按名重取读数/单位换算；每步带上下文异常          │
└──────────────────────────────────────────────┘
        │
        ▼
Aspen HYSYS V15（可见窗口）→ case 另存 cases/ → 结果 JSON 回传 CLI/Web
```

关键数据结构 spec 字段（parser 输出契约）：

| 字段 | 含义 |
|---|---|
| reactor_type | conversion / equilibrium / gibbs（体系不可识别时为 null） |
| selection_reason | 选型理由全文（界面展示） |
| components | HYSYS 组分库名列表 |
| reactions[] | name（ASCII 缩写如 SMR）/ equation / stoichiometry（负=反应物）/ type / conversion / basis_component |
| feed | T_C、P_bar、flow、flow_unit、flow_basis（total 或 component:X）、composition（摩尔分率） |
| outlet_spec | 出口 T_C / P_bar 约束 |
| missing_info | 建模必需缺口（非空 → 澄清流程） |
| assumptions | AI 自行假设清单（逐条展示） |

## 四、探索过程

系统不是一次性设计完成的，实际开发共用一天半，分三个阶段：

**第 1 天上午：COM 连通性 → 最小链路打通（Conversion 反应器）**
目标：证明 Python 能驱动 HYSYS 完成一次真实建模。先跑通连接验证
（verify_hysys.py），并用探针把 COM 接口地图 dump 成文档（docs/com_api_dump.txt）。
做到：Conversion 反应器全链路（模板打开→补组分→进料→转化率→另存）。
发现：三个根基性坑——V15 类型库没有 Solver.Solve()（只能 CanSolve 复位+轮询
IsSolving）；求解前持有的 COM 引用在求解后不刷新（必须按名称重取）；以及
"假收敛"（反应器没参与计算时出口=进料，Solver 照样报收敛），由此确立了
"出口==进料即假收敛 + 硬断言兜底"的判据（docs/gotchas.md #1-#3）。还发现
COM 无法从零建 case（空白 basis 写不进物性包），定下"模板 Open + 另存"的铁律
（gotchas #5）。

**第 1 天下午：Equilibrium（SMR 双工况）→ LLM parser → 通用 builder 泛化**
目标：第二条反应器路径 + 把硬编码脚本泛化成通用系统。做到：SMR 710/600 °C
双工况收敛（docs/result_smr_710.txt、result_smr_600.txt）；parser 上线（严格
JSON + 选型规则）；builder 泛化完成（禁止场景硬编码，静态扫描佐证见
docs/generalization_test.md）；两个必测场景端到端跑通。发现：Equilibrium 反应器
热负荷读不出来，逐属性诊断（docs/equilibrium_props.txt）定位到默认 BasisUnits 为
空字符串；GUI 侧确认根因是 Reaction 的 Phase 字段为空导致 Unknown Duty，补上
Phase/Basis/Keq 写后读回校验与不完整禁报成功的完整性检查。甲苯歧化还暴露了
BaseComponent 只收组分对象的类型坑（docs/conversion_base_component.txt）。

**第 2 天上午：Gibbs 路径 → 隔离性 → 通用性回归 → 组分解析 → Web 界面**
目标：补齐第三条路径并加固、交付演示。做到：Gibbs 四用例全过
（docs/gibbs_test.txt）；三连跑隔离性修复与回归（docs/isolation_test.txt）；
4 个未见场景通用性回归（docs/generalization_test.md）；乙苯组分名问题修复并加
分子式+分子量读回校验（docs/component_resolver.txt）；转化率口径修正并回归
（docs/conversion_check.txt）；Web 演示界面（FastAPI + 单文件 HTML）上线。
发现：模板自带 Methane 导致连续运行串味；HYSYS Simulation Name 与常用名不一致
（Ethylbenzene 实为 E-Benzene）且同义词模糊匹配会误配苯乙烯；Web 层还有 COM
线程初始化与 case 窗口显示时机的两个新坑（均已在第五节列出）。

## 五、遇到的问题及解决方式

| # | 问题 | 现象 | 根因 | 解决方案 | 验证证据 |
|---|---|---|---|---|---|
| 1 | BaseComponent 类型不匹配 | 赋字符串报 -2147352571 'Type mismatch' | 该属性是 Component 对象，不是字符串 | 用 fp.Components.Item 取组分对象写入并读回校验 | docs/conversion_base_component.txt：写法 b/c 成功读回 Component 实例 |
| 2 | Conversion 属性名错误 | 写 ConversionValue 报 "has no attribute" | 类型库属性名是 Conversion | 改用 Conversion（float，百分数），写后读回比对 | docs/conversion_base_component.txt 末行；甲苯歧化转化率 0.5000（conversion_check.txt [b]） |
| 3 | -32767 空值哨兵误伤 | 大放热工况热负荷被判成"空值"丢失 | 用 v > -32766 区间判断，把 -65479 kJ/s 的真实放热值误判为哨兵 | 改为精确匹配 -32767 ± 1e-6 | docs/gibbs_test.txt 用例 A：duty=-65.47950673946865 MW 正确读出 |
| 4 | 组分残留污染 | 连续运行串味，白名单检出外来组分 ['Methane'] | 模板 blank_pr.hsc 自带 Methane，且 HYSYS 对已打开同路径文档复用内存对象 | 每次建模前 close_all_cases + 组分白名单（多余移除，清不掉 fail fast）+ 重制纯净模板 blank_clean.hsc | docs/isolation_test.txt：三连跑 TDP 最大摩尔分率偏差 0.000000、无外来组分泄漏 PASS、SMR 锚点 0.096466 在 0.0965±0.005 内 |
| 5 | 乙苯组分名错误 | Components.Add("Ethylbenzene") 失败；同义词模糊匹配会把 Styrene 误配成 Ethylbenzene | HYSYS Simulation Name 是 E-Benzene；Styrene 的 Synonym 含 EthylBenzene，且 C8H10(106.17) 与 C8H8(104.15) 分子量仅差 1.9%，单靠 2% 阈值会漏判 | data/component_aliases.json 别名表 + 分子式比对 + 分子量 1% 阈值读回校验，不符即报"组分误配" | docs/component_resolver.txt：[1] 别名解析 PASS；[2] 故意注入错误别名被正确拒绝；[3] 乙苯脱氢全链路收敛 |
| 6 | 转化率计算口径错误 | 乙苯脱氢转化率报 72.2%，与平衡常数反算不自洽 | 误用 1−出口摩尔分率；增摩尔反应下该口径系统性偏高 | 统一改为摩尔流量口径 1−F_out/F_in；多反应按各自 basis_component 计算；化学计量自检偏差 >1% 告警 | docs/conversion_check.txt [a]：修正后 0.5655；K 反算 0.5643 与 HYSYS 内置 0.5643 偏差 0.00%（component_resolver.txt [3] 留有修正前 72.2% 的对照记录） |
| 7 | 缺失信息时选型被错误置空 | 体系可识别但条件不全的输入，reactor_type 被置 null，无法展示选型 | parser 规则把"信息缺口"与"选型"耦合 | 规则拆分：体系可识别就必须选型，null 仅限体系完全不可识别；test_parser.py 加一致性断言防回归 | docs/generalization_test.md 用例 4：cstr 输入正确走澄清（correct_clarification）且选型正确；test_parser.py 全部 PASS |
| 8 | Web 层 COM 线程/窗口 | 网页建模报 'CoInitialize has not been called'；把 case.Visible 提前置真后求解器卡 IsSolving=True 直到超时 | FastAPI 在工作线程执行接口，COM 未初始化且代理不能跨套间复用；GUI 文档窗口需要 STA 消息泵，与同步 COM 调用死锁 | 所有建模请求提交到单一专用线程（启动时 CoInitialize）；case.Visible+Activate 只在求解完成后调用（core.show_case） | 修复后 Web 实测收敛：duty=39.91 MW、5 组分组成表（docs/web_accept_smr.json） |

## 六、验证结果

**场景 1：甲烷蒸汽重整**（equilibrium；进料 520 °C / 13.5 bar / 3700 kgmole/h，
CH4:H2O=1:2.7；R1 SMR：CH4+H2O=CO+3H2，R2 WGS：CO+H2O=CO2+H2）
出处：docs/result_smr_710.txt、docs/result_smr_600.txt、docs/conversion_check.txt

| 指标 | 出口 710 °C | 出口 600 °C |
|---|---|---|
| 收敛 | 是 | 是 |
| 热负荷（吸热为正） | 39.909205800846394 MW | 20.152771814475127 MW |
| 出口 x(CH4) | 0.0964847959389573 | 0.16172819145851952 |
| 出口 x(H2) | 0.4058068461173789 | 0.2701890179936955 |
| CH4 转化率（摩尔流量口径） | 0.5390 | 0.3035 |
| K(SMR) / K(WGS) | 14.92 / 1.561 | 0.5199 / 2.744 |

趋势自洽：SMR 强吸热，升温 110 °C → CH4 转化率 0.3035→0.5390、热负荷
20.15→39.91 MW，方向与幅度符合平衡移动预期。

**场景 2：甲苯歧化**（conversion，甲苯单程转化率 50%，邻:间:对≈1:2:1）
出处：docs/component_resolver.txt [4]、docs/conversion_check.txt [b]
（docs/result_toluene.txt 待补充）

| 组分 | Toluene | Benzene | m-Xylene | o-Xylene | p-Xylene |
|---|---|---|---|---|---|
| 出口摩尔分率 | 0.500000 | 0.250000 | 0.125000 | 0.062500 | 0.062500 |

与手算一致：2 分子甲苯 → 1 苯 + 1 二甲苯，50% 转化 → 甲苯 0.5、苯 0.25、
二甲苯合计 0.25（按 1:2:1 拆分 0.0625/0.125/0.0625）；实测转化率 0.5000。

**通用性测试**（4 个未见场景，同进程连续执行）
出处：docs/generalization_test.md

| 用例 | 预期选型 | 实际选型 | 建模结果 | 耗时(s) |
|---|---|---|---|---|
| 1 甲烷燃烧（产物不可枚举） | gibbs | gibbs | 成功，收敛 | 9.8 |
| 2 合成氨（给转化率 15%） | conversion/equilibrium | conversion | 成功，收敛 | 5.2 |
| 3 乙苯脱氢 | equilibrium | equilibrium | 当时因组分名失败；别名解析器修复后复测收敛（component_resolver.txt [3]） | 4.2 |
| 4 CSTR 动力学 | cstr | cstr | 正确进入澄清流程（correct_clarification），未误建模 | 2.5 |

**物理校验**：Gibbs 用例 A 元素守恒反推——C/H/O 三元素进出偏差均 0.000%
（docs/gibbs_test.txt）；builder 内置出口组成合计 1.000±0.001、热负荷有限性断言，
任一不过则 converged=false 禁报成功。verify_builder.py 输出（质量守恒偏差数值）：
待补充；docs/eb_diagnosis.txt：待补充。

## 七、已知限制与边界

- **CSTR/PFR 未实现**：需要动力学模型（反应级数、活化能、指前因子），当前收到
  此类需求返回明确的中文澄清说明，不建模、不报错堆栈。
- 组分名必须是 HYSYS 组分库 Simulation Name；别名表
  （data/component_aliases.json）只覆盖已验证组分，表外组分依赖本机 HYSYS 安装
  的组分库，缺组分时报错并给出相近名称建议。
- 物性包默认 Peng-Robinson；极性液相体系未验证。
- 反应相态默认按气相（Phase=Vapor），液相反应需扩展 spec 字段。
- V15 COM 拒绝把平衡反应 Ln(K) Source 写成 Gibbs Free Energy（写 0 读回非 0）；
  回退为用 HYSYS 内置 Gibbs 自由能数据推导的定值 K，出口温度已知时严格精确
  （结果 warning 中明示）。
- 偶发会话污染：残留/损坏的 HYSYS 进程可能导致首次求解失败，重跑即恢复；
  演示前建议关闭所有 HYSYS 窗口。

## 八、AI 协作说明

开发流程：把系统拆成"COM 基建 → 单反应器最小链路 → 诊断 → 修复 → 文档化踩坑 →
泛化 → 回归"的小步任务，每步给 AI 明确的目标、约束（禁止事项）与可执行的验收
标准（断言数值区间），AI 生成实现后立刻用真实 HYSYS 跑数验证，通过才进入下一步。

关键的纠偏与校验方式（都是"让 AI 的产出可被证伪"）：

- **K 值反算**：乙苯脱氢的转化率从出口组成反推平衡常数 K=0.5643，与 HYSYS 内置
  K=0.5643 对拍（偏差 0.00%），由此发现"1−摩尔分率"口径错误（虚报 72.2%），
  修正为摩尔流量口径 0.5655（docs/conversion_check.txt）。
- **分子式+分子量读回校验**：组分添加不信任"添加成功"的返回值，读回分子式与
  分子量与请求比对；并**故意注入错误别名**（Ethylbenzene→Styrene）验证校验器
  会拒绝（docs/component_resolver.txt [2]）。
- **假收敛判据**：把"出口==进料"作为未求解铁证，配合转化率/组成/流量/温度硬断言
  区间，杜绝"求解器报收敛就报喜"（docs/gotchas.md #3）。
- **隔离性三连跑**：同一 HYSYS 进程连跑三个场景，比较重复场景结果偏差
  （0.000000）与外来组分泄漏，逼出模板污染问题（docs/isolation_test.txt）。
- **静态扫描**：对 builder/main 做物质名硬编码扫描，确认泛化不是口号
  （docs/generalization_test.md，匹配项全部在 parser 提示词内）。
