"""app/agent/llm.py —— LLM 工厂 + structured output。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 11 行起（§ app/agent/llm.py）。
职责：按配置造 ChatOpenAI（指向 DeepSeek 的 base_url），
      提供带结构化输出（with_structured_output）的封装，
      以及一个便宜的模型入口（LLM_MODEL_CHEAP）给低价值节点用。
"""
