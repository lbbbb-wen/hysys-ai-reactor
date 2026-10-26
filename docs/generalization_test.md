# 通用性回归测试

| 用例 | 预期 | 实际 | 选型正确 | 建模结果 | 收敛 | 耗时(s) | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | gibbs | gibbs | 正确 | 成功 | 是 | 9.8 | 甲烷与空气完全燃烧，产物分布未知（涉及CO2、H2O、N2、O2及可能的CO、NOx等），无法逐一枚举明确的反应方程式， |
| 2 | conversion/equilibrium | conversion | 正确 | 成功 | 是 | 5.2 | 用户给出了明确的单程转化率15%，且反应方程式明确（N2+3H2→2NH3），无动力学参数，因此选转化率反应器（conv |
| 3 | equilibrium | equilibrium | 正确 | 失败 | - | 4.2 | HysysError: [add_components] 添加组分 'Ethylbenzene' 失败（com_error: (-2147352567, 'Exception occurred.', (0, None, None, None |
| 4 | cstr | cstr | 正确 | correct_clarification | - | 2.5 | 反应器类型 'cstr' 暂未实现建模（当前支持：conversion/equilibrium/gibbs）；cstr/pfr 需要动力学模型，后续版本再支持；进料压力（feed.P_bar）未给出（ |

## 静态硬编码检查

```
parser.py:57: 反应（典型例子：甲烷蒸汽重整 CH4+H2O=CO+3H2 与水汽变换
parser.py:62: 2. 组分一律使用 HYSYS 组分库名：水写 H2O，氢气写 Hydrogen，甲烷写
parser.py:63: Methane，一氧化碳 CO，二氧化碳 CO2，氮气 Nitrogen，氧气 Oxygen，
parser.py:64: 苯 Benzene，甲苯 Toluene，邻/间/对二甲苯 o-Xylene/m-Xylene/p-Xylene，
parser.py:66: 3. 异构体混合物（如"二甲苯"）必须拆成多个明确组分（o-Xylene、
parser.py:72: 混合物流量时填 "total"；明确说"甲烷 100 kmol/h 进料"这类组分流量
parser.py:73: 时填 "component:Methane"（用 HYSYS 库名），composition 照常给
```
