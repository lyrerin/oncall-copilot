from fastmcp import FastMCP

mcp = FastMCP('probe')

@mcp.tool
def echo(text: str) -> str:
    return f'echo: {text}'

if __name__ == '__main__':
    mcp.run(transport = 'streamable-http', port = 8099)


