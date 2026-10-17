"""Offline real stdio fixture built with the official MCP SDK."""
import asyncio
import os
from pathlib import Path
import sys
import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

server = Server('cortex-fixture')
if os.environ.get('BAD_WIRE'):
    os.write(1, os.environ['BAD_WIRE'].encode() + b'\n')
if os.environ.get('PID_FILE'):
    Path(os.environ['PID_FILE']).write_text(str(os.getpid()))


def schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


TOOLS = [
    types.Tool(name='echo', description='Echo text', inputSchema=schema({'text': {'type': 'string'}}, ['text'])),
    types.Tool(name='add', description='Add numbers', inputSchema=schema({'a': {'type': 'number'}, 'b': {'type': 'number'}}, ['a', 'b'])),
    types.Tool(name='slow', inputSchema=schema()),
    types.Tool(name='fail', inputSchema=schema()),
    types.Tool(name='write_marker', inputSchema=schema()),
    types.Tool(name='crash', inputSchema=schema()),
    types.Tool(name='unsupported', inputSchema=schema({'x': {'type': 'string', 'format': 'email'}})),
]


@server.list_tools()
async def list_tools(request: types.ListToolsRequest):
    # Always paginate, to exercise the real client's discovery loop.
    if request.params and request.params.cursor == 'page2':
        return types.ListToolsResult(tools=TOOLS[3:])
    return types.ListToolsResult(tools=TOOLS[:3], nextCursor='page2')


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    if name == 'crash':
        os._exit(17)
    if name == 'slow':
        await asyncio.sleep(30)
    if name == 'fail':
        return types.CallToolResult(content=[types.TextContent(type='text', text='controlled failure')], isError=True)
    if name == 'write_marker':
        with Path(os.environ['MARKER_FILE']).open('a') as stream:
            stream.write('called\n')
        await asyncio.sleep(30)
    value = arguments.get('text') if name == 'echo' else arguments.get('a', 0) + arguments.get('b', 0)
    return types.CallToolResult(content=[types.TextContent(type='text', text=str(value))], structuredContent={'value': value})


async def main():
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


if __name__ == '__main__':
    if os.environ.get('START_DELAY'):
        import time
        time.sleep(30)
    anyio.run(main)
