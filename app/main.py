"""app/main.py —— FastAPI 应用入口 + lifespan。

状态：骨架，未实现。
实现位置：docs/05-代码指导-API与UI层.md 第 11 行起（§1 app/main.py），
          docs/04-代码指导-Agent层.md 第 1740 行起（只展示 lifespan 部分）。
职责：启动时建 checkpointer / MCP client 并挂到 app.state，关闭时释放；
      注册 app/api/ 下的三个 router。
"""
