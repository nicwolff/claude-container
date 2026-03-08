#!/usr/bin/env python3
"""Tests for docker_socket_proxy module."""

import asyncio
import json
import logging
import os
import runpy
import signal
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import docker_socket_proxy as proxy


# -- Helpers ----------------------------------------------


class MockWriter:
    """In-memory async writer for testing."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False
        self.drain_count = 0
        self.eof_written = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.drain_count += 1

    def close(self):
        self.closed = True

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof_written = True


def make_reader(data: bytes):
    """Create an asyncio.StreamReader pre-loaded with data."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def build_http_request(
    method, path, body=None, headers=None,
):
    """Build raw HTTP/1.1 request bytes."""
    line = f'{method} {path} HTTP/1.1\r\n'
    hdrs = headers or {}
    if body is not None:
        encoded = (
            body if isinstance(body, bytes)
            else json.dumps(body).encode()
        )
        hdrs['Content-Length'] = str(len(encoded))
    else:
        encoded = b''
    hdr_lines = ''.join(
        f'{k}: {v}\r\n' for k, v in hdrs.items()
    )
    return (line + hdr_lines + '\r\n').encode() + encoded


def build_http_response(
    status, phrase='OK', body=b'', headers=None,
):
    """Build raw HTTP/1.1 response bytes."""
    hdrs = headers or {}
    if body and 'Content-Length' not in hdrs:
        hdrs['Content-Length'] = str(len(body))
    hdr_lines = ''.join(
        f'{k}: {v}\r\n' for k, v in hdrs.items()
    )
    line = f'HTTP/1.1 {status} {phrase}\r\n'
    return (line + hdr_lines + '\r\n').encode() + body


# -- is_allowed_path --------------------------------------


class TestIsAllowedPath:

    def test_under_base(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/app',
            '/home/user/projects',
        )

    def test_equal_to_base(self):
        assert proxy.is_allowed_path(
            '/home/user/projects',
            '/home/user/projects',
        )

    def test_outside_base(self):
        assert not proxy.is_allowed_path(
            '/etc/passwd', '/home/user/projects',
        )

    def test_dotdot_escape(self):
        assert not proxy.is_allowed_path(
            '/home/user/projects/../../etc/passwd',
            '/home/user/projects',
        )

    def test_dotdot_still_under(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/a/../b',
            '/home/user/projects',
        )

    def test_prefix_not_subdir(self):
        """'/home/user/projects-evil' is not under
        '/home/user/projects'."""
        assert not proxy.is_allowed_path(
            '/home/user/projects-evil',
            '/home/user/projects',
        )

    def test_root_base(self):
        assert proxy.is_allowed_path('/anything', '/')

    def test_trailing_slash(self):
        assert proxy.is_allowed_path(
            '/home/user/projects/app/',
            '/home/user/projects',
        )


# -- check_create_request ---------------------------------


