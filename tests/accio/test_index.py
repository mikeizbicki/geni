"""The index: its home, its staleness, and the BM25 it answers."""
import io
import os
import time

import pytest

from accio import index
from accio.traverse import DEFAULT_IGNORES, DEFAULT_READMES
from accio.traverse import traverse as t


@pytest.fixture
def conn(repo):
    try:
        c = index.open_([str(repo)])
    except SystemExit as e:  # pragma: no cover
        pytest.skip(str(e))
    yield c
    c.close()


def test_home_honors_index_dir(repo):
    assert index.home([str(repo)], "/tmp/idx") == "/tmp/idx"


def test_home_honors_env(repo, monkeypatch):
    monkeypatch.setenv("ACCIO_INDEX_DIR", "/tmp/env-idx")
    assert index.home([str(repo)]) == "/tmp/env-idx"


def test_home_is_accio_inside_git_common_dir(repo):
    h = index.home([str(repo)])
    assert os.path.basename(h) == "accio"


def test_db_path_is_index_db(repo):
    assert index.db_path([str(repo)]).endswith("/index.db")


def test_xdg_home_when_no_repo(tmp_path):
    import tempfile
    d = tempfile.mkdtemp()
    try:
        h = index.home([d])
        assert h.endswith("/accio/" + index._key([d]))
    finally:
        os.rmdir(d)


def test_refresh_inserts_and_dedups(repo, mk, conn):
    mk("a.py", "x = 1\n")
    args = ([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.refresh(conn, *args)
    n1 = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    row1 = conn.execute("SELECT mtime, size FROM files").fetchone()
    # a second refresh with no change to the tree writes nothing
    index.refresh(conn, *args)
    row2 = conn.execute("SELECT mtime, size FROM files").fetchone()
    n2 = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n1 == n2 == 1
    assert (row1[0], row1[1]) == (row2[0], row2[1])


def test_refresh_picks_up_a_change(repo, mk, conn):
    p = mk("a.py", "x = 1\n")
    args = ([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.refresh(conn, *args)
    old = conn.execute("SELECT id FROM chunks").fetchone()[0]
    time.sleep(0.05)
    with open(p, "w") as f:
        f.write("y = 2\n")
    index.refresh(conn, *args)
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks")]
    assert len(ids) == 1
    assert ids[0] != old


def test_refresh_force_rebuilds(repo, mk, conn):
    mk("a.py", "x = 1\n")
    args = ([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.refresh(conn, *args)
    n1 = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    index.refresh(conn, *args, force=True)
    n2 = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n1 == n2 == 1


def test_refresh_drops_a_gone_file(repo, mk, conn):
    p = mk("a.py", "x = 1\n")
    args = ([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.refresh(conn, *args)
    os.remove(p)
    index.refresh(conn, *args)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_index_matches_the_walk(repo, mk, conn):
    # the cross-module invariant: the ids the index holds are exactly
    # the ids the plain walk yields over the same tree, so ranking and
    # rendering can never disagree about what a chunk is.
    mk("README.md", "root\n")
    mk("a/x.py", "def parse(x): return x\n")
    mk("a/y.py", "def render(y): return y\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    indexed = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    walked = {c.chunk_id for c in t([str(repo)])}
    assert indexed == walked


def test_search_ranks_by_bm25(repo, mk, conn):
    mk("a.py", "def parse(x): return x\n")
    mk("b.py", "def render(y): return y\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    found = list(index.search(conn, "parse"))
    assert found
    assert all(c.path.endswith("a.py") for c in found)


def test_search_is_deterministic(repo, mk, conn):
    mk("a.py", "same word same word\n")
    mk("b.py", "same word same word\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    first = [c.chunk_id for c in index.search(conn, "word")]
    second = [c.chunk_id for c in index.search(conn, "word")]
    assert first == second


def test_prune_removes_the_file(repo, mk):
    try:
        c = index.open_([str(repo)])
    except SystemExit as e:  # pragma: no cover
        pytest.skip(str(e))
    c.close()
    p = index.db_path([str(repo)])
    assert os.path.exists(p)
    index.prune([str(repo)])
    assert not os.path.exists(p)


def test_explain_says_nothing_yet(repo, mk):
    try:
        index.open_([str(repo)]).close()
    except SystemExit as e:  # pragma: no cover
        pytest.skip(str(e))
    p = index.db_path([str(repo)])
    os.remove(p)
    buf = io.StringIO()
    index.explain([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True,
                  None, buf)
    out = buf.getvalue()
    assert "nothing" in out
    assert "would index" in out


def test_explain_reports_a_delta(repo, mk, conn):
    mk("a.py", "x = 1\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    mk("b.py", "y = 2\n")
    buf = io.StringIO()
    index.explain([str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True,
                  None, buf)
    out = buf.getvalue()
    assert "delta: +1 -0 ~0" in out
    assert "b.py" in out
