"""app/agent/nodes/synthesize.py —— 节点：汇总证据 → 根因假设。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 865 行起（§ app/agent/nodes/synthesize.py）。
职责：把累积的全部证据汇总成排序后的 hypotheses + confidence，
      并用 pgvector 检索历史故障做参照。
"""
