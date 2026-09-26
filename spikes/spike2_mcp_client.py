import asyncio

from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8099/mcp") as client:
        tools = await client.list_tools()
        print('工具列表:', [t.name for t in tools])

        result = await client.call_tool('echo', {'text': 'hello'})
        print('工具调用结果:', result)

if __name__ == '__main__':
    asyncio.run(main())