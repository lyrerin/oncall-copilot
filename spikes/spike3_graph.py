from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class S(TypedDict):
    text: str


def upper(state: S) -> S:
    return {"text": state["text"].upper()}


def tag(state: S) -> S:
    return {"text": state["text"] + " OK"}


g = StateGraph(S)
g.add_node("upper", upper)
g.add_node("tag", tag)
g.add_edge(START, "upper")
g.add_edge("upper", "tag")
g.add_edge("tag", END)

app = g.compile()
print(app.invoke({"text": "hello graph"})["text"])
print(app.get_graph().draw_ascii())