# -*- coding: utf-8 -*-
"""app.py —— Web 演示界面：FastAPI + 单文件 HTML（零前端框架）。

  GET  /               返回 templates/index.html
  POST /api/simulate   {"text": "..."} -> parser -> builder -> 结果 JSON
                       （异常返回 {"ok": false, "error", "stage"}）

与 CLI 共用 main.run_pipeline，不复制建模代码。
启动：python app.py  或  start.bat（自动开浏览器 http://localhost:8000）
"""
import os
import sys

# 与 main.py 一致：Windows 控制台 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from main import run_pipeline
from hysys import units

# COM 线程模型约束：pythoncom 要求每个使用 COM 的线程先 CoInitialize，
# 且 win32com 对象不能跨套间线程复用（core.py 的 _app 是模块级缓存）。
# 因此所有建模请求统一提交到这一个专用线程执行（COM 只初始化一次、
# 请求串行——HYSYS 本身也是单实例求解，串行符合预期）。
import concurrent.futures


def _init_com():
    import pythoncom
    pythoncom.CoInitialize()
    return True


_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_executor.submit(_init_com).result()  # 启动时完成 COM 初始化，失败立即暴露

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "templates", "index.html")

app = FastAPI(title="AI 驱动的 HYSYS 反应器智能建模")


class SimRequest(BaseModel):
    text: str


@app.get("/")
def index():
    return FileResponse(INDEX, media_type="text/html; charset=utf-8")


def _with_mass_fraction(comp):
    """由摩尔分率补算质量分率（MW 未知的组分质量分率为 None）。"""
    rows = []
    denom = 0.0
    for name, x in comp.items():
        mw = units.MOLECULAR_WEIGHTS.get(name)
        part = (x or 0.0) * mw if mw else None
        rows.append({"component": name, "mol_frac": x,
                     "mw": mw, "_mass_part": part})
        if part:
            denom += part
    for r in rows:
        r["mass_frac"] = (r["_mass_part"] / denom
                          if (r["_mass_part"] and denom > 0) else None)
        r.pop("_mass_part")
    return rows


@app.post("/api/simulate")
def simulate(req: SimRequest):
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"ok": False, "stage": "parser",
                             "error": "输入为空，请填写反应描述"})
    pipe = _executor.submit(run_pipeline, text).result()
    if pipe["error"]:
        return JSONResponse({"ok": False, "stage": pipe["stage"],
                             "error": pipe["error"],
                             "timings": pipe["timings"]})
    spec, result = pipe["spec"], pipe["result"]
    body = {"ok": True, "spec": spec, "result": result,
            "timings": pipe["timings"]}
    comp = ((result or {}).get("outlet") or {}).get("composition_molfrac")
    if comp:
        body["outlet_table"] = _with_mass_fraction(comp)
    return JSONResponse(body)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