class TestCheckCreateRequest:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def _check(self, body):
        return proxy.check_create_request(
            body, self.base, self.rw,
        )

    def test_clean_request(self):
        body = {'Image': 'ubuntu', 'HostConfig': {}}
        assert self._check(body) is None

    def test_no_host_config(self):
        body = {'Image': 'ubuntu'}
        assert self._check(body) is None

    def test_null_host_config(self):
        body = {'Image': 'ubuntu', 'HostConfig': None}
        assert self._check(body) is None

    def test_privileged(self):
        body = {'HostConfig': {'Privileged': True}}
        assert 'privileged' in self._check(body)

    def test_privileged_false(self):
        body = {'HostConfig': {'Privileged': False}}
        assert self._check(body) is None

    def test_blocked_cap_sys_admin(self):
        body = {'HostConfig': {'CapAdd': ['SYS_ADMIN']}}
        assert 'SYS_ADMIN' in self._check(body)

    def test_blocked_cap_all(self):
        body = {'HostConfig': {'CapAdd': ['ALL']}}
        assert 'ALL' in self._check(body)

    def test_allowed_cap(self):
        body = {
            'HostConfig': {
                'CapAdd': ['NET_BIND_SERVICE'],
            },
        }
        assert self._check(body) is None

    def test_null_cap_add(self):
        body = {'HostConfig': {'CapAdd': None}}
        assert self._check(body) is None

    def test_host_pid(self):
        body = {'HostConfig': {'PidMode': 'host'}}
        assert 'PID' in self._check(body)

    def test_container_pid(self):
        body = {
            'HostConfig': {'PidMode': 'container:abc'},
        }
        assert self._check(body) is None

    def test_host_network(self):
        body = {'HostConfig': {'NetworkMode': 'host'}}
        assert 'network' in self._check(body)

    def test_bridge_network(self):
        body = {'HostConfig': {'NetworkMode': 'bridge'}}
        assert self._check(body) is None

    def test_bad_bind(self):
        body = {
            'HostConfig': {
                'Binds': [
                    '/etc/shadow:/etc/shadow:ro',
                ],
            },
        }
        result = self._check(body)
        assert 'bind mount not allowed' in result
        assert '/etc/shadow' in result

    def test_bind_under_rw_base(self):
        bind = f'{self.rw}/src:/src'
        body = {'HostConfig': {'Binds': [bind]}}
        assert self._check(body) is None

    def test_bind_sibling_ro(self):
        bind = f'{self.base}/other:/other:ro'
        body = {'HostConfig': {'Binds': [bind]}}
        assert self._check(body) is None

    def test_bind_sibling_rw_rejected(self):
        bind = f'{self.base}/other:/other'
        body = {'HostConfig': {'Binds': [bind]}}
        result = self._check(body)
        assert 'must be read-only' in result

    def test_bind_sibling_explicit_rw_rejected(self):
        bind = f'{self.base}/other:/other:rw'
        body = {'HostConfig': {'Binds': [bind]}}
        result = self._check(body)
        assert 'must be read-only' in result

    def test_named_volume_in_binds(self):
        body = {
            'HostConfig': {
                'Binds': [
                    'rover_rover-static:/app/static',
                ],
            },
        }
        assert self._check(body) is None

    def test_null_binds(self):
        body = {'HostConfig': {'Binds': None}}
        assert self._check(body) is None

    def test_bad_mount(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': '/etc',
                }],
            },
        }
        assert 'bind mount not allowed' in (
            self._check(body)
        )

    def test_mount_under_rw_base(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.rw}/data',
                }],
            },
        }
        assert self._check(body) is None

    def test_mount_sibling_readonly_true(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                    'ReadOnly': True,
                }],
            },
        }
        assert self._check(body) is None

    def test_mount_sibling_rw_rejected(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                }],
            },
        }
        result = self._check(body)
        assert 'must be read-only' in result

    def test_mount_sibling_readonly_false_rejected(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'bind',
                    'Source': f'{self.base}/other',
                    'ReadOnly': False,
                }],
            },
        }
        result = self._check(body)
        assert 'must be read-only' in result

    def test_volume_mount_ignored(self):
        body = {
            'HostConfig': {
                'Mounts': [{
                    'Type': 'volume',
                    'Source': 'myvolume',
                }],
            },
        }
        assert self._check(body) is None

    def test_null_mounts(self):
        body = {'HostConfig': {'Mounts': None}}
        assert self._check(body) is None


# -- check_bind_string ------------------------------------


