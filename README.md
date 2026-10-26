# hysys-ai-reactor

AI 驱动的化工反应器智能建模：用自然语言描述反应体系，系统自动完成反应器选型并在 Aspen HYSYS 中建模仿真，返回收敛结果。

## 系统架构

```
自然语言描述
    │
    ▼
┌─────────────────────┐   parser.py：LLM（DeepSeek）按严格 schema 输出
│  LLM 选型层          │   reactor_type / components / reactions / feed /
└─────────────────────┘   outlet_spec / missing_info / assumptions
    │  结构化 JSON spec
    ▼
┌─────────────────────┐   builder.py：validate_spec 前置校验（缺信息→澄清），
│  通用 HYSYS 构建层   │   组分别名解析、反应/流股/反应器创建、求解、
└─────────────────────┘   转化率与完整性断言；无任何场景/物质名硬编码
    │
    ▼
┌─────────────────────┐   hysys/core.py + hysys/units.py：pywin32 封装
│  COM 自动化层        │   HYSYS COM（连接、模板建 case、读写流股、
└─────────────────────┘   求解轮询、按名重取对象、单位换算）
    │
    ▼
Aspen HYSYS V15（可见窗口，case 另存至 cases/）
```

## 技术选型与理由

**pywin32 + HYSYS COM**，而非 UI 自动化（模拟鼠标键盘/图像识别）：

- COM 是 HYSYS 官方自动化接口，操作对象是组件/流股/反应器等模型对象，确定性可重复；UI 自动化依赖分辨率、界面语言、窗口焦点，脆弱且无法读回校验。
- COM 支持带单位的读写（`SetValue(v, "bar")` / `GetValue("kJ/s")`）和逐属性读回验证，工程数据可信。
- 每一步 COM 调用都包在带上下文的异常里（哪一步、什么错），可定位、可回归。

实测的 V15 COM 约束（详见 docs/gotchas.md）：禁止从零建 case（从模板 Open 再另存）、V15 无 `Solver.Solve()`（CanSolve 复位 + 轮询 IsSolving）、`-32767` 为空值哨兵、求解后必须按名称重新获取对象再读数。

## 环境要求

- Windows + Aspen HYSYS V15（本仓库所有结果均在 V15 实测）
- Python 3.12
- Python 依赖（requirements.txt）：`pywin32`、`fastapi`、`uvicorn`、`openai`、`pydantic`
- LLM 配置：复制 `.env.example` 为 `.env`，填写
  `LLM_BASE_URL`（OpenAI 兼容端点，开发用 https://api.deepseek.com/v1）、
  `LLM_API_KEY`（只放 key 本体）、`LLM_MODEL`（开发用 deepseek-chat）

## 安装与运行

```bat
git clone <repo> && cd hysys-ai-reactor
pip install -r requirements.txt
:: 配置 .env 后，双击 start.bat：启动 Web 服务并自动打开 http://localhost:8000
```

COM 环境自检：`python verify_hysys.py`（连接/版本/模板检查）。

## 使用方法

### CLI

```bat
python main.py "模拟甲烷蒸汽重整，按平衡反应建模。进料：甲烷 1000 kgmole/h、水蒸气 2700 kgmole/h，520 °C、13.5 bar。出口温度 710 °C。"
python main.py --file prompts\smr.txt   :: 从 UTF-8 文件读提示词
python main.py "..." --save result.json :: 完整 spec+结果存 JSON
```

输出节选（与 docs/result_smr_710.txt 一致）：

```
[类型] equilibrium
[收敛] 是
[出口] T = 710.00 °C, P = 13.5000 bar
[热负荷] 39.9092 MW（正值=吸热）
[转化率] SMR：53.90%（按基准组分摩尔流量）
[case 文件] ...\cases\gen_YYYYMMDD_HHMMSS_equilibrium.hsc
```

### Web

双击 `start.bat` 或 `python app.py`，浏览器打开 http://localhost:8000 ：
多行输入框 + 两个内置示例（甲烷蒸汽重整 / 甲苯歧化），提交后页面展示
选型徽章与理由、AI 假设清单、收敛状态、出口温度/压力/热负荷、各反应转化率、
出口组成表（摩尔/质量分率）、case 文件路径与耗时；信息不足时展示澄清问题而不是报错。
建模结束后 HYSYS 自动弹出该 case 窗口，可直接在 GUI 中查看流程图。

## 验证结果

### 必测场景

场景 1：甲烷蒸汽重整（equilibrium，CH4+H2O=CO+3H2、CO+H2O=CO2+H2；
进料 520 °C / 13.5 bar / 总 3700 kgmole/h，CH4:H2O=1:2.7）
出处：docs/result_smr_710.txt、docs/result_smr_600.txt、docs/conversion_check.txt

