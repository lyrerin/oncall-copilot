"""app/agent/state.py —— LangGraph 的 State 定义 + reducers。

状态：骨架，未实现（文件目前为空）。
实现位置：docs/01-架构设计.md 第 152 行起（§4.1 State 定义，已给全文），
          docs/04-代码指导-Agent层.md 第 400 行起（同内容，带 import operator）。
内容：Usage（TypedDict）+ merge_usage（自定义 reducer）+ IncidentState（TypedDict, total=False）。
关键点：evidence / errors 用 operator.add，usage 用 merge_usage；
        挂了 reducer 的字段必须在图入口显式给初始值。
"""
import operator
from typing import TypedDict,Annotated,Literal

class Usage(TypedDict):
    """调用统计信息"""
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int

def merge_usage(a: Usage, b: Usage) -> Usage:
    """合并两个调用统计信息"""
    return Usage({
        "prompt_tokens": a["prompt_tokens"] + b["prompt_tokens"],
        "completion_tokens": a["completion_tokens"] + b["completion_tokens"],
        "llm_calls": a["llm_calls"] + b["llm_calls"],
    })

class IncidentState(TypedDict, total=False):
    """事件状态"""
    incident_id: str
    alert: str

    evidence_requests: list[dict]
    evidence: Annotated[list[dict], operator.add]
    investigation_round: int
    missing_info: list[str]

    hypothesis: list[dict]
    confidence: float

    plan: dict | None 
    approval: dict | None
    execution: dict | None
    verification: dict | None

    status: str
    usage: Annotated[Usage, merge_usage]
    errors: Annotated[list[dict], operator.add]
