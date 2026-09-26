"""app/services/dedup.py —— Redis 去重与幂等锁。

状态：骨架，未实现。
实现位置：docs/03-代码指导-基础层.md 第 370 行起（§4 Redis：去重与锁）。
职责：告警指纹去重（SET NX）、执行动作的 idempotency_key 幂等锁。
自测：同一个 key 调两次，第二次应返回 idempotent_replay=True（同文档第 1541 行起）。
"""
