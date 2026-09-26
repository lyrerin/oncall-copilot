"""app/api/webhook.py —— Alertmanager 告警入口。

状态：骨架，未实现。
实现位置：docs/05-代码指导-API与UI层.md 第 119 行起（§ app/api/webhook.py）。
职责：收 Alertmanager 的 POST，归一化告警，指纹去重后把 incident 丢进 ARQ 队列，
      立刻返回 202，不在请求里跑诊断。
"""
