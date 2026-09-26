"""app/api/incidents.py —— 查 incident + SSE 时间线。

状态：骨架，未实现。
实现位置：docs/05-代码指导-API与UI层.md 第 538 行起（§ app/api/incidents.py）。
职责：GET incident 详情、GET 证据列表，
      以及用 sse-starlette 把 LangGraph 的流式事件推给前端。
"""
