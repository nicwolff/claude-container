#!/usr/bin/env python3
"""Docker socket proxy that blocks privileged containers and
restricts bind mounts to an allowed directory tree.

Sits between the Docker CLI and the real daemon socket,
inspecting POST /containers/create requests and rejecting
those that request privileged mode, dangerous capabilities,
or bind mounts outside the allowed directory tree.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys

UPSTREAM_SOCK = os.environ.get(
    'DOCKER_PROXY_UPSTREAM', '/var/run/docker-real.sock'
)
LISTEN_SOCK = os.environ.get(
    'DOCKER_PROXY_LISTEN', '/var/run/docker.sock'
)
ALLOWED_MOUNT_BASE = os.environ.get('ALLOWED_MOUNT_BASE', '/')
ALLOWED_RW_BASE = os.environ.get('ALLOWED_RW_BASE', '/')
BLOCKED_CAPS = frozenset({
    'ALL', 'SYS_ADMIN', 'SYS_PTRACE', 'NET_ADMIN',
    'SYS_RAWIO', 'SYS_MODULE', 'DAC_READ_SEARCH',
})
CREATE_RE = re.compile(
    rb'^POST /v\d[^/]*/containers/create'
)
MAX_BODY = 10 * 1024 * 1024
BUF_SIZE = 65536
LOG = logging.getLogger('docker-socket-proxy')


def main():
    """Configure logging and run the async proxy."""
    log_level = os.environ.get('LOG_LEVEL', 'WARNING').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format='%(name)s: %(message)s',
    )
    LOG.info(
        'starting: %s -> %s (mounts under %s, rw under %s)',
        LISTEN_SOCK, UPSTREAM_SOCK,
        ALLOWED_MOUNT_BASE, ALLOWED_RW_BASE,
    )
    asyncio.run(run_proxy())


async def run_proxy(
    listen=None, upstream=None, ready_event=None,
    stop_event=None,
):
    """Start the Unix socket server and run until signalled."""
    listen = listen or LISTEN_SOCK
    upstream = upstream or UPSTREAM_SOCK
    if os.path.exists(listen):
        os.unlink(listen)

    async def on_connect(r, w):
        await handle_connection(r, w, upstream)

    server = await asyncio.start_unix_server(
        on_connect, path=listen,
    )
    os.chmod(listen, 0o666)
    LOG.info('listening on %s', listen)
    if ready_event is not None:
        ready_event.set()
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()


async def handle_connection(client_r, client_w, upstream):
    """Handle a single client connection to the proxy."""
    upstream_r = upstream_w = None
    try:
        upstream_r, upstream_w = (
            await asyncio.open_unix_connection(upstream)
        )
        while True:
            keep_going = await proxy_request(
                client_r, client_w,
                upstream_r, upstream_w,
            )
            if not keep_going:
                break
    except (
        asyncio.IncompleteReadError,
        ConnectionError,
        OSError,
    ):
        pass
    finally:
        for writer in (client_w, upstream_w):
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass


async def proxy_request(
    client_r, client_w, upstream_r, upstream_w,
):
    """Proxy one HTTP request-response cycle.

    Returns True to continue (keep-alive), False to stop.
    """
    req_line, req_raw, req_hdrs = (
        await read_headers(client_r)
    )
    if req_line is None:
        return False
    LOG.debug(
        'request: %s', req_line.decode('latin-1').strip(),
    )
    content_length = int(
        req_hdrs.get('content-length', '0')
    )
    if CREATE_RE.match(req_line):
        if content_length > MAX_BODY:
            await send_error(
                client_w, 413, 'request body too large',
            )
            return False
        body = b''
        if content_length > 0:
            body = await client_r.readexactly(
                content_length,
            )
        error = validate_create_body(
            body, ALLOWED_MOUNT_BASE, ALLOWED_RW_BASE,
        )
        if error:
            await send_error(client_w, 403, error)
            return False
        upstream_w.write(req_raw + body)
        await upstream_w.drain()
    else:
        upstream_w.write(req_raw)
        await upstream_w.drain()
        if content_length > 0:
            body = await client_r.readexactly(
                content_length,
            )
            upstream_w.write(body)
            await upstream_w.drain()
        elif 'chunked' in req_hdrs.get(
            'transfer-encoding', '',
        ):
            await forward_chunked(client_r, upstream_w)
    status, should_close = await relay_response(
        upstream_r, client_w,
    )
    LOG.debug(
        'response: %d, close=%s', status, should_close,
    )
    if status == 101:
        LOG.debug('entering splice mode')
        await splice(
            client_r, client_w, upstream_r, upstream_w,
        )
        LOG.debug('splice finished')
        return False
    return not should_close


async def relay_response(upstream_r, client_w):
    """Read one HTTP response and forward to the client.

    Returns (status_code, should_close).
    """
    resp_line, resp_raw, resp_hdrs = (
        await read_headers(upstream_r)
    )
    if resp_line is None:
        return 0, True
    status = parse_status(resp_line)
    client_w.write(resp_raw)
    await client_w.drain()
    if status == 101:
        return 101, True
    if (100 <= status < 200) or status in (204, 304):
        conn = resp_hdrs.get('connection', '').lower()
        return status, conn == 'close'
    resp_cl = int(
        resp_hdrs.get('content-length', '-1')
    )
    is_chunked = 'chunked' in resp_hdrs.get(
        'transfer-encoding', '',
    )
    if resp_cl >= 0:
        if resp_cl > 0:
            data = await upstream_r.readexactly(resp_cl)
            client_w.write(data)
            await client_w.drain()
    elif is_chunked:
        await forward_chunked(upstream_r, client_w)
    else:
        while True:
            data = await upstream_r.read(BUF_SIZE)
            if not data:
                break
            client_w.write(data)
            await client_w.drain()
        return status, True
    conn = resp_hdrs.get('connection', '').lower()
    return status, conn == 'close'


async def forward_chunked(src, dst):
    """Forward chunked transfer-encoded body.

    Handles IncompleteReadError (Docker closing mid-chunk)
    by forwarding any partial data before returning.
    Also handles empty readline (connection drop before
    chunk header).
    """
    try:
        while True:
            chunk_hdr = await src.readline()
            if not chunk_hdr:
                break
            dst.write(chunk_hdr)
            size = int(chunk_hdr.strip(), 16)
            if size == 0:
                trailer = await src.readline()
                dst.write(trailer)
                await dst.drain()
                break
            data = await src.readexactly(size + 2)
            dst.write(data)
            await dst.drain()
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            dst.write(exc.partial)
            await dst.drain()


async def splice(r1, w1, r2, w2):
    """Bidirectional byte forwarding until both directions
    complete.

    When one direction hits EOF, propagate the half-close
    and wait for the other direction to finish.
    """
    task1 = asyncio.create_task(
        _forward_stream(r1, w2),
    )
    task2 = asyncio.create_task(
        _forward_stream(r2, w1),
    )
    try:
        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending:
            completed = done.pop()
            remaining = pending.pop()
            if completed == task1:
                _half_close(w2)
            else:
                _half_close(w1)
            try:
                await remaining
            except Exception:
                pass
    finally:
        for task in (task1, task2):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (
                    asyncio.CancelledError,
                    Exception,
                ):
                    pass
        for writer in (w1, w2):
            try:
                writer.close()
            except Exception:
                pass


def _half_close(writer):
    """Send write-EOF without tearing down the full
    connection.
    """
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except Exception:
        pass


async def _forward_stream(reader, writer):
    """Forward bytes from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (
        ConnectionError, OSError, asyncio.CancelledError,
    ):
        pass


