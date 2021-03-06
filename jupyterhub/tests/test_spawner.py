"""Tests for process spawning"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import logging
import os
import signal
from subprocess import Popen
import sys
import tempfile
import time
from unittest import mock

import pytest
import requests
from tornado import gen

from ..user import User
from ..objects import Hub
from .. import spawner as spawnermod
from ..spawner import LocalProcessSpawner
from .. import orm

_echo_sleep = """
import sys, time
print(sys.argv)
time.sleep(30)
"""

_uninterruptible = """
import time
while True:
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        print("interrupted")
"""


def setup():
    logging.basicConfig(level=logging.DEBUG)


def new_spawner(db, **kwargs):
    user = kwargs.setdefault('user', User(db.query(orm.User).first(), {}))
    kwargs.setdefault('cmd', [sys.executable, '-c', _echo_sleep])
    kwargs.setdefault('hub', Hub())
    kwargs.setdefault('notebook_dir', os.getcwd())
    kwargs.setdefault('default_url', '/user/{username}/lab')
    kwargs.setdefault('oauth_client_id', 'mock-client-id')
    kwargs.setdefault('INTERRUPT_TIMEOUT', 1)
    kwargs.setdefault('TERM_TIMEOUT', 1)
    kwargs.setdefault('KILL_TIMEOUT', 1)
    kwargs.setdefault('poll_interval', 1)
    return user._new_spawner('', spawner_class=LocalProcessSpawner, **kwargs)


@pytest.mark.gen_test
def test_spawner(db, request):
    spawner = new_spawner(db)
    ip, port = yield spawner.start()
    assert ip == '127.0.0.1'
    assert isinstance(port, int)
    assert port > 0
    db.commit()

    # wait for the process to get to the while True: loop
    time.sleep(1)

    status = yield spawner.poll()
    assert status is None
    yield spawner.stop()
    status = yield spawner.poll()
    assert status == 1


@gen.coroutine
def wait_for_spawner(spawner, timeout=10):
    """Wait for an http server to show up
    
    polling at shorter intervals for early termination
    """
    deadline = time.monotonic() + timeout
    def wait():
        return spawner.server.wait_up(timeout=1, http=True)
    while time.monotonic() < deadline:
        status = yield spawner.poll()
        assert status is None
        try:
            yield wait()
        except TimeoutError:
            continue
        else:
            break
    yield wait()


@pytest.mark.gen_test(run_sync=False)
def test_single_user_spawner(app, request):
    user = next(iter(app.users.values()), None)
    spawner = user.spawner
    spawner.cmd = ['jupyterhub-singleuser']
    yield user.spawn()
    assert spawner.server.ip == '127.0.0.1'
    assert spawner.server.port > 0
    yield wait_for_spawner(spawner)
    status = yield spawner.poll()
    assert status is None
    yield spawner.stop()
    status = yield spawner.poll()
    assert status == 0


def test_stop_spawner_sigint_fails(db, io_loop):
    spawner = new_spawner(db, cmd=[sys.executable, '-c', _uninterruptible])
    io_loop.run_sync(spawner.start)
    
    # wait for the process to get to the while True: loop
    time.sleep(1)
    
    status = io_loop.run_sync(spawner.poll)
    assert status is None
    
    io_loop.run_sync(spawner.stop)
    status = io_loop.run_sync(spawner.poll)
    assert status == -signal.SIGTERM


def test_stop_spawner_stop_now(db, io_loop):
    spawner = new_spawner(db)
    io_loop.run_sync(spawner.start)
    
    # wait for the process to get to the while True: loop
    time.sleep(1)
    
    status = io_loop.run_sync(spawner.poll)
    assert status is None
    
    io_loop.run_sync(lambda : spawner.stop(now=True))
    status = io_loop.run_sync(spawner.poll)
    assert status == -signal.SIGTERM


def test_spawner_poll(db, io_loop):
    first_spawner = new_spawner(db)
    user = first_spawner.user
    io_loop.run_sync(first_spawner.start)
    proc = first_spawner.proc
    status = io_loop.run_sync(first_spawner.poll)
    assert status is None
    if user.state is None:
        user.state = {}
    first_spawner.orm_spawner.state = first_spawner.get_state()
    assert 'pid' in first_spawner.orm_spawner.state
    
    # create a new Spawner, loading from state of previous
    spawner = new_spawner(db, user=first_spawner.user)
    spawner.start_polling()
    
    # wait for the process to get to the while True: loop
    io_loop.run_sync(lambda : gen.sleep(1))
    status = io_loop.run_sync(spawner.poll)
    assert status is None
    
    # kill the process
    proc.terminate()
    for i in range(10):
        if proc.poll() is None:
            time.sleep(1)
        else:
            break
    assert proc.poll() is not None

    io_loop.run_sync(lambda : gen.sleep(2))
    status = io_loop.run_sync(spawner.poll)
    assert status is not None


def test_setcwd():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(os.path.abspath(td))
        spawnermod._try_setcwd(td)
        assert os.path.samefile(os.getcwd(), td)
    os.chdir(cwd)
    chdir = os.chdir
    temp_root = os.path.realpath(os.path.abspath(tempfile.gettempdir()))
    def raiser(path):
        path = os.path.realpath(os.path.abspath(path))
        if not path.startswith(temp_root):
            raise OSError(path)
        chdir(path)
    with mock.patch('os.chdir', raiser):
        spawnermod._try_setcwd(cwd)
        assert os.getcwd().startswith(temp_root)
    os.chdir(cwd)


def test_string_formatting(db):
    s = new_spawner(db, notebook_dir='user/%U/', default_url='/base/{username}')
    name = s.user.name
    assert s.notebook_dir == 'user/{username}/'
    assert s.default_url == '/base/{username}'
    assert s.format_string(s.notebook_dir) == 'user/%s/' % name
    assert s.format_string(s.default_url) == '/base/%s' % name


@pytest.mark.gen_test
def test_popen_kwargs(db):
    mock_proc = mock.Mock(spec=Popen)
    def mock_popen(*args, **kwargs):
        mock_proc.args = args
        mock_proc.kwargs = kwargs
        mock_proc.pid = 5
        return mock_proc

    s = new_spawner(db, popen_kwargs={'shell': True}, cmd='jupyterhub-singleuser')
    with mock.patch.object(spawnermod, 'Popen', mock_popen):
        yield s.start()

    assert mock_proc.kwargs['shell'] == True
    assert mock_proc.args[0][:1] == (['jupyterhub-singleuser'])


@pytest.mark.gen_test
def test_shell_cmd(db, tmpdir, request):
    f = tmpdir.join('bashrc')
    f.write('export TESTVAR=foo\n')
    s = new_spawner(db,
        cmd=[sys.executable, '-m', 'jupyterhub.tests.mocksu'],
        shell_cmd=['bash', '--rcfile', str(f), '-i', '-c'],
    )
    s.orm_spawner.server = orm.Server()
    db.commit()
    (ip, port) = yield s.start()
    request.addfinalizer(s.stop)
    s.server.ip = ip
    s.server.port = port
    db.commit()
    yield wait_for_spawner(s)
    r = requests.get('http://%s:%i/env' % (ip, port))
    r.raise_for_status()
    env = r.json()
    assert env['TESTVAR'] == 'foo'
