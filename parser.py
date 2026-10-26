# -*- coding: utf-8 -*-
"""
parser.py —— AI 解析层：自然语言 -> 结构化建模指令。

职责边界：本模块只做"决策"（反应器选型、组分映射、参数抽取），
不碰任何 HYSYS COM；所有 COM 调用都在 builder 层（build_minimal.py /
build_smr.py 等）。

环境配置（从 .env 读取，.env 已在 .gitignore 中，绝不入库）：
  LLM_BASE_URL  OpenAI 兼容接口地址，如 https://api.openai.com/v1
  LLM_API_KEY   API Key
  LLM_MODEL     模型名

对外接口：
  check_connectivity()          最小请求验证连通性并打印模型返回
  parse_user_input_raw(text)    返回模型原始输出字符串
  parse_user_input(text)        返回严格解析后的 dict
"""

import json
import os
import sys

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

OUTPUT_SCHEMA = """{
  "reactor_type": "conversion|equilibrium|gibbs|cstr|pfr|null",
  "selection_reason": "中文选型理由，说明为什么选它、为什么排除其他类型",
  "components": ["HYSYS库组分名"],
  "reactions": [
    {"name": "", "equation": "文字方程式",
     "stoichiometry": {"组分名": -1}, "type": "conversion|equilibrium|gibbs",
     "conversion": null, "basis_component": null}
  ],
  "feed": {"T_C": null, "P_bar": null, "flow": null, "flow_unit": "",
           "flow_basis": "total", "composition": {}},
  "outlet_spec": {"T_C": null, "P_bar": null},
  "missing_info": [],
  "assumptions": []
}"""

SYSTEM_PROMPT = """你是化工流程模拟助手，负责把用户的自然语言需求解析成结构化的
HYSYS 反应器建模指令。你只输出一个 JSON 对象：不要 markdown 代码块、
不要任何多余文字、不要注释。JSON 结构严格如下（键名与类型不得更改）：

""" + OUTPUT_SCHEMA + """

规则：
1. 反应器选型优先级：
   - 给了动力学参数或设备参数：气相选 pfr、液相选 cstr（聚合反应例外，
     若选其它类型须在 selection_reason 中说明理由）
   - 无动力学参数但给了转化率/收率/选择性：选 conversion
   - 可逆反应、无动力学也无转化率、受化学平衡控制：选 equilibrium
   - 燃烧且产物分布未知、裂解等无法逐一写出明确反应方程式的体系：
     选 gibbs。注意：只要能写出明确的可逆反应方程式，即使体系含多个
     反应（典型例子：甲烷蒸汽重整 CH4+H2O=CO+3H2 与水汽变换
     CO+H2O=CO2+H2 两个可逆反应并存），也应选 equilibrium 而不是
     gibbs；gibbs 仅用于反应方程式无法枚举、只能靠元素守恒和 Gibbs
     自由能最小化预测产物分布的场景
   - 管式反应器+动力学：pfr；釜式反应器+动力学：cstr
2. 组分一律使用 HYSYS 组分库名：水写 H2O，氢气写 Hydrogen，甲烷写
   Methane，一氧化碳 CO，二氧化碳 CO2，氮气 Nitrogen，氧气 Oxygen，
   苯 Benzene，甲苯 Toluene，邻/间/对二甲苯 o-Xylene/m-Xylene/p-Xylene，
   乙醇 Ethanol。不确定的组分名写最常见的 HYSYS 库名。
3. 异构体混合物（如"二甲苯"）必须拆成多个明确组分（o-Xylene、
   m-Xylene、p-Xylene）进入 components 与 feed.composition，
   拆分比例假设写入 assumptions；但反应方程式必须按第 11 条合并为
   单反应+分数系数，不得按异构体拆成多条反应。
4. 流量单位必须显式给出（kg/h、kmol/h 等），并如实保留用户给的单位；
   质量流量（kg/h）不得自行换算成摩尔流量。
   feed.flow_basis 表示 flow 的语义："total"=混合物总流量；
   "component:<组分名>"=某单组分的流量。用户表述含"总流量"或只给
   混合物流量时填 "total"；明确说"甲烷 100 kmol/h 进料"这类组分流量
   时填 "component:Methane"（用 HYSYS 库名），composition 照常给
   摩尔分率，并在 assumptions 写明换算后的总流量。
5. stoichiometry 中反应物系数为负、产物为正，数值为化学计量数；
   conversion 型反应填 conversion（百分数）与 basis_component；
   equilibrium/gibbs 型这两项为 null。
6. missing_info 只填建模必需的缺口（如进料流量未给出）；非必需参数
   自行做合理假设并写入 assumptions。
   选型与缺口分离：只要反应体系可识别（知道反应物/产物），即使进料
   条件不完整，也必须照常给出 reactor_type 与 selection_reason；
   reactor_type 为 null 的唯一情形是反应体系完全无法识别
   （例如只说"帮我模拟一个反应"），此时 selection_reason 写
   "信息不足，无法判断反应器类型"。禁止臆断一个占位类型。
   一致性要求：凡关键字段（components、reactions、feed.T_C、feed.P_bar、
   feed.composition）为 null 或空，且你没有给出明确数值假设，
   必须把该项加入 missing_info；missing_info 非空时不得声称可以建模。
   进料温度未给出时直接假设 25 °C 并写入 assumptions，不列入 missing_info。
   进料流量未给出时直接假设总流量 1000 kgmole/h（flow_basis="total"）
   并写入 assumptions（说明该假设不影响摩尔分率/绝热温度等结果），
   不列入 missing_info。
   出口压力未给出时假设等于进料压力（压降为 0）并写入 assumptions，
   不列入 missing_info。
7. feed.composition 的键为 HYSYS 库组分名，值为摩尔分率（数值），
   各值之和应为 1；用户给的是比例时归一化后填入，并在 assumptions 说明。
8. 用户未给定的进料压力保持 null 并进 missing_info，不要编造；
   出口压力、流量、进料温度按第 6 条的默认假设处理。
   出口温度未给定时 outlet_spec.T_C 必须保持 null——禁止假设等于
   进料温度或填任何猜测值（未指定出口温度即绝热操作，出口温度由
   能量平衡计算），并在 assumptions 写明："未指定出口温度，
   反应器按绝热操作，出口温度由能量平衡计算"。


9. 温度一律用摄氏度的数值（T_C），压力用 bar 的数值（P_bar）；
   用户给 MPa、kPa 时换算成 bar 填入并在 assumptions 说明换算。
10. 每个反应的 name 用该反应体系的通用英文缩写（大写字母/数字/-/_，
    不超过 12 个字符），例如甲烷蒸汽重整=SMR、水汽变换=WGS、
    甲苯歧化=TDP、乙苯脱氢=EBDH；同一体系多个同类反应时用 -1/-2
    编号（如 TDP-1、TDP-2）。禁止中文名和长句。
11. 同一反应的产物若含同分异构体（如二甲苯的 o/m/p 三种），必须用
    单条反应+分数系数表达（如 stoichiometry 里 o-Xylene:0.25,
    m-Xylene:0.5, p-Xylene:0.25），禁止按异构体拆成多条
    Conversion/Equilibrium 反应——HYSYS 会把多条独立转化率反应按
    顺序叠加结算，导致总转化率虚高（三条 50% 变 87.5%）。
12. components 只列进料组成与所有反应化学计量中实际出现的组分，
    禁止多列任何未参与反应的组分（如凭空加 Hydrogen、Nitrogen）。
"""