| 指标 | 710 °C 出口 | 600 °C 出口 |
|---|---|---|
| 收敛 | 是 | 是 |
| 热负荷（吸热为正） | 39.91 MW | 20.15 MW |
| 出口 x(CH4) | 0.0965 | 0.1617 |
| 出口 x(H2) | 0.4058 | 0.2702 |
| CH4 转化率（摩尔流量口径） | 53.90% | 30.35% |
| 平衡常数 K（710/600 °C 定值） | 14.92 / 1.561 | 0.5199 / 2.744 |

场景 2：甲苯歧化（conversion，单程转化率 50%，产物苯:二甲苯，邻:间:对≈1:2:1）
出处：docs/component_resolver.txt [4]、docs/conversion_check.txt [b]
（docs/result_toluene.txt 待补充）

| 指标 | 数值 |
|---|---|
| 收敛 | 是 |
| 出口 x(Toluene) / x(Benzene) | 0.5000 / 0.2500 |
| 出口 x(m-/o-/p-Xylene) | 0.1250 / 0.0625 / 0.0625 |
| 甲苯转化率 | 50.00% |

### 其它实测

- 乙苯脱氢（equilibrium，650 °C 出口）：转化率 56.55%；由出口组成反算 K=0.5643，
  与 HYSYS 内置 K=0.5643 偏差 0.00%（docs/conversion_check.txt [a]）。
- Gibbs 反应器四用例全过（docs/gibbs_test.txt）：纯氧等温 800 °C 放热 -65.48 MW、
  产物仅 CO2+H2O；λ=2 绝热 1205.8 °C；λ=1 参照 1955.2 °C（1700–2100 °C 区间）；
  欠氧 1000 °C 出现 CO 0.2470 / H2 0.4197；元素守恒偏差 0.000%。
- 通用性回归（docs/generalization_test.md）：4 个未训练场景连续执行，
  选型全部正确（gibbs / conversion / equilibrium / cstr），3 个建模成功，
  cstr 用例正确进入澄清流程；静态扫描确认 builder/main 无物质名硬编码
  （匹配项全部在 parser 的提示词内）。其中乙苯用例当时因组分名问题失败，
  已由别名解析器修复并复测通过（docs/component_resolver.txt）。
- 组分别名解析（docs/component_resolver.txt）：Ethylbenzene → E-Benzene
  （分子式+分子量读回校验）；注入错误别名（Ethylbenzene→Styrene）能判误配并报错。
- verify_builder.py 输出：待补充。

## 已知限制

- 仅支持 conversion / equilibrium / gibbs 三类反应器；cstr / pfr 需要动力学模型，
  当前返回明确的中文澄清说明，不会报错堆栈。
- 组分名必须是 HYSYS 组分库的 Simulation Name（水=H2O、乙苯=E-Benzene 等）；
  常用别名见 data/component_aliases.json，表外组分依赖本机 HYSYS 安装的组分库。
- V15 COM 拒绝把平衡反应的 Ln(K) Source 写成 Gibbs Free Energy（写 0 读回非 0）。
  回退方案：用 HYSYS 内置 Gibbs 自由能数据推导定值 K，出口温度已知时严格精确
  （结果里以 warning 明示）。
- 反应相态默认按气相（Phase=Vapor）；液相体系需扩展 spec 字段。
- 反应名统一为 ASCII 缩写（SMR、WGS、TDP-1 等，由 LLM 按 parser 规则命名，
  builder 做清洗兜底；纯中文名退化为 RXN1/RXN2），避免 HYSYS 界面显示问号。
- 偶发会话污染：残留/损坏的 HYSYS 进程可能导致首次求解失败，重跑即恢复；
  演示前建议关闭所有 HYSYS 窗口。
- 转化率按基准组分摩尔流量口径（1 − F_out/F_in），增摩尔反应下与
  "1 − 摩尔分率" 口径差异显著（乙苯脱氢：0.566 vs 0.722），本仓库取前者。

## 目录结构

```
app.py            Web 服务（FastAPI，GET / + POST /api/simulate，端口 8000）
main.py           CLI 入口；run_pipeline() 为 CLI/Web 共用管线
parser.py         LLM 选型层：自然语言 -> 严格 JSON spec
builder.py        通用构建器：spec -> HYSYS 建模/求解/结果（无场景硬编码）
hysys/core.py     COM 基建：连接、模板建 case、别名解析、求解、读数
hysys/units.py    单位与分子量换算
templates/        blank_clean.hsc（纯净 case 模板）、index.html（Web 页面）
data/component_aliases.json   组分别名表（canonical -> Simulation Name + 分子式）
start.bat         双击启动 Web 并打开浏览器
requirements.txt  Python 依赖
verify_hysys.py   COM 环境自检；explore_com.py 生成接口地图
test_*.py         回归测试（parser/gibbs/通用性/别名/转化率口径）
docs/             验证报告与踩坑记录（gotchas.md、com_api_dump.txt 等）
cases/            每次建模另存的 gen_*.hsc
```