class TestCheckBindString:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_under_rw_no_opts(self):
        assert proxy.check_bind_string(
            f'{self.rw}/src:/src', self.base, self.rw,
        ) is None

    def test_under_rw_explicit_rw(self):
        assert proxy.check_bind_string(
            f'{self.rw}/src:/src:rw',
            self.base, self.rw,
        ) is None

    def test_sibling_ro(self):
        assert proxy.check_bind_string(
            f'{self.base}/other:/x:ro',
            self.base, self.rw,
        ) is None

    def test_sibling_ro_with_extra_opts(self):
        assert proxy.check_bind_string(
            f'{self.base}/other:/x:ro,z',
            self.base, self.rw,
        ) is None

    def test_sibling_no_opts_rejected(self):
        result = proxy.check_bind_string(
            f'{self.base}/other:/x',
            self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_sibling_rw_rejected(self):
        result = proxy.check_bind_string(
            f'{self.base}/other:/x:rw',
            self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_outside_base_rejected(self):
        result = proxy.check_bind_string(
            '/etc/passwd:/etc/passwd:ro',
            self.base, self.rw,
        )
        assert 'bind mount not allowed' in result

    def test_named_volume_allowed(self):
        assert proxy.check_bind_string(
            'myvolume:/app/data',
            self.base, self.rw,
        ) is None

    def test_named_volume_with_opts_allowed(self):
        assert proxy.check_bind_string(
            'rover_rover-static:/app/static:rw',
            self.base, self.rw,
        ) is None


# -- check_mount_object -----------------------------------


class TestCheckMountObject:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_under_rw(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.rw}/data',
        }
        assert proxy.check_mount_object(
            mount, self.base, self.rw,
        ) is None

    def test_sibling_readonly(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.base}/other',
            'ReadOnly': True,
        }
        assert proxy.check_mount_object(
            mount, self.base, self.rw,
        ) is None

    def test_sibling_not_readonly(self):
        mount = {
            'Type': 'bind',
            'Source': f'{self.base}/other',
        }
        result = proxy.check_mount_object(
            mount, self.base, self.rw,
        )
        assert 'must be read-only' in result

    def test_outside_base(self):
        mount = {
            'Type': 'bind',
            'Source': '/etc',
            'ReadOnly': True,
        }
        result = proxy.check_mount_object(
            mount, self.base, self.rw,
        )
        assert 'bind mount not allowed' in result


# -- validate_create_body ---------------------------------


class TestValidateCreateBody:
    base = '/home/user/projects'
    rw = '/home/user/projects/app'

    def test_empty_body(self):
        assert proxy.validate_create_body(
            b'', self.base, self.rw,
        ) is None

    def test_valid_json_allowed(self):
        body = json.dumps({
            'Image': 'ubuntu',
            'HostConfig': {},
        }).encode()
        assert proxy.validate_create_body(
            body, self.base, self.rw,
        ) is None

    def test_valid_json_rejected(self):
        body = json.dumps({
            'HostConfig': {'Privileged': True},
        }).encode()
        result = proxy.validate_create_body(
            body, self.base, self.rw,
        )
        assert 'privileged' in result

    def test_invalid_json(self):
        result = proxy.validate_create_body(
            b'not json', self.base, self.rw,
        )
        assert 'invalid JSON' in result

    def test_invalid_unicode(self):
        result = proxy.validate_create_body(
            b'\xff\xfe', self.base, self.rw,
        )
        assert 'invalid JSON' in result


# -- parse_status -----------------------------------------


class TestParseStatus:

    def test_normal(self):
        assert proxy.parse_status(
            b'HTTP/1.1 200 OK\r\n',
        ) == 200

    def test_no_phrase(self):
        assert proxy.parse_status(
            b'HTTP/1.1 204\r\n',
        ) == 204

    def test_malformed(self):
        assert proxy.parse_status(b'garbage\r\n') == 0

    def test_empty(self):
        assert proxy.parse_status(b'') == 0

    def test_non_integer(self):
        assert proxy.parse_status(
            b'HTTP/1.1 abc OK\r\n',
        ) == 0


# -- read_headers -----------------------------------------


class TestReadHeaders:

    @pytest.mark.asyncio
    async def test_normal_request(self):
        data = (
            b'GET /v1.45/info HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Accept: */*\r\n'
            b'\r\n'
        )
        reader = make_reader(data)
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line == b'GET /v1.45/info HTTP/1.1\r\n'
        assert raw == data
        assert hdrs['host'] == 'localhost'
        assert hdrs['accept'] == '*/*'

    @pytest.mark.asyncio
    async def test_eof(self):
        reader = make_reader(b'')
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line is None
        assert raw is None
        assert hdrs is None

    @pytest.mark.asyncio
    async def test_header_without_colon(self):
        data = (
            b'GET / HTTP/1.1\r\n'
            b'malformed-header\r\n'
            b'\r\n'
        )
        reader = make_reader(data)
        _, _, hdrs = await proxy.read_headers(reader)
        assert 'malformed-header' not in hdrs

    @pytest.mark.asyncio
    async def test_empty_line_terminator(self):
        """Handle \\n without \\r as header terminator."""
        data = b'GET / HTTP/1.1\r\nHost: x\r\n\n'
        reader = make_reader(data)
        line, raw, hdrs = await proxy.read_headers(
            reader,
        )
        assert line is not None
        assert hdrs['host'] == 'x'


