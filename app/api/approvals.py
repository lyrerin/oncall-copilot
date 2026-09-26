"""app/api/approvals.py —— 审批 → resume。

状态：骨架，未实现。
实现位置：docs/05-代码指导-API与UI层.md 第 780 行起（§ app/api/approvals.py）。
职责：接收人工审批决定（批准/驳回 + 理由），写 approvals 表，
      然后调 graph 的 resume 把中断的流程继续跑下去。
"""
