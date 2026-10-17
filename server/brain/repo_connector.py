"""Bounded, read-only import of a public GitHub repository for meeting analysis."""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import weakref
from urllib.parse import urlsplit

from brain.workspace_registry import REGISTRY, workspace_key
from brain.workspace_store import save_repo

log = logging.getLogger(__name__)

MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 6000
CLONE_TIMEOUT = 45
_NAME = re.compile(r'[A-Za-z0-9_.-]+\Z')


def canonical_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 300:
        raise ValueError('Use a public https://github.com/owner/repository URL.')
    parsed = urlsplit(url.strip())
    if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com'
            or parsed.query or parsed.fragment):
        raise ValueError('Only public GitHub HTTPS repository URLs are supported.')
    parts = parsed.path.strip('/').split('/')
    if len(parts) != 2 or any(not _NAME.fullmatch(p) or p in {'.', '..'} for p in parts):
        raise ValueError('Use a repository URL, without a branch or file path.')
    owner, repo = parts
    repo = repo.removesuffix('.git')
    if not repo or repo in {'.', '..'}:
        raise ValueError('Repository name is missing.')
    return f'https://github.com/{owner}/{repo}.git'


def _check_size(root: Path) -> None:
    total = count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
            count += 1
            if total > MAX_BYTES or count > MAX_FILES:
                raise ValueError('Repository exceeds the demo limit (50 MiB or 6000 files).')


async def _clone(url: str, target: Path) -> None:
    # Do not use this machine's Git credentials, URL rewrites, hooks or prompts.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_ASKPASS='false')
    proc = await asyncio.create_subprocess_exec(
        'git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
        '-c', 'http.followRedirects=false', '-c', 'protocol.file.allow=never',
        'clone', '--depth=1', '--single-branch', '--no-tags', '--no-recurse-submodules',
        '--', url, str(target), env=env, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    waiter = asyncio.create_task(proc.wait())
    try:
        async with asyncio.timeout(CLONE_TIMEOUT):
            while not waiter.done():
                await asyncio.wait({waiter}, timeout=0.1)
                _check_size(target)
            if await waiter:
                raise ValueError('Could not clone this public repository. Check its URL and visibility.')
            _check_size(target)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def connect_repo(session, url: str, *, restore_generation: object | None = None) -> None:
    """Session.repo_connector hook. Publish only a fully built, current code map."""
    from brain.code_map import CodeMap
    from agents.diagrammer import emit_event

    temporary = None
    workspace = workspace_key(session)
    if workspace is not None:
        REGISTRY.register(session)
        generation = restore_generation if restore_generation is not None else REGISTRY.claim(workspace)
    else:
        generation = object()
        session._repo_connect_generation = generation
    event_id = await emit_event(session, 'Repository index', 'deterministic',
                                'started', 'index public repository')
    try:
        canonical = canonical_url(url)
        temporary = Path(tempfile.mkdtemp(prefix='twin-repo-'))
        checkout = temporary / 'checkout'
        await _clone(canonical, checkout)
        # Index exactly the repository the workspace connected. A checked-in
        # demo_project is ordinary source, never an implicit root override.
        code_map = await asyncio.to_thread(lambda: CodeMap(checkout).build())
        scope = "repo:" + hashlib.sha256(canonical.encode()).hexdigest()[:16] + ":code"
        if workspace is not None:
            def persist() -> None:
                try:
                    save_repo(workspace, canonical)
                except Exception:
                    log.exception("Could not persist repository URL for workspace %s", workspace)

            if not REGISTRY.publish(workspace, generation, code_map, canonical,
                                    scope, temporary, on_publish=persist):
                raise asyncio.CancelledError
        else:
            if session._repo_connect_generation is not generation:
                raise asyncio.CancelledError
            # A previous analyst may still be reading its checkout. Its map
            # finalizer removes it only after all references are gone.
            weakref.finalize(code_map, shutil.rmtree, temporary, ignore_errors=True)
            session.code_map = code_map
            session.repo_url = canonical
            session.code_fact_scope = scope
            session._repo_directory = temporary
        temporary = None
        update = session.push_update(f'I indexed {len(code_map.files)} source files from {canonical.removesuffix(".git")}.')
        if inspect.isawaitable(update):
            await update
        await emit_event(session, 'Repository index', 'deterministic', 'done',
                         f'{len(code_map.files)} files indexed', event_id=event_id)
    except asyncio.CancelledError:
        await emit_event(session, 'Repository index', 'deterministic', 'cancelled',
                         'repository request replaced', event_id=event_id)
        raise
    except Exception as exc:
        # Avoid returning Git stderr (it may contain local credential-helper output).
        message = str(exc) if isinstance(exc, ValueError) else 'Repository indexing failed or timed out.'
        await session.broadcast({'type': 'error', 'message': message[:300], 'recoverable': True})
        await emit_event(session, 'Repository index', 'deterministic', 'failed',
                         message[:160], event_id=event_id)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
