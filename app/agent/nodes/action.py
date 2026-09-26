"""app/agent/nodes/action.py —— propose / approval / execute / verify 四个节点。

状态：骨架，未实现。
实现位置：docs/04-代码指导-Agent层.md 第 1031 行起（§ app/agent/nodes/action.py）。
职责：
  propose_action   —— 生成处置方案；
  request_approval —— 高危动作在这里 interrupt 等人批；
  execute_action   —— 调 executor MCP 工具真正执行；
  verify_action    —— 执行后验证效果。
关键点：写工具只在这里用，不暴露给诊断阶段。
"""
