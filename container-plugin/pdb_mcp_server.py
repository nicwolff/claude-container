#!/usr/bin/env python3
"""MCP server for debugging Python apps in Docker containers."""

import asyncio
import json
import shlex
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

PDB_PROMPT = b'(Pdb) '


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
        self._partial: bytes = b''
        self._data_event: asyncio.Event = asyncio.Event()

    async def start(self) -> dict[str, Any]:
        """Start the Docker Compose session with debugging."""
        if self.is_running:
            return {
                'success': False,
                'error': 'Session already running',
            }

        cmd = [
            'docker', 'compose',
            '-f', self.compose_file,
            'run', '-i', '--rm', self.service,
        ]
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
            self.output_task = asyncio.create_task(
                self._read_output()
            )

            await self._wait_for_prompt(timeout=10.0)

            return {
                'success': True,
                'pid': self.process.pid,
                'message': 'Pdb session started successfully',
            }
        except Exception as e:
            self.is_running = False
            return {'success': False, 'error': str(e)}

    async def send_command(self, command: str) -> dict[str, Any]:
        """Send a command to the Pdb session and return output."""
        if (
            not self.is_running
            or not self.process
            or not self.process.stdin
        ):
            return {
                'success': False,
                'error': 'No active session',
            }

        if self.process.returncode is not None:
            return {
                'success': False,
                'error': (
                    'Process has terminated with code'
                    f' {self.process.returncode}'
                ),
            }

        try:
            self.output_buffer.clear()
            self.process.stdin.write(f'{command}\n'.encode())
            await self.process.stdin.drain()
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
            return {
                'success': False,
                'error': 'No active session',
            }

        try:
            if self.process and self.process.stdin:
                try:
                    self.process.stdin.write(b'quit\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(
                        self.process.wait(), timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    if self.process:
                        self.process.kill()
                        await self.process.wait()

            if self.output_task:
                self.output_task.cancel()
                try:
                    await self.output_task
                except asyncio.CancelledError:
                    pass

            return {
                'success': True,
                'message': 'Session stopped successfully',
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}
        finally:
            self.is_running = False

    async def _read_output(self) -> None:
        """Read output byte-by-byte to detect prompts."""
        if not self.process or not self.process.stdout:
            return

        try:
            while True:
                byte = await self.process.stdout.read(1)
                if not byte:
                    if self._partial:
                        self.output_buffer.append(
                            self._partial.decode(
                                'utf-8', errors='replace',
                            )
                        )
                        self._partial = b''
                        self._data_event.set()
                    break
                self._partial += byte
                if byte == b'\n':
                    self._flush_partial()
                elif self._partial.endswith(PDB_PROMPT):
                    self._flush_partial()
        except asyncio.CancelledError:
            if self._partial:
                self.output_buffer.append(
                    self._partial.decode(
                        'utf-8', errors='replace',
                    )
                )
                self._partial = b''

    def _flush_partial(self) -> None:
        """Flush partial byte buffer to output buffer."""
        self.output_buffer.append(
            self._partial.decode('utf-8', errors='replace')
        )
        self._partial = b''
        self._data_event.set()

    async def _wait_for_prompt(
        self, timeout: float = 5.0,
    ) -> str:
        """Wait for the Pdb prompt to appear in output."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        collected: list[str] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            while self.output_buffer:
                chunk = self.output_buffer.pop(0)
                collected.append(chunk)
                if '(Pdb)' in chunk:
                    return ''.join(collected)

            self._data_event.clear()
            try:
                await asyncio.wait_for(
                    self._data_event.wait(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break

        while self.output_buffer:
            collected.append(self.output_buffer.pop(0))
        return ''.join(collected)


_ACTIVE_SESSION: PdbSession | None = None


async def handle_start_pdb_session(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Handle the start_pdb_session tool call."""
    service = arguments.get('service')
    compose_file = arguments.get('compose_file')
    command = arguments.get('command')

    if not service:
        result = {
            'success': False,
            'error': 'service parameter is required',
        }
        return [TextContent(
            type='text', text=json.dumps(result, indent=2),
        )]

    existing = _get_session()
    if existing and existing.is_running:
        await existing.stop()

    session = PdbSession(
        service=service,
        compose_file=compose_file,
        command=command,
    )
    _set_session(session)

    result = await session.start()
    return [TextContent(
        type='text', text=json.dumps(result, indent=2),
    )]


async def handle_send_pdb_command(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Handle the send_pdb_command tool call."""
    command = arguments.get('command')

    if not command:
        result = {
            'success': False,
            'error': 'command parameter is required',
        }
        return [TextContent(
            type='text', text=json.dumps(result, indent=2),
        )]

    session = _get_session()
    if not session:
        result = {
            'success': False,
            'error': 'No active session. Start one first.',
        }
        return [TextContent(
            type='text', text=json.dumps(result, indent=2),
        )]

    result = await session.send_command(command)
    return [TextContent(
        type='text', text=json.dumps(result, indent=2),
    )]


async def handle_stop_pdb_session(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Handle the stop_pdb_session tool call."""
    session = _get_session()
    if not session:
        result = {
            'success': False,
            'error': 'No active session to stop',
        }
        return [TextContent(
            type='text', text=json.dumps(result, indent=2),
        )]

    result = await session.stop()
    _set_session(None)
    return [TextContent(
        type='text', text=json.dumps(result, indent=2),
    )]


def _get_session() -> PdbSession | None:
    """Get the currently active Pdb session."""
    return _ACTIVE_SESSION


def _set_session(session: PdbSession | None) -> None:
    """Set the active Pdb session."""
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = session


async def main() -> None:
    """Run the MCP server."""
    server = Server('pdb-mcp-server')

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name='start_pdb_session',
                description=(
                    'Start a Pdb debugging session'
                    ' in a Docker container'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'service': {
                            'type': 'string',
                            'description': (
                                'Docker Compose service name'
                            ),
                        },
                        'compose_file': {
                            'type': 'string',
                            'description': (
                                'Path to docker-compose.yml'
                                ' (optional)'
                            ),
                        },
                        'command': {
                            'type': 'string',
                            'description': (
                                'Command to run in the'
                                ' container (optional)'
                            ),
                        },
                    },
                    'required': ['service'],
                },
            ),
            Tool(
                name='send_pdb_command',
                description=(
                    'Send a command to the active'
                    ' Pdb session'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'command': {
                            'type': 'string',
                            'description': (
                                'Pdb command to execute'
                                ' (e.g., "n", "s", "p var")'
                            ),
                        },
                    },
                    'required': ['command'],
                },
            ),
            Tool(
                name='stop_pdb_session',
                description=(
                    'Stop the active Pdb debugging'
                    ' session'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: Any,
    ) -> list[TextContent]:
        if name == 'start_pdb_session':
            return await handle_start_pdb_session(arguments)
        elif name == 'send_pdb_command':
            return await handle_send_pdb_command(arguments)
        elif name == 'stop_pdb_session':
            return await handle_stop_pdb_session(arguments)
        else:
            result = {
                'success': False,
                'error': f'Unknown tool: {name}',
            }
            return [TextContent(
                type='text',
                text=json.dumps(result, indent=2),
            )]

    async with stdio_server() as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


if __name__ == '__main__':
    asyncio.run(main())
