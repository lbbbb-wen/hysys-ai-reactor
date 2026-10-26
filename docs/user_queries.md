# 用户指令与问题整理

> 整理项目全程中用户向 AI 提出的指令、需求与澄清请求。上一会话的原始消息已丢失，
> 由会话摘要转述；本次会话的消息为原文。当前这条整理指令本身已排除。

## 阶段一：环境搭建与 COM 基建（上一会话 · 摘要转述）

1. 创建 `.gitignore`（含 `*.hsc` 等）、`requirements.txt`、`README.md`；`verify_hysys.py`（COM 连接验证）；`explore_com.py` 生成 `docs/com_api_dump.txt` 接口地图。
2. `build_minimal.py`：Conversion 反应器全链路（模板打开、补组分、进料 520°C/13.5bar/1000 kgmole/h CH4:H2O=1:2.7、转化率 50%、另存 cases/minimal_test.hsc）。
3. `diagnose_reactor.py`：诊断"假收敛"（读旧引用问题），只诊断不改建模逻辑。
4. 修复 `build_minimal.py` 的求解段：Solver 轮询 + 求解后按名称重新获取对象 + 硬断言。
5. `docs/gotchas.md`：7 条踩坑记录（现象/原因/正确做法），UTF-8。
6. `build_smr.py`：Equilibrium 反应器 SMR 双工况（710/600°C），Keq 先探测 Gibbs 源，文献关联式回退需注明来源系数；8 项硬断言。
7. `parser.py`：自然语言 → 结构化 spec JSON（严格 schema），5 条选型规则；test_parser.py 5 用例验收。提示用户自己建 `.env` 文件。
8. 修正 `.env` 文件后重跑 parser 测试。日志和报错里涉及 key 的部分一律脱敏为 `sk-****`。
9. 把两个硬编码脚本泛化成通用构建器，禁止任何场景硬编码。第一步重构 `hysys/core.py` + `hysys/units.py`；第二步 `builder.py`；第三步 `main.py` CLI。
10. parser.py 中，`missing_info` 非空时不要臆断 reactor_type，改为输出 null；main.py 打印时 null 显示为"待定"。（后因部分场景耦合 bug 改为：体系可识别就必须选型）
11. builder.py 增加 validate_spec；parser.py 增加一致性规则；test_parser.py 增加一致性断言。
12. 流量语义歧义澄清：新增 `feed.flow_basis`（total 或 component:X）；验收总摩尔流量约 1052.4、热负荷约 -10 MW。
13. main.py 顶部 UTF-8 重配置；新增 `--file` 参数（encoding="utf-8-sig"）；统一输出函数不散落 print。
14. builder.py 第 360 行 `reactor2.HeatFlowValue` 对 Equilibrium 抛 com_error：先诊断 dump 到 `docs/equilibrium_props.txt`；多路径容错全失败则 duty=null 写 warnings；main.py 加 `--save`。
15. GUI 确认 Unknown Duty 根因是 Reaction 的 Phase 字段为空；枚举 Phase/Basis 属性写 `docs/reaction_phase_props.txt`；补 Phase=Vapor、Basis=Partial Press、Fixed Keq 每项读回校验；完整性检查任一为空返回 converged=false 禁止误报成功；求解后组成合计 1.000±0.001、duty 有限断言。
16. 甲苯歧化 BaseComponent 抛 Type mismatch：写临时脚本 dump `docs/conversion_base_component.txt`；`_set_base_component` 优先组分对象回退字符串；确认分数系数是否接受；不改 equilibrium/gibbs 路径。
17. 通用性回归 `test_generalization.py`：四个用例同进程连续执行间隔3秒；预期 gibbs/conversion 或 equilibrium/equilibrium/cstr；missing_info 判定 correct_clarification 不算失败；`docs/generalization_test.md` 表格 + `docs/gen_results/`；静态检查无物质名硬编码。
18. 乙苯 Add 失败根因：HYSYS Simulation Name 是 "E-Benzene"；更危险的是 Styrene 的 Synonym 含 EthylBenzene；建 `data/component_aliases.json`；`resolve_component` 分子量读回校验 >2% 判误配；builder 所有 Components.Add 替换；重跑用例3 转化率>30%；回归甲苯歧化与 SMR 710；结果存 `docs/component_resolver.txt`。
19. 转化率 72.2% 是错的，实际应 56.6%（反证 K 自洽）；统一改为摩尔流量 1-F_out/F_in；多反应按 basis_component 分别计算输出 `{reaction_name: conversion}`；化学计量自检偏差>1% 警告；回归 a/b/c 存 `docs/conversion_check.txt`。
20. 检查系统时间准确性；打印本地/UTC/时区/NTP 状态；偏差>5分钟则 resync；时区不是 Asia/Shanghai 则设为 China Standard Time；结果存 `docs/clock_check.txt`。
21. 在 HYSYS GUI 新建纯净模板，组分列表保持为空，另存 `templates/blank_clean.hsc`；builder 模板路径改为该文件；重跑甲苯歧化与 SMR 确认无白名单警告。

