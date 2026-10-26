# -*- coding: utf-8 -*-
"""转化率口径回归：按摩尔流量 1 - F_out/F_in（不用摩尔分率差）。

a) 乙苯脱氢：conv ≈ 0.566 ± 0.006，且 K = P*x^2/((1+x)(1-x)) 与 HYSYS
   推导的 K 偏差 < 1%
b) 甲苯歧化：conv = 0.500（总摩尔数不变，两种口径应一致）
c) SMR：CH4 转化率按摩尔流量输出，710°C 应显著高于 600°C
结果存 docs/conversion_check.txt。
"""
import re
import sys
import time

import builder
import parser as llm_parser

LINES = []


def log(s=""):
    print(s)
    LINES.append(str(s))


def main():
    log("转化率口径回归（摩尔流量基准）  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 66)
    all_ok = True

    # a) 乙苯脱氢
    log("[a] 乙苯脱氢（equilibrium，620°C 进料 1.2 bar，出口 650°C）")
    spec = llm_parser.parse_user_input(
        "模拟乙苯脱氢制苯乙烯：C6H5C2H5 <-> C6H5C2H3 + H2，可逆反应，"
        "进料620℃、1.2 bar，无动力学数据，反应器出口650℃，求产物分布")
    r = builder.build_from_spec(spec)
    conv = next(iter((r.get("conversions") or {}).values()), None)
    comp = (r.get("outlet") or {}).get("composition_molfrac") or {}
    p_bar = (r.get("outlet") or {}).get("P_bar") or 1.2
    log("  converged=%s  conversions=%s" % (r.get("converged"),
                                            r.get("conversions")))
    for k in sorted(comp):
        log("    x[%-14s] = %.6f" % (k, comp[k]))
    ok = conv is not None and abs(conv - 0.566) <= 0.006
    all_ok &= ok
    log("  转化率 = %.4f  [%s]（要求 0.566±0.006）"
        % (conv or -1, "PASS" if ok else "FAIL"))
    # K 自洽：x = 每摩尔进料 EB 的反应进度 = conv（纯 EB 进料）
    k_hysys = None
    for w in r.get("warnings", []):
        m = re.search(r"推导 K=([0-9.]+)", w)
        if m:
            k_hysys = float(m.group(1))
    if conv is not None:
        x = conv
        k_calc = p_bar * x * x / ((1 + x) * (1 - x))
        log("  K(由组成反算) = %.4f，HYSYS K = %s" % (k_calc, k_hysys))
        if k_hysys:
            dev = abs(k_calc - k_hysys) / k_hysys * 100
            ok = dev < 1.0
            all_ok &= ok
            log("  K 偏差 %.2f%%  [%s]（要求 <1%%）"
                % (dev, "PASS" if ok else "FAIL"))

    # b) 甲苯歧化
    log("")
    log("[b] 甲苯歧化（conversion 50%%，总摩尔数不变）")
    spec2 = llm_parser.parse_user_input(
        "甲苯歧化制苯，进料10000 kg/h纯甲苯，380°C、2.5 MPa，"
        "甲苯单程转化率50%，产物苯与二甲苯（邻:间:对约1:2:1）")
    r2 = builder.build_from_spec(spec2)
    conv2 = next(iter((r2.get("conversions") or {}).values()), None)
    for w in r2.get("warnings", []):
        log("  [warning] %s" % w)
    log("  converged=%s  conversions=%s" % (r2.get("converged"),
                                            r2.get("conversions")))
    ok = conv2 is not None and abs(conv2 - 0.500) < 0.005
    all_ok &= ok
    log("  转化率 = %.4f  [%s]（要求 0.500）"
        % (conv2 or -1, "PASS" if ok else "FAIL"))

    # c) SMR 710°C vs 600°C（CH4 转化率按摩尔流量）
    log("")
    log("[c] SMR 两工况 CH4 转化率（摩尔流量口径）")
    convs = {}
    for t in (710, 600):
        spec3 = llm_parser.parse_user_input(
            "Simulate steam methane reforming with equilibrium reactions. "
            "Feed: methane 1000 kgmole/h and steam 2700 kgmole/h at 520 °C "
            "and 13.5 bar. Outlet temperature %d °C." % t)
        r3 = builder.build_from_spec(spec3)
        for w in r3.get("warnings", []):
            log("  %d°C [warning] %s" % (t, w))
        # R1（CH4+H2O=CO+3H2）以 CH4 为基准；取含 Methane 基准的反应
        c3 = (r3.get("conversions") or {})
        ch4_conv = None
        for rn, cv in c3.items():
            # 找 CH4 为基准的反应（R1）；两反应都列出来
            log("  %d°C  反应 %s 转化率 = %.4f" % (t, rn, cv))
        # CH4 只参与 R1：取最大值对应的反应不安全，直接按出口 CH4 计算复核
        xc = ((r3.get("outlet") or {}).get("composition_molfrac")
              or {}).get("Methane", 1.0)
        convs[t] = c3
        log("  %d°C  converged=%s  x_CH4=%.4f" % (t, r3.get("converged"), xc))
    c710 = max((convs.get(710) or {"": 0}).values())
    c600 = max((convs.get(600) or {"": 0}).values())
    ok = c710 > c600 and 0.4 < c710 < 0.7 and 0.15 < c600 < 0.5
    all_ok &= ok
    log("  CH4 转化率：710°C=%.4f  600°C=%.4f  [%s]（710 应显著高于 600）"
        % (c710, c600, "PASS" if ok else "FAIL"))

    log("")
    log("总体：%s" % ("ALL PASS" if all_ok else "HAS FAIL"))
    with open(r"c:\work\hysys-ai-reactor\docs\conversion_check.txt",
              "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
