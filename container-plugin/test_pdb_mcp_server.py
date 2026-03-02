#!/usr/bin/env python3
"""Tests for pdb_mcp_server module."""

import asyncio
import json
import runpy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pdb_mcp_server import (
    PDB_PROMPT,
    PdbSession,
    _get_session,
    _set_session,
    handle_send_pdb_command,
    handle_start_pdb_session,
    handle_stop_pdb_session,
    main,
)


@pytest.fixture(autouse=True)
def _reset_session():
    """Reset global session state between tests."""
    _set_session(None)
    yield
    _set_session(None)


class MockReader:
    """Async reader returning bytes one at a time."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        if self._pos >= len(self._data):
            return b''
        result = self._data[self._pos:self._pos + n]
        self._pos += n
        return result


class BlockingReader:
    """Async reader that returns data then blocks forever."""

    def __init__(self, data: bytes = b'') -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int) -> bytes:
        if self._pos < len(self._data):
            result = self._data[self._pos:self._pos + n]
            self._pos += n
            await asyncio.sleep(0)
            return result
        await asyncio.sleep(999999)
        return b''


def _make_process(
    stdout_data=b'', pid=12345, returncode=None,
):
    """Create a mock subprocess.Process."""
    proc = MagicMock()
    proc.stdout = MockReader(stdout_data)
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _parse_result(content_list):
    """Parse JSON from TextContent list."""
    return json.loads(content_list[0].text)


# -- PdbSession.__init__ --


def test_init_defaults():
    s = PdbSession(service='web')
    assert s.service == 'web'
    assert s.compose_file == 'docker-compose.yml'
    assert s.command is None
    assert s.process is None
    assert s.output_buffer == []
    assert s.output_task is None
    assert s.is_running is False
    assert s._partial == b''
    assert not s._data_event.is_set()


def test_init_custom():
    s = PdbSession(
        service='api',
        compose_file='custom.yml',
        command='pytest -x',
    )
    assert s.service == 'api'
    assert s.compose_file == 'custom.yml'
    assert s.command == 'pytest -x'


def test_init_compose_file_none_defaults():
    s = PdbSession(service='web', compose_file=None)
    assert s.compose_file == 'docker-compose.yml'


# -- PdbSession.start --


@pytest.mark.asyncio
async def test_start_already_running():
    s = PdbSession(service='web')
    s.is_running = True
    result = await s.start()
    assert result['success'] is False
    assert 'already running' in result['error']


@pytest.mark.asyncio
async def test_start_success():
    pdb_out = b'> file.py(1)<module>()\n(Pdb) '
    proc = _make_process(stdout_data=pdb_out, pid=999)

    with patch(
        'pdb_mcp_server.asyncio.create_subprocess_exec',
        new_callable=AsyncMock,
        return_value=proc,
    ):
        s = PdbSession(service='web')
        result = await s.start()

    assert result['success'] is True
    assert result['pid'] == 999
    assert s.is_running is True


@pytest.mark.asyncio
async def test_start_with_command():
    pdb_out = b'(Pdb) '
    proc = _make_process(stdout_data=pdb_out, pid=1)

    with patch(
        'pdb_mcp_server.asyncio.create_subprocess_exec',
        new_callable=AsyncMock,
        return_value=proc,
    ) as mock_exec:
        s = PdbSession(
            service='web',
            compose_file='alt.yml',
            command='pytest -x tests/',
        )
        await s.start()

    call_args = mock_exec.call_args[0]
    assert '-f' in call_args
    assert 'alt.yml' in call_args
    assert '-i' in call_args
    assert 'pytest' in call_args
    assert '-x' in call_args
    assert 'tests/' in call_args


@pytest.mark.asyncio
async def test_start_exception():
    with patch(
        'pdb_mcp_server.asyncio.create_subprocess_exec',
        new_callable=AsyncMock,
        side_effect=OSError('docker not found'),
    ):
        s = PdbSession(service='web')
        result = await s.start()

    assert result['success'] is False
    assert 'docker not found' in result['error']
    assert s.is_running is False


# -- PdbSession.send_command --


@pytest.mark.asyncio
async def test_send_command_not_running():
    s = PdbSession(service='web')
    result = await s.send_command('n')
    assert result['success'] is False
    assert 'No active session' in result['error']


@pytest.mark.asyncio
async def test_send_command_no_process():
    s = PdbSession(service='web')
    s.is_running = True
    s.process = None
    result = await s.send_command('n')
    assert result['success'] is False


@pytest.mark.asyncio
async def test_send_command_no_stdin():
    s = PdbSession(service='web')
    s.is_running = True
    s.process = MagicMock()
    s.process.stdin = None
    result = await s.send_command('n')
    assert result['success'] is False


@pytest.mark.asyncio
async def test_send_command_process_terminated():
    s = PdbSession(service='web')
    s.is_running = True
    proc = _make_process(returncode=1)
    s.process = proc
    result = await s.send_command('n')
    assert result['success'] is False
    assert 'terminated' in result['error']
    assert '1' in result['error']


@pytest.mark.asyncio
async def test_send_command_success():
    response = b'> file.py(2)func()\n-> x = 1\n(Pdb) '
    proc = _make_process(stdout_data=response, pid=10)
    proc.returncode = None

    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = asyncio.create_task(s._read_output())

    result = await s.send_command('n')
    assert result['success'] is True
    assert result['command'] == 'n'
    assert result['has_prompt'] is True
    assert '(Pdb)' in result['output']

    if s.output_task:
        s.output_task.cancel()
        try:
            await s.output_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_send_command_exception():
    s = PdbSession(service='web')
    s.is_running = True
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(
        side_effect=BrokenPipeError('broken'),
    )
    s.process = proc
    result = await s.send_command('n')
    assert result['success'] is False
    assert 'BrokenPipeError' in result['error']


# -- PdbSession.stop --


@pytest.mark.asyncio
async def test_stop_not_running():
    s = PdbSession(service='web')
    result = await s.stop()
    assert result['success'] is False
    assert 'No active session' in result['error']


@pytest.mark.asyncio
async def test_stop_graceful():
    proc = _make_process()
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = None

    result = await s.stop()
    assert result['success'] is True
    assert s.is_running is False
    proc.stdin.write.assert_called_with(b'quit\n')


@pytest.mark.asyncio
async def test_stop_timeout_kills():
    proc = _make_process()
    proc.wait = AsyncMock(
        side_effect=[asyncio.TimeoutError(), 0],
    )
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = None

    result = await s.stop()
    assert result['success'] is True
    assert s.is_running is False
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_stop_cancels_output_task():
    proc = _make_process()
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc

    async def fake_reader():
        await asyncio.sleep(999999)

    s.output_task = asyncio.create_task(fake_reader())
    result = await s.stop()
    assert result['success'] is True
    assert s.output_task.cancelled()


@pytest.mark.asyncio
async def test_stop_timeout_process_gone():
    """Process becomes None between timeout and kill."""
    proc = _make_process()
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = None

    async def clear_proc(*_args, **_kwargs):
        s.process = None
        raise asyncio.TimeoutError()

    proc.wait = clear_proc
    result = await s.stop()
    assert result['success'] is True
    assert s.is_running is False


@pytest.mark.asyncio
async def test_stop_no_process():
    s = PdbSession(service='web')
    s.is_running = True
    s.process = None
    s.output_task = None

    result = await s.stop()
    assert result['success'] is True
    assert s.is_running is False


@pytest.mark.asyncio
async def test_stop_no_stdin():
    proc = MagicMock()
    proc.stdin = None
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = None

    result = await s.stop()
    assert result['success'] is True


@pytest.mark.asyncio
async def test_stop_exception_still_clears_running():
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(side_effect=OSError('fail'))
    s = PdbSession(service='web')
    s.is_running = True
    s.process = proc
    s.output_task = None

    result = await s.stop()
    assert result['success'] is False
    assert 'fail' in result['error']
    assert s.is_running is False


# -- PdbSession._read_output --


@pytest.mark.asyncio
async def test_read_output_no_process():
    s = PdbSession(service='web')
    s.process = None
    await s._read_output()
    assert s.output_buffer == []


@pytest.mark.asyncio
async def test_read_output_no_stdout():
    s = PdbSession(service='web')
    s.process = MagicMock()
    s.process.stdout = None
    await s._read_output()
    assert s.output_buffer == []


@pytest.mark.asyncio
async def test_read_output_newline_detection():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = MockReader(b'line one\nline two\n')
    s.process = proc
    await s._read_output()
    assert s.output_buffer == ['line one\n', 'line two\n']


@pytest.mark.asyncio
async def test_read_output_prompt_detection():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = MockReader(b'> file.py(1)\n(Pdb) ')
    s.process = proc
    await s._read_output()
    assert len(s.output_buffer) == 2
    assert s.output_buffer[0] == '> file.py(1)\n'
    assert s.output_buffer[1] == '(Pdb) '


@pytest.mark.asyncio
async def test_read_output_eof_with_partial():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = MockReader(b'no newline')
    s.process = proc
    await s._read_output()
    assert s.output_buffer == ['no newline']
    assert s._data_event.is_set()


@pytest.mark.asyncio
async def test_read_output_eof_without_partial():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = MockReader(b'line\n')
    s.process = proc
    await s._read_output()
    assert s.output_buffer == ['line\n']
    assert s._partial == b''


@pytest.mark.asyncio
async def test_read_output_cancel_with_partial():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = BlockingReader(b'par')
    s.process = proc

    task = asyncio.create_task(s._read_output())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert 'par' in ''.join(s.output_buffer)


@pytest.mark.asyncio
async def test_read_output_cancel_without_partial():
    s = PdbSession(service='web')
    proc = MagicMock()
    proc.stdout = BlockingReader(b'')
    s.process = proc

    task = asyncio.create_task(s._read_output())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert s.output_buffer == []
    assert s._partial == b''


# -- PdbSession._flush_partial --


def test_flush_partial():
    s = PdbSession(service='web')
    s._partial = b'hello world'
    s._flush_partial()
    assert s.output_buffer == ['hello world']
    assert s._partial == b''
    assert s._data_event.is_set()


# -- PdbSession._wait_for_prompt --


@pytest.mark.asyncio
async def test_wait_for_prompt_immediate():
    s = PdbSession(service='web')
    s.output_buffer = ['> file.py(1)\n', '(Pdb) ']
    s._data_event.set()
    result = await s._wait_for_prompt(timeout=1.0)
    assert '(Pdb)' in result


@pytest.mark.asyncio
async def test_wait_for_prompt_timeout_empty():
    s = PdbSession(service='web')
    result = await s._wait_for_prompt(timeout=0.1)
    assert result == ''


@pytest.mark.asyncio
async def test_wait_for_prompt_timeout_with_data():
    s = PdbSession(service='web')
    s.output_buffer = ['some output\n']
    s._data_event.set()
    result = await s._wait_for_prompt(timeout=0.1)
    assert result == 'some output\n'
    assert '(Pdb)' not in result


@pytest.mark.asyncio
async def test_wait_for_prompt_zero_timeout():
    s = PdbSession(service='web')
    s.output_buffer = ['(Pdb) ']
    result = await s._wait_for_prompt(timeout=0.0)
    assert result == '(Pdb) '


# -- _get_session / _set_session --


def test_get_set_session():
    assert _get_session() is None
    s = PdbSession(service='web')
    _set_session(s)
    assert _get_session() is s
    _set_session(None)
    assert _get_session() is None


# -- handle_start_pdb_session --


@pytest.mark.asyncio
async def test_handle_start_no_service():
    result = _parse_result(
        await handle_start_pdb_session({}),
    )
    assert result['success'] is False
    assert 'service' in result['error']


@pytest.mark.asyncio
async def test_handle_start_success():
    mock_session = MagicMock()
    mock_session.start = AsyncMock(
        return_value={
            'success': True, 'pid': 1,
            'message': 'ok',
        },
    )

    with patch(
        'pdb_mcp_server.PdbSession',
        return_value=mock_session,
    ):
        result = _parse_result(
            await handle_start_pdb_session(
                {'service': 'web'},
            ),
        )

    assert result['success'] is True


@pytest.mark.asyncio
async def test_handle_start_stops_existing():
    existing = MagicMock()
    existing.is_running = True
    existing.stop = AsyncMock()
    _set_session(existing)

    mock_new = MagicMock()
    mock_new.start = AsyncMock(
        return_value={'success': True, 'pid': 1, 'message': 'ok'},
    )

    with patch(
        'pdb_mcp_server.PdbSession',
        return_value=mock_new,
    ):
        await handle_start_pdb_session({'service': 'web'})

    existing.stop.assert_called_once()


@pytest.mark.asyncio
async def test_handle_start_existing_not_running():
    existing = MagicMock()
    existing.is_running = False
    existing.stop = AsyncMock()
    _set_session(existing)

    mock_new = MagicMock()
    mock_new.start = AsyncMock(
        return_value={'success': True, 'pid': 1, 'message': 'ok'},
    )

    with patch(
        'pdb_mcp_server.PdbSession',
        return_value=mock_new,
    ):
        await handle_start_pdb_session({'service': 'web'})

    existing.stop.assert_not_called()


# -- handle_send_pdb_command --


@pytest.mark.asyncio
async def test_handle_send_no_command():
    result = _parse_result(
        await handle_send_pdb_command({}),
    )
    assert result['success'] is False
    assert 'command' in result['error']


@pytest.mark.asyncio
async def test_handle_send_no_session():
    result = _parse_result(
        await handle_send_pdb_command({'command': 'n'}),
    )
    assert result['success'] is False
    assert 'No active session' in result['error']


@pytest.mark.asyncio
async def test_handle_send_success():
    mock_session = MagicMock()
    mock_session.send_command = AsyncMock(
        return_value={
            'success': True, 'command': 'n',
            'output': '(Pdb) ', 'has_prompt': True,
        },
    )
    _set_session(mock_session)

    result = _parse_result(
        await handle_send_pdb_command({'command': 'n'}),
    )
    assert result['success'] is True
    mock_session.send_command.assert_called_once_with('n')


# -- handle_stop_pdb_session --


@pytest.mark.asyncio
async def test_handle_stop_no_session():
    result = _parse_result(
        await handle_stop_pdb_session({}),
    )
    assert result['success'] is False
    assert 'No active session' in result['error']


@pytest.mark.asyncio
async def test_handle_stop_success():
    mock_session = MagicMock()
    mock_session.stop = AsyncMock(
        return_value={
            'success': True,
            'message': 'Session stopped successfully',
        },
    )
    _set_session(mock_session)

    result = _parse_result(
        await handle_stop_pdb_session({}),
    )
    assert result['success'] is True
    assert _get_session() is None


# -- main() --


@pytest.mark.asyncio
async def test_main_list_tools():
    captured = {}

    def make_capture(key):
        def factory():
            def decorator(fn):
                captured[key] = fn
                return fn
            return decorator
        return factory

    mock_server = MagicMock()
    mock_server.list_tools = make_capture('list_tools')
    mock_server.call_tool = make_capture('call_tool')
    mock_server.create_initialization_options = MagicMock(
        return_value={},
    )
    mock_server.run = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock()),
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        'pdb_mcp_server.Server',
        return_value=mock_server,
    ), patch(
        'pdb_mcp_server.stdio_server',
        return_value=mock_ctx,
    ):
        await main()

    tools = await captured['list_tools']()
    assert len(tools) == 3
    names = [t.name for t in tools]
    assert 'start_pdb_session' in names
    assert 'send_pdb_command' in names
    assert 'stop_pdb_session' in names


@pytest.mark.asyncio
async def test_main_call_tool_start():
    captured = {}

    def make_capture(key):
        def factory():
            def decorator(fn):
                captured[key] = fn
                return fn
            return decorator
        return factory

    mock_server = MagicMock()
    mock_server.list_tools = make_capture('list_tools')
    mock_server.call_tool = make_capture('call_tool')
    mock_server.create_initialization_options = MagicMock(
        return_value={},
    )
    mock_server.run = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock()),
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        'pdb_mcp_server.Server',
        return_value=mock_server,
    ), patch(
        'pdb_mcp_server.stdio_server',
        return_value=mock_ctx,
    ):
        await main()

    call_tool = captured['call_tool']

    with patch(
        'pdb_mcp_server.handle_start_pdb_session',
        new_callable=AsyncMock,
    ) as mock_h:
        mock_h.return_value = [MagicMock()]
        await call_tool('start_pdb_session', {'service': 'x'})
        mock_h.assert_called_once_with({'service': 'x'})


@pytest.mark.asyncio
async def test_main_call_tool_send():
    captured = {}

    def make_capture(key):
        def factory():
            def decorator(fn):
                captured[key] = fn
                return fn
            return decorator
        return factory

    mock_server = MagicMock()
    mock_server.list_tools = make_capture('list_tools')
    mock_server.call_tool = make_capture('call_tool')
    mock_server.create_initialization_options = MagicMock(
        return_value={},
    )
    mock_server.run = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock()),
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        'pdb_mcp_server.Server',
        return_value=mock_server,
    ), patch(
        'pdb_mcp_server.stdio_server',
        return_value=mock_ctx,
    ):
        await main()

    call_tool = captured['call_tool']

    with patch(
        'pdb_mcp_server.handle_send_pdb_command',
        new_callable=AsyncMock,
    ) as mock_h:
        mock_h.return_value = [MagicMock()]
        await call_tool('send_pdb_command', {'command': 'n'})
        mock_h.assert_called_once_with({'command': 'n'})


@pytest.mark.asyncio
async def test_main_call_tool_stop():
    captured = {}

    def make_capture(key):
        def factory():
            def decorator(fn):
                captured[key] = fn
                return fn
            return decorator
        return factory

    mock_server = MagicMock()
    mock_server.list_tools = make_capture('list_tools')
    mock_server.call_tool = make_capture('call_tool')
    mock_server.create_initialization_options = MagicMock(
        return_value={},
    )
    mock_server.run = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock()),
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        'pdb_mcp_server.Server',
        return_value=mock_server,
    ), patch(
        'pdb_mcp_server.stdio_server',
        return_value=mock_ctx,
    ):
        await main()

    call_tool = captured['call_tool']

    with patch(
        'pdb_mcp_server.handle_stop_pdb_session',
        new_callable=AsyncMock,
    ) as mock_h:
        mock_h.return_value = [MagicMock()]
        await call_tool('stop_pdb_session', {})
        mock_h.assert_called_once_with({})


@pytest.mark.asyncio
async def test_main_call_tool_unknown():
    captured = {}

    def make_capture(key):
        def factory():
            def decorator(fn):
                captured[key] = fn
                return fn
            return decorator
        return factory

    mock_server = MagicMock()
    mock_server.list_tools = make_capture('list_tools')
    mock_server.call_tool = make_capture('call_tool')
    mock_server.create_initialization_options = MagicMock(
        return_value={},
    )
    mock_server.run = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock()),
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch(
        'pdb_mcp_server.Server',
        return_value=mock_server,
    ), patch(
        'pdb_mcp_server.stdio_server',
        return_value=mock_ctx,
    ):
        await main()

    call_tool = captured['call_tool']
    result = await call_tool('unknown_tool', {})
    text = json.loads(result[0].text)
    assert text['success'] is False
    assert 'Unknown tool' in text['error']


# -- __main__ block --


def test_main_entry_point():
    with patch('asyncio.run') as mock_run:
        runpy.run_path(
            '/Volumes/Develop/claude-container'
            '/container-plugin/pdb_mcp_server.py',
            run_name='__main__',
        )
    mock_run.assert_called_once()


# -- PDB_PROMPT constant --


def test_pdb_prompt_constant():
    assert PDB_PROMPT == b'(Pdb) '
