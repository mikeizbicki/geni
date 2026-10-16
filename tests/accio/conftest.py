"""Fixtures for the accio tests: a tmpdir that is a git repository.

A wall-clock budget on a test that spawns no git is a budget spent on
nothing, so `repo` initializes one and no test commits: git answers
`ls-files --exclude-standard` for a repo with no commits the same way
it answers for one with a thousand, and the walk only needs the answer.
"""
import os
import subprocess

import pytest


@pytest.fixture
def repo(tmp_path):
    """A temporary directory that is also a git repository."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def mk(repo):
    """Write text under the repo and return the path."""
    def _mk(name, text):
        path = os.path.join(str(repo), name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path
    return _mk


@pytest.fixture
def mkbytes(repo):
    """Write bytes under the repo and return the path."""
    def _mk(name, data):
        path = os.path.join(str(repo), name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path
    return _mk