## 阶段二：Web 演示界面（本次会话 · 原文）

22. 背景：CLI 已全部打通并验证。现在加 Web 界面用于演示录屏。要求零前端框架，单文件 HTML，FastAPI 提供接口。

    任务：
    1. 创建 app.py：GET / 返回 templates/index.html；POST /api/simulate 接收 `{"text": "..."}`，调用 parser → builder，返回完整结果 JSON；异常时返回 `{"ok": false, "error": "...", "stage": "parser|builder"}`；每个请求结束后把 case 文件路径一并返回；端口 8000。
    2. 页面布局 a) 输入区：多行文本框 + 两个示例按钮（"甲烷蒸汽重整"、"甲苯歧化"）+ "开始建模" 按钮；提交后按钮禁用并显示"建模中…（正在驱动 HYSYS）" b) 选型卡片：反应器类型大字徽章 + 选型理由全文 c) 建模假设区 d) 澄清区：missing_info 非空时用醒目样式展示问题列表 e) 结果区：收敛状态、出口温度/压力、热负荷、各反应转化率、出口组成表格、case 文件路径 f) 耗时显示。
    3. 错误展示：后端异常的 stage 和 message 直接显示在结果区，不要弹 alert，不要吞异常。
    4. 中文界面；等宽字体显示数字；表格不合并单元格；窄屏下表格可横向滚动。
    5. 写 requirements.txt 和 start.bat。
    6. 保持 CLI 可用，main.py 逻辑抽成共用函数，不要在 app.py 里复制建模代码。

    验收：
    1. 浏览器输入场景 1 描述，能看到选型卡片、假设、组成表、case 路径。
    2. 输入"帮我模拟一个反应"，看到澄清问题而非报错。
    3. 输入一个未支持类型（如"给我建个 CSTR，k=0.05"），看到明确的中文说明而非堆栈。
    4. 刷新页面后功能正常，无残留状态。

23. 为什么我点了开始建模但是 HYSYS 没有任何反馈？

24. 就只出现了这个界面，没看到完整过程。（附：截图只有 HYSYS Resources 起始页，标题栏却已显示 case 名）

25. 甲烷蒸汽重整我点的时候是英文的，改成中文。

## 阶段三：反应命名优化（本次会话 · 原文）

26. 把 builder 里创建 Reaction 时的名字改成英文/数字命名（如 "R1_SMR"、"R2_WGS"、"R1_TDP"），不要用中文或太长的名字，避免 HYSYS 显示成问号。然后重跑 SMR，打开 case 确认：1. Reaction 下拉框里有两个反应，名字清晰显示 2. R1: CH4+H2O=CO+3H2，R2: CO+H2O=CO2+H2，系数正确 3. 两个反应的 Ln(K) Source 都是 Gibbs Free Energy。验收：HYSYS 里 Reaction 列表无问号，两个反应都在。

27. 反应命名使用有意义的名称，如 SMR、WGS、TDP-1 等，而不是 R1/R2。

## 统计

- 总指令条数：27（上一会话 21 条，本次会话 6 条；已排除当前整理指令）
- 其中明确的"验收"指令：Web 界面 4 条、反应命名 3 条
- 明确禁止事项：禁止场景/物质名硬编码、禁止凭空臆造数据、禁止改动现有代码、禁止吞异常、禁止用含糊限制掩盖未实现功能
