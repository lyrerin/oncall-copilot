"""tests/conftest.py —— pytest 公共 fixture。

状态：骨架，未实现。
说明：docs 里没有给这份文件的全文，需要自己按实测需要写。
项目已配好 pytest-asyncio（见 pyproject.toml 的 asyncio_mode = "auto"），
异步测试函数直接写 async def 即可，不用加 @pytest.mark.asyncio。
预计要提供的 fixture：测试库 session、清理用的 Redis、MCP client 连接。
"""
