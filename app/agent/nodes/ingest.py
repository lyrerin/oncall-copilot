"""app/agent/nodes/ingest.py —— 节点 1：归一化告警。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 461 行起（§ app/agent/nodes/ingest.py）。
职责：归一化告警、算指纹去重、建/复用 incident。
关键点：这里要显式返回 "evidence": []，给带 reducer 的字段一个初始值。
"""
