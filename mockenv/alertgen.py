"""mockenv/alertgen.py —— 定时灌假告警。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 1317 行起（§6.2 告警发生器）。
职责：按间隔朝 app/api/webhook.py 发 Alertmanager 格式的假告警，
      用来端到端演示"告警进 → 调查 → 审批 → 执行"整条链路。
"""