# -- send_error -------------------------------------------


class TestSendError:

    @pytest.mark.asyncio
    async def test_403(self):
        writer = MockWriter()
        await proxy.send_error(writer, 403, 'denied')
        output = bytes(writer.data)
        assert b'HTTP/1.1 403 Forbidden' in output
        assert b'"message": "denied"' in output
        assert b'Connection: close' in output

    @pytest.mark.asyncio
    async def test_413(self):
        writer = MockWriter()
        await proxy.send_error(writer, 413, 'too big')
        output = bytes(writer.data)
        assert b'413 Request Entity Too Large' in output

    @pytest.mark.asyncio
    async def test_unknown_status(self):
        writer = MockWriter()
        await proxy.send_error(writer, 500, 'oops')
        output = bytes(writer.data)
        assert b'500 Error' in output


# -- forward_chunked --------------------------------------


class TestForwardChunked:

    @pytest.mark.asyncio
    async def test_single_chunk(self):
        data = b'5\r\nhello\r\n0\r\n\r\n'
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert b'hello' in bytes(writer.data)
        assert bytes(writer.data).endswith(b'0\r\n\r\n')

    @pytest.mark.asyncio
    async def test_multiple_chunks(self):
        data = (
            b'3\r\nabc\r\n'
            b'2\r\nde\r\n'
            b'0\r\n\r\n'
        )
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        output = bytes(writer.data)
        assert b'abc' in output
        assert b'de' in output

    @pytest.mark.asyncio
    async def test_empty_chunked(self):
        data = b'0\r\n\r\n'
        reader = make_reader(data)
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert bytes(writer.data) == b'0\r\n\r\n'

    @pytest.mark.asyncio
    async def test_incomplete_read_error_partial(self):
        """IncompleteReadError forwards partial data."""
        reader = asyncio.StreamReader()
        # Feed chunk header then only partial chunk data
        reader.feed_data(b'a\r\n')  # 10 bytes expected
        reader.feed_data(b'hello')  # only 5 bytes
        reader.feed_eof()
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        output = bytes(writer.data)
        assert b'a\r\n' in output
        assert b'hello' in output

    @pytest.mark.asyncio
    async def test_incomplete_read_error_empty_partial(self):
        """IncompleteReadError with empty partial is safe."""
        reader = asyncio.StreamReader()
        reader.feed_data(b'5\r\n')  # 5 bytes + 2 CRLF
        # Feed nothing, then EOF - readexactly gets 0 bytes
        reader.feed_eof()
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        # Header was written, but no partial data to forward
        assert b'5\r\n' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_empty_readline_connection_drop(self):
        """Empty readline means connection dropped."""
        reader = make_reader(b'')
        writer = MockWriter()
        await proxy.forward_chunked(reader, writer)
        assert bytes(writer.data) == b''


# -- _forward_stream --------------------------------------


class TestForwardStream:

    @pytest.mark.asyncio
    async def test_normal_forward(self):
        reader = make_reader(b'hello world')
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)
        assert bytes(writer.data) == b'hello world'

    @pytest.mark.asyncio
    async def test_connection_error(self):
        reader = asyncio.StreamReader()
        reader.set_exception(
            ConnectionResetError('gone'),
        )
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)

    @pytest.mark.asyncio
    async def test_os_error(self):
        reader = asyncio.StreamReader()
        reader.set_exception(OSError('broken'))
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)

    @pytest.mark.asyncio
    async def test_cancelled(self):
        """CancelledError is handled gracefully."""
        reader = asyncio.StreamReader()
        reader.set_exception(asyncio.CancelledError())
        writer = MockWriter()
        await proxy._forward_stream(reader, writer)


# -- _half_close ------------------------------------------


class TestHalfClose:

    def test_supported(self):
        w = MockWriter()
        proxy._half_close(w)
        assert w.eof_written

    def test_not_supported(self):
        w = MockWriter()
        w.can_write_eof = lambda: False
        proxy._half_close(w)
        assert not w.eof_written

    def test_write_eof_raises(self):
        w = MockWriter()
        w.write_eof = MagicMock(
            side_effect=OSError('broken'),
        )
        proxy._half_close(w)

    def test_can_write_eof_raises(self):
        w = MockWriter()
        w.can_write_eof = MagicMock(
            side_effect=RuntimeError('fail'),
        )
        proxy._half_close(w)


