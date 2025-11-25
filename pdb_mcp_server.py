#!/usr/bin/env python3
"""MCP server for debugging Python applications in Docker containers using Pdb."""

import asyncio
import json
import shlex
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


class PdbSession:
    """Manages a single Pdb debugging session in a Docker container."""

    def __init__(
        self,
        service: str,
        compose_file: str | None = None,
        command: str | None = None,
    ) -> None:
        self.service = service
        self.compose_file = compose_file or 'docker-compose.yml'
        self.command = command
        self.process: asyncio.subprocess.Process | None = None
        self.output_buffer: list[str] = []
        self.output_task: asyncio.Task[None] | None = None
        self.is_running = False

    async def start(self) -> dict[str, Any]:
        """Start the Docker Compose session with debugging enabled."""
        if self.is_running:
            return {'success': False, 'error': 'Session already running'}

        cmd = ['docker', 'compose', 'run', '--rm', self.service]
        if self.command:
            cmd.extend(shlex.split(self.command))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.is_running = True
            self.output_task = asyncio.create_task(self._read_output())

            # Wait for initial Pdb prompt
            await self._wait_for_prompt(timeout=10.0)

            return {
                'success': True,
                'pid': self.process.pid,
                'message': 'Pdb session started successfully',
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def send_command(self, command: str) -> dict[str, Any]:
        """Send a command to the Pdb session and return the output."""
        if not self.is_running or not self.process or not self.process.stdin:
            return {'success': False, 'error': 'No active session'}

        # Check if process is still alive
        if self.process.returncode is not None:
            return {
                'success': False,
                'error': f'Process has terminated with code {self.process.returncode}',
            }

        try:
            # Clear buffer before sending command
            self.output_buffer.clear()

            # Send command
            self.process.stdin.write(f'{command}\n'.encode())
            await self.process.stdin.drain()

            # Wait for response
            output = await self._wait_for_prompt(timeout=5.0)

            return {
                'success': True,
                'command': command,
                'output': output,
                'has_prompt': '(Pdb)' in output,
            }
        except Exception as e:
            error_details = f'{type(e).__name__}: {str(e)}'
            return {'success': False, 'error': error_details}

    async def stop(self) -> dict[str, Any]:
        """Stop the Pdb session and clean up resources."""
        if not self.is_running:
            return {'success': False, 'error': 'No active session'}

        try:
            # Try graceful shutdown
            if self.process and self.process.stdin:
                try:
                    self.process.stdin.write(b'quit\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    if self.process:
                        self.process.kill()
                        await self.process.wait()

            # Cancel output task
            if self.output_task:
                self.output_task.cancel()
                try:
                    await self.output_task
                except asyncio.CancelledError:
                    pass

            self.is_running = False
            return {'success': True, 'message': 'Session stopped successfully'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def _read_output(self) -> None:
        """Read output from the process and buffer it."""
        if not self.process or not self.process.stdout:
            return

        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode('utf-8', errors='replace')
                self.output_buffer.append(decoded)
        except asyncio.CancelledError:
            pass

    async def _wait_for_prompt(self, timeout: float = 5.0) -> str:
        """Wait for the Pdb prompt to appear in output."""
        start_time = asyncio.get_event_loop().time()
        collected_output: list[str] = []

        while asyncio.get_event_loop().time() - start_time < timeout:
            if self.output_buffer:
                line = self.output_buffer.pop(0)
                collected_output.append(line)
                if '(Pdb)' in line:
                    return ''.join(collected_output)
            await asyncio.sleep(0.1)

        # Timeout reached, return what we have
        return ''.join(collected_output)


# Global session manager
_active_session: PdbSession | None = None


def _get_session() -> PdbSession | None:
    """Get the currently active Pdb session."""
    return _active_session


def _set_session(session: PdbSession | None) -> None:
    """Set the active Pdb session."""
    global _active_session
    _active_session = session


async def handle_start_pdb_session(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the start_pdb_session tool call."""
    service = arguments.get('service')
    compose_file = arguments.get('compose_file')
    command = arguments.get('command')

    if not service:
        result = {'success': False, 'error': 'service parameter is required'}
        return [TextContent(type='text', text=json.dumps(result, indent=2))]

    # Stop existing session if any
    existing_session = _get_session()
    if existing_session and existing_session.is_running:
        await existing_session.stop()

    # Create and start new session
    session = PdbSession(service=service, compose_file=compose_file, command=command)
    _set_session(session)

    result = await session.start()
    return [TextContent(type='text', text=json.dumps(result, indent=2))]


async def handle_send_pdb_command(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the send_pdb_command tool call."""
    command = arguments.get('command')

    if not command:
        result = {'success': False, 'error': 'command parameter is required'}
        return [TextContent(type='text', text=json.dumps(result, indent=2))]

    session = _get_session()
    if not session:
        result = {'success': False, 'error': 'No active session. Start one first.'}
        return [TextContent(type='text', text=json.dumps(result, indent=2))]

    result = await session.send_command(command)
    return [TextContent(type='text', text=json.dumps(result, indent=2))]


async def handle_stop_pdb_session(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle the stop_pdb_session tool call."""
    session = _get_session()
    if not session:
        result = {'success': False, 'error': 'No active session to stop'}
        return [TextContent(type='text', text=json.dumps(result, indent=2))]

    result = await session.stop()
    _set_session(None)
    return [TextContent(type='text', text=json.dumps(result, indent=2))]


async def main() -> None:
    """Run the MCP server."""
    server = Server('pdb-mcp-server')

    # Register tools
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name='start_pdb_session',
                description='Start a Pdb debugging session in a Docker container',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'service': {
                            'type': 'string',
                            'description': 'Docker Compose service name to run',
                        },
                        'compose_file': {
                            'type': 'string',
                            'description': 'Path to docker-compose.yml (optional)',
                        },
                        'command': {
                            'type': 'string',
                            'description': 'Command to run in the container (optional)',
                        },
                    },
                    'required': ['service'],
                },
            ),
            Tool(
                name='send_pdb_command',
                description='Send a command to the active Pdb session',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'command': {
                            'type': 'string',
                            'description': 'Pdb command to execute (e.g., "n", "s", "p var")',
                        },
                    },
                    'required': ['command'],
                },
            ),
            Tool(
                name='stop_pdb_session',
                description='Stop the active Pdb debugging session',
                inputSchema={
                    'type': 'object',
                    'properties': {},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        if name == 'start_pdb_session':
            return await handle_start_pdb_session(arguments)
        elif name == 'send_pdb_command':
            return await handle_send_pdb_command(arguments)
        elif name == 'stop_pdb_session':
            return await handle_stop_pdb_session(arguments)
        else:
            result = {'success': False, 'error': f'Unknown tool: {name}'}
            return [TextContent(type='text', text=json.dumps(result, indent=2))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
