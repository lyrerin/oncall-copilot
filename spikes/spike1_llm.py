import os
 
from langchain_openai import ChatOpenAI
 
llm = ChatOpenAI(
    model = os.getenv("LLM_MODEL", "deepseek-chat"),
    api_key = os.getenv("LLM_API_KEY"),
    base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    temperature=0
    )
msg = llm.invoke('只回复两个字：通了')
print("模型回复：", msg.content)
print("token：", msg.usage_metadata)
