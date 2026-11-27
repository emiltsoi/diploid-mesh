"""_lock_fd_windows retry semantics — stub msvcrt so it runs off-Windows."""

import errno
import os
import tempfile

import pytest

from diploid_mesh import open_threads
from diploid_mesh.open_threads import _lock_fd_windows, _unlock_fd_windows


class _FakeMsvcrt:
    LK_LOCK = 0
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self, errnum: int | None = None, fail_times: int = 0) -> None:
        self.calls: list[int] = []
        self.errnum = errnum
        self.fail_times = fail_times

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append(mode)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError(self.errnum, "locked")


@pytest.fixture
def fd():
    fd_, path = tempfile.mkstemp()
    yield fd_
    os.close(fd_)
    os.unlink(path)


def test_retries_contention_then_succeeds(monkeypatch, fd) -> None:
    fake = _FakeMsvcrt(errno.EACCES, fail_times=3)
    monkeypatch.setattr(open_threads, "msvcrt", fake)
    _lock_fd_windows(fd)
    assert fake.calls == [_FakeMsvcrt.LK_NBLCK] * 4


def test_non_contention_error_fails_fast(monkeypatch, fd) -> None:
    fake = _FakeMsvcrt(errno.EINVAL, fail_times=99)
    monkeypatch.setattr(open_threads, "msvcrt", fake)
    with pytest.raises(OSError):
        _lock_fd_windows(fd)
    assert fake.calls == [_FakeMsvcrt.LK_NBLCK]  # no retry


def test_deadline_gives_up(monkeypatch, fd) -> None:
    fake = _FakeMsvcrt(errno.EACCES, fail_times=10_000)
    monkeypatch.setattr(open_threads, "msvcrt", fake)
    monkeypatch.setattr(open_threads, "_LOCK_ACQUIRE_DEADLINE_S", 0.2)
    with pytest.raises(OSError):
        _lock_fd_windows(fd)
    assert len(fake.calls) > 1  # retried until deadline


def test_unlock_uses_unlck_mode(monkeypatch, fd) -> None:
    fake = _FakeMsvcrt()
    monkeypatch.setattr(open_threads, "msvcrt", fake)
    _unlock_fd_windows(fd)
    assert fake.calls == [_FakeMsvcrt.LK_UNLCK]
