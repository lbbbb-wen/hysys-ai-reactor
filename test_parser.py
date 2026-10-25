# -*- coding: utf-8 -*-
"""
test_parser.py —— 验证 parser.parse_user_input 的 5 个用例。

每个用例：打印模型原始输出 + json.loads 直接解析的结果，然后做断言。
验收：
  a. 甲烷蒸汽重整 -> equilibrium
  b. 甲苯歧化 -> conversion，flow=10000 且 flow_unit=kg/h，二甲苯拆三种异构体
  c. 甲烷完全燃烧、产物分布未知 -> gibbs
  d. 乙醇液相反应+动力学参数 -> cstr
  e. "帮我模拟一个反应" -> missing_info 非空
"""

import json
import sys

import parser

CASES = [
    {
        "id": "a",
        "text": "模拟甲烷蒸汽重整反应器，进料甲烷:水=1:2.7（摩尔比），"
                "进料 520°C、13.5 bar，总流量 3700 kmol/h，"
                "分别看出口 710°C 和 600°C 两个工况",
        "expect_type": "equilibrium",
    },
    {
        "id": "b",
        "text": "甲苯歧化制苯和二甲苯，进料 10000 kg/h 纯甲苯，"
                "380°C、2.5 MPa，甲苯转化率 50%",
        "expect_type": "conversion",
        "expect_flow": 10000,
        "expect_flow_unit": "kg/h",
        "expect_xylene_isomers": True,
    },
    {
        "id": "c",
        "text": "甲烷完全燃烧，进料甲烷和空气，产物分布未知，帮我建反应器模型",
        "expect_type": "gibbs",
    },
    {
        "id": "d",
        "text": "乙醇液相脱水生成乙烯，釜式反应器，动力学参数："
                "k = 2.1e5·exp(-8000/T) 1/s，反应级数 1 级，液相",
        "expect_type": "cstr",
    },
    {
        "id": "e",
        "text": "帮我模拟一个反应",
        "expect_type": None,  # 不限制选型，只要求 missing_info 非空
        "expect_missing_info": True,
    },
]


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                            ("（%s）" % detail) if detail else ""))
    return ok


def main():
    # 先发一次最小请求验证连通性并打印模型返回；失败时明确报错退出
    try:
        parser.check_connectivity()
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)

    failures = []
    for case in CASES:
        print("\n" + "=" * 72)
        print("用例 %s：%s" % (case["id"], case["text"]))
        print("=" * 72)
        raw = parser.parse_user_input_raw(case["text"])
        print("--- 模型原始输出 ---")
        print(raw)
        print("--- json.loads 直接解析 ---")
        try:
            data = json.loads(raw)  # 验收点：必须能直接解析
        except json.JSONDecodeError as exc:
            check("JSON 可直接解析", False, str(exc))
            failures.append(case["id"])
            continue
        print(json.dumps(data, ensure_ascii=False, indent=2))

        ok = True
        if case["expect_type"]:
            ok &= check("reactor_type == %s" % case["expect_type"],
                        data.get("reactor_type") == case["expect_type"],
                        "实测 %s" % data.get("reactor_type"))
        if case.get("expect_flow") is not None:
            ok &= check("feed.flow == %s" % case["expect_flow"],
                        data.get("feed", {}).get("flow") == case["expect_flow"],
                        "实测 %r" % data.get("feed", {}).get("flow"))
        if case.get("expect_flow_unit"):
            ok &= check("feed.flow_unit == %s" % case["expect_flow_unit"],
                        data.get("feed", {}).get("flow_unit") == case["expect_flow_unit"],
                        "实测 %r" % data.get("feed", {}).get("flow_unit"))
        if case.get("expect_xylene_isomers"):
            comps = data.get("components", [])
            isomers = {"o-Xylene", "m-Xylene", "p-Xylene"}
            ok &= check("二甲苯拆为三种异构体",
                        isomers.issubset(set(comps)),
                        "components=%s" % comps)
        if case.get("expect_missing_info"):
            ok &= check("missing_info 非空",
                        bool(data.get("missing_info")),
                        "missing_info=%s" % data.get("missing_info"))
        if not ok:
            failures.append(case["id"])

    # 附带验证 parse_user_input（含兜底剥离与键校验）能正常返回 dict
    print("\n--- parse_user_input 接口抽查（用例 a）---")
    data = parser.parse_user_input(CASES[0]["text"])
    ok = isinstance(data, dict) and data.get("reactor_type") == "equilibrium"
    check("parse_user_input 返回 dict 且选型正确", ok,
          "reactor_type=%s" % data.get("reactor_type"))
    if not ok:
        failures.append("a-api")

    print("\n" + "=" * 72)
    if failures:
        print("FAIL：未通过用例 %s" % ", ".join(failures))
        sys.exit(1)
    print("全部用例通过：PASS")


if __name__ == "__main__":
    main()
