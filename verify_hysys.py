# -*- coding: utf-8 -*-
"""
verify_hysys.py — 验证 HYSYS COM 自动化连接

流程：
1. 通过 ProgID "HYSYS.Application" 附加到已运行的 HYSYS 实例，若无则启动新实例
2. 打印 HYSYS 版本号
3. 新建空白 Simulation Case，打印其 Flowsheet 名称
4. 关闭该 Case（不保存），保持 HYSYS 进程存活

任何一步失败都会打印完整 traceback 和排查建议，并以非零码退出。
"""

import sys
import traceback

TROUBLESHOOT_TIPS = """
==================== 排查建议 ====================
1. 确认本机已安装 Aspen HYSYS V15，且可以手动正常启动。
2. 确认 ProgID 已注册：注册表 HKEY_CLASSES_ROOT\\HYSYS.Application 应存在。
3. pywin32 未安装或损坏：重新执行 pip install pywin32。
4. 位数不匹配：若 HYSYS 为 32 位而 Python 为 64 位（或相反），COM 调用可能失败，
   请安装与 HYSYS 位数一致的 Python。
5. 权限问题：COM 自动化通常要求以交互式用户身份运行；不要在服务/计划任务会话中
   运行本脚本。必要时先手动启动一次 HYSYS，再以同一用户身份运行本脚本。
6. DCOM 配置：可在 dcomcnfg 中检查 HYSYS 的启动/访问权限。
7. 首次注册：某些环境需要以管理员身份运行一次 HYSYS 完成 COM 注册。
=================================================
"""


def fail(step: str) -> None:
    """打印失败步骤、完整 traceback 和排查建议，然后退出。"""
    print(f"\n[失败] 步骤：{step}", file=sys.stderr)
    traceback.print_exc()
    print(TROUBLESHOOT_TIPS, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # 0. 导入 pywin32
    try:
        import win32com.client
    except ImportError:
        fail("导入 pywin32（请先执行 pip install pywin32）")

    # 1. 附加已有实例，否则启动新实例
    try:
        try:
            app = win32com.client.GetActiveObject("HYSYS.Application")
            print("[1] 已附加到正在运行的 HYSYS 实例")
        except Exception:
            # 无运行中实例时 GetActiveObject 会抛 com_error，属正常情况，转而启动新实例
            app = win32com.client.Dispatch("HYSYS.Application")
            print("[1] 未检测到运行中的实例，已启动新的 HYSYS 实例")

        # COM 启动时界面可能为隐藏状态，强制置为可见
        app.Visible = True
    except Exception:
        fail("启动/附加 HYSYS.Application")

    # 2. 打印版本号
    try:
        print(f"[2] HYSYS 版本号: {app.Version}")
    except Exception:
        fail("读取 HYSYS 版本号")

    # 3. 新建空白 Simulation Case 并打印 Flowsheet 名称
    try:
        case = app.SimulationCases.Add()
        print(f"[3] 已新建空白 Simulation Case，Flowsheet 名称: {case.Flowsheet.Name}")
    except Exception:
        fail("新建 Simulation Case / 读取 Flowsheet 名称")

    # 4. 关闭该 Case（不保存），保持 HYSYS 进程存活（不调用 app.Quit）
    try:
        try:
            case.Close(False)  # False = 不保存修改
        except TypeError:
            # 部分版本 Close 不接受参数，回退为无参调用（新建未修改的 Case 不会提示保存）
            print("[4] Close(False) 签名不支持，回退为 Close()")
            case.Close()
        print("[4] 已关闭该 Case（未保存），HYSYS 进程保持运行")
    except Exception:
        fail("关闭 Simulation Case")

    print("\n验证完成：HYSYS COM 自动化连接正常。")


if __name__ == "__main__":
    main()