async def read_headers(reader):
    """Read HTTP start line and headers.

    Returns (first_line, all_raw_bytes, headers_dict).
    Returns (None, None, None) on EOF.
    """
    first_line = await reader.readline()
    if not first_line:
        return None, None, None
    raw = bytearray(first_line)
    headers = {}
    while True:
        line = await reader.readline()
        raw.extend(line)
        if line in (b'\r\n', b'\n', b''):
            break
        decoded = line.decode('latin-1').strip()
        if ':' in decoded:
            key, _, value = decoded.partition(':')
            headers[key.strip().lower()] = value.strip()
    return bytes(first_line), bytes(raw), headers


async def send_error(writer, status, message):
    """Send an HTTP error response with a JSON body."""
    body = json.dumps({'message': message}).encode()
    phrases = {
        403: 'Forbidden',
        413: 'Request Entity Too Large',
    }
    phrase = phrases.get(status, 'Error')
    header = (
        f'HTTP/1.1 {status} {phrase}\r\n'
        f'Content-Type: application/json\r\n'
        f'Content-Length: {len(body)}\r\n'
        f'Connection: close\r\n'
        f'\r\n'
    )
    writer.write(header.encode() + body)
    await writer.drain()


def validate_create_body(body, allowed_base, rw_base):
    """Parse and validate a container create request body.

    Returns an error message or None.
    """
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 'invalid JSON in request body'
    return check_create_request(
        parsed, allowed_base, rw_base,
    )


