"""tests/test_idempotency.py —— 测幂等。

状态：骨架，未实现。
说明：docs 里没给全文，测试目标见 docs/03-代码指导-基础层.md 第 1541 行起，
      期望输出 first: False  second: True。
覆盖：同一个 idempotency_key 调两次 apply_action，
      第二次不重复执行、原样返回首次结果、返回 idempotent_replay=True。
"""