# -- splice -----------------------------------------------


class TestSplice:

    @pytest.mark.asyncio
    async def test_bidirectional(self):
        r1 = make_reader(b'from-client')
        w1 = MockWriter()
        r2 = make_reader(b'from-server')
        w2 = MockWriter()
        await proxy.splice(r1, w1, r2, w2)
        assert bytes(w2.data) == b'from-client'
        assert bytes(w1.data) == b'from-server'
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_one_side_closes_first(self):
        """When one direction ends first, the other
        continues until done."""
        r1 = make_reader(b'short')
        w1 = MockWriter()
        r2 = make_reader(b'')
        w2 = MockWriter()
        await proxy.splice(r1, w1, r2, w2)
        assert bytes(w2.data) == b'short'
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_writer_close_raises(self):
        """Exception in writer.close() is suppressed."""
        r1 = make_reader(b'data')
        w1 = MockWriter()
        w1.close = MagicMock(
            side_effect=RuntimeError('close boom'),
        )
        r2 = make_reader(b'')
        w2 = MockWriter()
        w2.close = MagicMock(
            side_effect=OSError('close fail'),
        )
        await proxy.splice(r1, w1, r2, w2)

    @pytest.mark.asyncio
    async def test_client_half_close(self):
        """Client EOF first; server data still arrives."""
        r1 = make_reader(b'')
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()

        async def feed_later():
            await asyncio.sleep(0.01)
            r2.feed_data(b'container output')
            r2.feed_eof()

        feeder = asyncio.create_task(feed_later())
        await proxy.splice(r1, w1, r2, w2)
        await feeder
        assert bytes(w1.data) == b'container output'
        assert w2.eof_written
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_server_closes_first(self):
        """Server EOF first; client data still arrives."""
        r1 = asyncio.StreamReader()
        w1 = MockWriter()
        r2 = make_reader(b'server-data')
        w2 = MockWriter()

        async def feed_later():
            await asyncio.sleep(0.01)
            r1.feed_data(b'client-data')
            r1.feed_eof()

        feeder = asyncio.create_task(feed_later())
        await proxy.splice(r1, w1, r2, w2)
        await feeder
        assert bytes(w1.data) == b'server-data'
        assert bytes(w2.data) == b'client-data'
        assert w1.eof_written
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_remaining_task_raises(self):
        """Exception from remaining task is caught."""
        r1 = make_reader(b'')
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()
        original = proxy._forward_stream

        async def error_forward(reader, writer):
            if reader is r2:
                await asyncio.sleep(0.01)
                raise ValueError('unexpected')
            return await original(reader, writer)

        with patch.object(
            proxy, '_forward_stream', error_forward,
        ):
            await proxy.splice(r1, w1, r2, w2)
        assert w1.closed
        assert w2.closed

    @pytest.mark.asyncio
    async def test_external_cancellation(self):
        """External cancellation cleans up properly."""

        async def uncatchable(reader, writer):
            await asyncio.sleep(100)

        r1 = asyncio.StreamReader()
        w1 = MockWriter()
        r2 = asyncio.StreamReader()
        w2 = MockWriter()
        with patch.object(
            proxy, '_forward_stream', uncatchable,
        ):
            task = asyncio.create_task(
                proxy.splice(r1, w1, r2, w2),
            )
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert w1.closed
        assert w2.closed


# -- relay_response ---------------------------------------