def check_create_request(body, allowed_base, rw_base):
    """Validate a parsed container create request.

    Returns an error message if the request violates policy,
    or None if allowed.
    """
    host_cfg = body.get('HostConfig') or {}
    if host_cfg.get('Privileged'):
        return 'privileged containers are not allowed'
    cap_add = set(host_cfg.get('CapAdd') or [])
    blocked = cap_add & BLOCKED_CAPS
    if blocked:
        return (
            f'capabilities not allowed: {sorted(blocked)}'
        )
    if host_cfg.get('PidMode') == 'host':
        return 'host PID namespace is not allowed'
    if host_cfg.get('NetworkMode') == 'host':
        return 'host network mode is not allowed'
    for bind in host_cfg.get('Binds') or []:
        error = check_bind_string(
            bind, allowed_base, rw_base,
        )
        if error:
            return error
    for mount in host_cfg.get('Mounts') or []:
        if mount.get('Type') == 'bind':
            error = check_mount_object(
                mount, allowed_base, rw_base,
            )
            if error:
                return error
    return None


def check_bind_string(bind, allowed_base, rw_base):
    """Validate a Binds entry ('host:container[:opts]').

    Returns an error message or None.
    Named volumes (no leading /) are always allowed.
    """
    parts = bind.split(':')
    host_path = parts[0]
    if not host_path.startswith('/'):
        return None
    if not is_allowed_path(host_path, allowed_base):
        return f'bind mount not allowed: {host_path}'
    if not is_allowed_path(host_path, rw_base):
        opts = parts[2] if len(parts) > 2 else ''
        if 'ro' not in opts.split(','):
            return (
                f'bind mount must be read-only: '
                f'{host_path}'
            )
    return None


def check_mount_object(mount, allowed_base, rw_base):
    """Validate a Mounts entry (dict with Type=bind).

    Returns an error message or None.
    """
    source = mount.get('Source', '')
    if not is_allowed_path(source, allowed_base):
        return f'bind mount not allowed: {source}'
    if not is_allowed_path(source, rw_base):
        if not mount.get('ReadOnly'):
            return (
                f'bind mount must be read-only: {source}'
            )
    return None


def is_allowed_path(path, allowed_base):
    """Check whether path is under the allowed base."""
    normalized = os.path.normpath(path)
    base = os.path.normpath(allowed_base)
    if base == '/':
        return True
    return (
        normalized == base
        or normalized.startswith(base + '/')
    )


def parse_status(resp_line):
    """Extract HTTP status code from a response line."""
    parts = resp_line.split(None, 2)
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except (ValueError, TypeError):
            pass
    return 0


if __name__ == '__main__':
    main()
