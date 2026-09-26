"""app/obs/tracing.py —— Langfuse 链路追踪。

状态：骨架，未实现。
实现位置：docs/06-可观测与评估.md 第 25 行起（§ app/obs/tracing.py）。
职责：初始化 Langfuse client / callback handler，
      把 LangGraph 的每个节点调用都挂上 trace，方便回放与对账成本。
注意：LANGFUSE_ENABLED=false 时要能干净地降级成 no-op。
"""