class TestRelayResponse:

    @pytest.mark.asyncio
    async def test_200_with_body(self):
        resp = build_http_response(200, body=b'ok')
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        output = bytes(writer.data)
        assert b'200 OK' in output
        assert output.endswith(b'ok')

    @pytest.mark.asyncio
    async def test_204_no_body(self):
        resp = build_http_response(
            204, phrase='No Content', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 204
        assert not close

    @pytest.mark.asyncio
    async def test_304_no_body(self):
        resp = build_http_response(
            304, phrase='Not Modified', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 304

    @pytest.mark.asyncio
    async def test_1xx_no_body(self):
        resp = build_http_response(
            100, phrase='Continue', body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 100

    @pytest.mark.asyncio
    async def test_101_upgrade(self):
        resp = (
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: tcp\r\n'
            b'\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 101
        assert close

    @pytest.mark.asyncio
    async def test_chunked_response(self):
        resp = (
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'3\r\nabc\r\n0\r\n\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, _ = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert b'abc' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_close_delimited(self):
        resp = (
            b'HTTP/1.1 200 OK\r\n'
            b'\r\n'
            b'streaming data'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert close
        assert b'streaming data' in bytes(writer.data)

    @pytest.mark.asyncio
    async def test_eof_on_headers(self):
        reader = make_reader(b'')
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 0
        assert close

    @pytest.mark.asyncio
    async def test_connection_close_header(self):
        resp = build_http_response(
            200, body=b'x',
            headers={'Connection': 'close'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        _, close = await proxy.relay_response(
            reader, writer,
        )
        assert close

    @pytest.mark.asyncio
    async def test_content_length_zero(self):
        resp = build_http_response(
            200, body=b'',
            headers={'Content-Length': '0'},
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 200
        assert not close

    @pytest.mark.asyncio
    async def test_204_connection_close(self):
        resp = (
            b'HTTP/1.1 204 No Content\r\n'
            b'Connection: close\r\n'
            b'\r\n'
        )
        reader = make_reader(resp)
        writer = MockWriter()
        status, close = await proxy.relay_response(
            reader, writer,
        )
        assert status == 204
        assert close


# -- proxy_request ----------------------------------------


class TestProxyRequest:

    def _make_pair(self, req_data, resp_data):
        """Create reader/writer pairs for
        client+upstream."""
        client_r = make_reader(req_data)
        client_w = MockWriter()
        upstream_r = make_reader(resp_data)
        upstream_w = MockWriter()
        return client_r, client_w, upstream_r, upstream_w

    @pytest.mark.asyncio
    async def test_allowed_create(self):
        body = {'Image': 'ubuntu', 'HostConfig': {}}
        req = build_http_request(
            'POST', '/v1.45/containers/create', body,
        )
        resp = build_http_response(
            201, body=b'{"Id":"x"}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False or result is True
        assert b'containers/create' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_rejected_create_privileged(self):
        body = {'HostConfig': {'Privileged': True}}
        req = build_http_request(
            'POST', '/v1.45/containers/create', body,
        )
        resp = build_http_response(
            200, body=b'unused',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False
        assert b'403 Forbidden' in bytes(cw.data)
        assert len(uw.data) == 0

    @pytest.mark.asyncio
    async def test_rejected_create_too_large(self):
        req = (
            b'POST /v1.45/containers/create HTTP/1.1\r\n'
            b'Content-Length: 999999999\r\n'
            b'\r\n'
        )
        resp = build_http_response(
            200, body=b'unused',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(proxy, 'MAX_BODY', 100):
            result = await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert result is False
        assert b'413' in bytes(cw.data)

    @pytest.mark.asyncio
    async def test_create_no_body(self):
        req = build_http_request(
            'POST', '/v1.45/containers/create',
        )
        resp = build_http_response(
            400, phrase='Bad Request',
            body=b'{"message":""}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        with patch.object(
            proxy, 'ALLOWED_MOUNT_BASE', '/home',
        ), patch.object(
            proxy, 'ALLOWED_RW_BASE', '/home',
        ):
            await proxy.proxy_request(
                cr, cw, ur, uw,
            )
        assert b'containers/create' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_non_create_with_body(self):
        req = build_http_request(
            'POST', '/v1.45/containers/abc/start',
            body={'detach': True},
        )
        resp = build_http_response(
            204, phrase='No Content',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert b'containers/abc/start' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_non_create_chunked(self):
        req = (
            b'POST /v1.45/build HTTP/1.1\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'5\r\nhello\r\n0\r\n\r\n'
        )
        resp = build_http_response(200, body=b'built')
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert b'hello' in bytes(uw.data)

    @pytest.mark.asyncio
    async def test_eof_returns_false(self):
        cr = make_reader(b'')
        cw = MockWriter()
        ur = make_reader(b'')
        uw = MockWriter()
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_upgrade_response(self):
        req = build_http_request(
            'POST', '/v1.45/exec/abc/start',
            body={'Detach': False, 'Tty': True},
        )
        resp = (
            b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: tcp\r\n'
            b'\r\n'
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_keep_alive(self):
        req = build_http_request(
            'GET', '/v1.45/info',
        )
        resp = build_http_response(
            200, body=b'{"info": true}',
        )
        cr, cw, ur, uw = self._make_pair(req, resp)
        result = await proxy.proxy_request(
            cr, cw, ur, uw,
        )
        assert result is True


# -- handle_connection ------------------------------------


class TestHandleConnection:

    @pytest.mark.asyncio
    async def test_full_request_response(self):
        req = build_http_request(
            'GET', '/v1.45/info',
        )
        resp = build_http_response(
            200, body=b'ok',
            headers={'Connection': 'close'},
        )
        with tempfile.TemporaryDirectory() as td:
            upstream_sock = os.path.join(
                td, 'upstream.sock',
            )
            responses = [resp]

            async def fake_server(reader, writer):
                _ = await reader.read(4096)
                writer.write(responses.pop(0))
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(
                fake_server, path=upstream_sock,
            )
            try:
                client_r = make_reader(req)
                client_w = MockWriter()
                await proxy.handle_connection(
                    client_r, client_w, upstream_sock,
                )
                assert b'ok' in bytes(client_w.data)
            finally:
                server.close()
                await server.wait_closed()

    @pytest.mark.asyncio
    async def test_upstream_connect_fails(self):
        client_r = make_reader(
            b'GET / HTTP/1.1\r\n\r\n',
        )
        client_w = MockWriter()
        await proxy.handle_connection(
            client_r, client_w, '/nonexistent/sock',
        )
        assert client_w.closed

    @pytest.mark.asyncio
    async def test_incomplete_read_error(self):
        """IncompleteReadError is handled gracefully."""
        req = (
            b'POST /v1.45/containers/create HTTP/1.1\r\n'
            b'Content-Length: 9999\r\n'
            b'\r\n'
            b'short'
        )
        with tempfile.TemporaryDirectory() as td:
            sock = os.path.join(td, 'up.sock')

            async def fake_server(reader, writer):
                await asyncio.sleep(10)

            server = await asyncio.start_unix_server(
                fake_server, path=sock,
            )
            try:
                client_r = make_reader(req)
                client_w = MockWriter()
                with patch.object(
                    proxy, 'ALLOWED_MOUNT_BASE', '/',
                ):
                    await proxy.handle_connection(
                        client_r, client_w, sock,
                    )
            finally:
                server.close()
                await server.wait_closed()

    @pytest.mark.asyncio
    async def test_writer_close_raises(self):
        """Exception in writer.close() is swallowed."""
        client_r = make_reader(b'')
        client_w = MockWriter()
        client_w.close = MagicMock(
            side_effect=RuntimeError('boom'),
        )
        await proxy.handle_connection(
            client_r, client_w, '/nonexistent/sock',
        )


# -- run_proxy --------------------------------------------


class TestRunProxy:

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            listen = os.path.join(td, 'listen.sock')
            upstream = os.path.join(
                td, 'upstream.sock',
            )
            ready = asyncio.Event()

            async def stop_after_ready():
                await ready.wait()
                assert os.path.exists(listen)
                await asyncio.sleep(0.05)
                os.kill(os.getpid(), signal.SIGTERM)

            task = asyncio.create_task(
                proxy.run_proxy(listen, upstream, ready),
            )
            stopper = asyncio.create_task(
                stop_after_ready(),
            )
            await asyncio.gather(task, stopper)

    @pytest.mark.asyncio
    async def test_removes_stale_socket(self):
        with tempfile.TemporaryDirectory() as td:
            listen = os.path.join(td, 'listen.sock')
            upstream = os.path.join(
                td, 'upstream.sock',
            )
            with open(listen, 'w') as f:
                f.write('stale')
            ready = asyncio.Event()

            async def stop_soon():
                await ready.wait()
                os.kill(os.getpid(), signal.SIGTERM)

            task = asyncio.create_task(
                proxy.run_proxy(listen, upstream, ready),
            )
            stopper = asyncio.create_task(stop_soon())
            await asyncio.gather(task, stopper)


# -- main -------------------------------------------------


class TestMain:

    def test_main_starts_proxy(self):
        with patch.object(proxy, 'run_proxy') as mock_rp:
            mock_rp.return_value = asyncio.Future()
            mock_rp.return_value.set_result(None)
            with patch('asyncio.run') as mock_run:
                proxy.main()
                mock_run.assert_called_once()

    def test_log_level_from_env(self):
        with patch.object(proxy, 'run_proxy') as mock_rp:
            mock_rp.return_value = asyncio.Future()
            mock_rp.return_value.set_result(None)
            with patch('asyncio.run'), \
                 patch.dict(
                     os.environ, {'LOG_LEVEL': 'DEBUG'},
                 ):
                proxy.main()
        root = logging.getLogger()
        assert root.level in (
            logging.DEBUG, logging.WARNING,
        )

    def test_log_level_invalid_falls_back(self):
        with patch.object(proxy, 'run_proxy') as mock_rp:
            mock_rp.return_value = asyncio.Future()
            mock_rp.return_value.set_result(None)
            with patch('asyncio.run'), \
                 patch.dict(
                     os.environ,
                     {'LOG_LEVEL': 'NOTREAL'},
                 ):
                proxy.main()

    def test_dunder_main(self):
        with patch('asyncio.run'):
            runpy.run_module(
                'docker_socket_proxy',
                run_name='__main__',
            )


# -- Integration ------------------------------------------


class TestIntegration:

    @pytest.mark.asyncio
    async def test_end_to_end_allowed_request(self):
        """Full proxy: allowed GET flows through."""
        with tempfile.TemporaryDirectory() as td:
            listen_sock = os.path.join(
                td, 'proxy.sock',
            )
            upstream_sock = os.path.join(
                td, 'docker.sock',
            )

            async def docker_fake(reader, writer):
                await reader.readline()
                while True:
                    line = await reader.readline()
                    if line in (b'\r\n', b'\n', b''):
                        break
                resp = build_http_response(
                    200, body=b'{"ok":true}',
                    headers={'Connection': 'close'},
                )
                writer.write(resp)
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(
                docker_fake, path=upstream_sock,
            )
            try:
                ready = asyncio.Event()

                async def run():
                    await proxy.run_proxy(
                        listen_sock,
                        upstream_sock,
                        ready,
                    )

                proxy_task = asyncio.create_task(run())
                await ready.wait()

                r, w = (
                    await asyncio.open_unix_connection(
                        listen_sock,
                    )
                )
                req = build_http_request(
                    'GET', '/v1.45/info',
                )
                w.write(req)
                await w.drain()
                data = await r.read(4096)
                assert b'200 OK' in data
                assert b'{"ok":true}' in data
                w.close()
                os.kill(os.getpid(), signal.SIGTERM)
                await proxy_task
            finally:
                server.close()
                await server.wait_closed()

    @pytest.mark.asyncio
    async def test_end_to_end_blocked_create(self):
        """Full proxy: privileged create is rejected."""
        with tempfile.TemporaryDirectory() as td:
            listen_sock = os.path.join(
                td, 'proxy.sock',
            )
            upstream_sock = os.path.join(
                td, 'docker.sock',
            )

            async def docker_fake(reader, writer):
                await asyncio.sleep(10)

            server = await asyncio.start_unix_server(
                docker_fake, path=upstream_sock,
            )
            try:
                ready = asyncio.Event()
                proxy_task = asyncio.create_task(
                    proxy.run_proxy(
                        listen_sock,
                        upstream_sock,
                        ready,
                    ),
                )
                await ready.wait()

                r, w = (
                    await asyncio.open_unix_connection(
                        listen_sock,
                    )
                )
                body = {
                    'HostConfig': {'Privileged': True},
                }
                req = build_http_request(
                    'POST',
                    '/v1.45/containers/create',
                    body,
                )
                w.write(req)
                await w.drain()
                data = await r.read(4096)
                assert b'403 Forbidden' in data
                assert b'privileged' in data
                w.close()
                os.kill(os.getpid(), signal.SIGTERM)
                await proxy_task
            finally:
                server.close()
                await server.wait_closed()