def load_env(path=ENV_PATH):
    """极简 .env 读取（KEY=VALUE，忽略注释与空行），不覆盖已有环境变量。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def get_config():
    load_env()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "缺少 LLM 配置：%s。请在项目根目录创建 .env（已在 .gitignore 中），"
            "包含 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三项。"
            % ", ".join(missing)
        )
    return (os.environ["LLM_BASE_URL"],
            os.environ["LLM_API_KEY"],
            os.environ["LLM_MODEL"])


def _client():
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("缺少 openai 包，请先执行：pip install -r requirements.txt")
    base_url, api_key, _ = get_config()
    return OpenAI(base_url=base_url, api_key=api_key)


def _chat(messages):
    """调用 OpenAI 兼容接口；优先请求 JSON 模式，不支持时自动回退。"""
    client = _client()
    _, _, model = get_config()
    kwargs = dict(model=model, messages=messages, temperature=0.0)
    try:
        resp = client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs)
    except Exception as exc:
        # 部分兼容端点不支持 response_format，去掉重试一次
        if "response_format" in str(exc) or "json" in str(exc).lower():
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    return resp.choices[0].message.content


def check_connectivity():
    """发一次最小请求验证连通性并打印模型返回；失败时明确报错。"""
    try:
        reply = _chat([
            {"role": "system", "content": "你是连通性测试探针，原样回复用户内容。"},
            {"role": "user", "content": "reply with the single word: pong"},
        ])
    except Exception as exc:
        raise RuntimeError(
            "LLM 连通性测试失败：%s: %s。请检查 .env 中的 LLM_BASE_URL 可达、"
            "LLM_API_KEY 有效、LLM_MODEL 名称正确。"
            % (type(exc).__name__, exc)
        )
    _, _, model = get_config()
    print("LLM 连通性测试通过（model=%s），模型返回：%s" % (model, reply))
    return reply


def parse_user_input_raw(text):
    """返回模型原始输出字符串（不做任何加工）。"""
    return _chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ])


def parse_user_input(text):
    """自然语言 -> 结构化建模指令 dict。输出非法 JSON 时抛带原文的异常。"""
    raw = parse_user_input_raw(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 模型偶发包裹 markdown 代码块时的兜底剥离
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            if stripped.rstrip().endswith("```"):
                stripped = stripped.rstrip()[:-3]
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "模型输出不是合法 JSON：%s\n原始输出：\n%s" % (exc, raw))
    required = ("reactor_type", "selection_reason", "components", "reactions",
                "feed", "outlet_spec", "missing_info", "assumptions")
    missing = [k for k in required if k not in data]
    if missing:
        raise RuntimeError("模型输出缺少必需键 %s：\n%s" % (missing, raw))
    # 契约兜底：反应体系完全无法识别（组分与反应都为空）时，
    # 不允许臆断 reactor_type；体系可识别则保留选型（缺口走 missing_info）
    if not data.get("components") and not data.get("reactions"):
        data["reactor_type"] = None
        data["selection_reason"] = "信息不足，无法判断反应器类型"
    return data


if __name__ == "__main__":
    try:
        check_connectivity()
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)
