"""app/agent/graph.py —— 图装配 + 编译。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 1491 行起（§ app/agent/graph.py）。
职责：把 app/agent/nodes/ 下的节点加进 StateGraph，连边和条件边，
      挂 checkpointer（AsyncPostgresSaver）后 compile。
关键点：入口处要给带 reducer 的字段显式初始值（"evidence": []）。
"""
