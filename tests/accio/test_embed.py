"""The dic boundary: --embed, --expand, and the fake model that stands in.

The fake is a module put in `sys.modules` before `select` or `index`
is allowed to `import dic`, so the tests exercise the boundary without
a network round trip, an API key, or a dollar.  It is not a mock: the
functions it names are exactly the two the SPEC names, and a change to
the boundary is a change to what the fake has to carry.
"""
import sys, types

import pytest

from accio import index, select
from accio.traverse import DEFAULT_IGNORES, DEFAULT_READMES


@pytest.fixture
def fake_dic(monkeypatch):
    """A free, offline `dic` whose vectors make the ranking legible."""
    mod = types.ModuleType("dic")
    calls = {"embed": [], "chat": []}

    def embed(model, texts):
        calls["embed"].append((model, list(texts)))
        # a two-element vector whose first coordinate answers "is the
        # letter x here": the query "x" is [1, 0] and any text with an
        # x in it lands on the same side of the sphere, which is the
        # one property every vector test here needs.
        return [[1.0, 0.0] if "x" in t else [0.0, 1.0] for t in texts]

    def chat(model, prompt):
        calls["chat"].append((model, prompt))
        return "xyzzy"

    mod.embed = embed
    mod.chat = chat
    mod.calls = calls
    monkeypatch.setitem(sys.modules, "dic", mod)
    yield mod


@pytest.fixture
def conn(repo):
    try:
        c = index.open_([str(repo)])
    except SystemExit as e:  # pragma: no cover
        pytest.skip(str(e))
    yield c
    c.close()


def test_embed_fills_the_vector_column(repo, mk, fake_dic, conn):
    mk("a.py", "x = 1\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    n = index.embed(conn, "m")
    assert n == 1
    row = conn.execute(
        "SELECT vector, embedder FROM chunks").fetchone()
    assert row["vector"] is not None
    assert row["embedder"] == "m"


def test_embed_is_idempotent(repo, mk, fake_dic, conn):
    mk("a.py", "x = 1\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.embed(conn, "m")
    # a second call embeds nothing: the chunks already carry a vector
    # from this model, and the SPEC's paid part is the delta.
    n = index.embed(conn, "m")
    assert n == 0
    assert len(fake_dic.calls["embed"]) == 1


def test_embed_rebuilds_when_the_model_changes(repo, mk, fake_dic, conn):
    mk("a.py", "x = 1\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    index.embed(conn, "m1")
    index.embed(conn, "m2")
    row = conn.execute("SELECT embedder FROM chunks").fetchone()
    assert row["embedder"] == "m2"


def test_embed_batches_by_BATCH(repo, mk, fake_dic, conn, monkeypatch):
    monkeypatch.setattr(index, "BATCH", 2)
    for i in range(5):
        mk(f"a{i}.py", "x = 1\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    index.embed(conn, "m")
    # five chunks at two a request is three requests, which is the SPEC's
    # "batch size dominates every other cost decision" made testable.
    assert len(fake_dic.calls["embed"]) == 3


def test_veclist_ranks_by_similarity(repo, mk, fake_dic):
    mk("hit.py", "x = 1\n")
    mk("miss.py", "y = 2\n")
    found = list(select.rag("x", [str(repo)], DEFAULT_READMES,
                            DEFAULT_IGNORES, True, embed_model="m"))
    assert found
    assert found[0].path.endswith("hit.py")
    # the miss is still in the list, only below the hit
    paths = [c.path for c in found]
    assert any(p.endswith("miss.py") for p in paths)


def test_search_is_still_free(repo, mk, fake_dic, conn):
    mk("a.py", "parse the query\n")
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES, True)
    # index.search touches no model at all
    list(index.search(conn, "parse"))
    assert fake_dic.calls["embed"] == []
    assert fake_dic.calls["chat"] == []


def test_rewrite_calls_dic_once_per_time(fake_dic):
    out = select.rewrite("hello", model="m", times=3)
    assert len(fake_dic.calls["chat"]) == 3
    assert "hello" in out


def test_rag_without_embed_does_not_import_dic(repo, mk, fake_dic):
    mk("a.py", "parse the query\n")
    list(select.rag("parse", [str(repo)], DEFAULT_READMES,
                    DEFAULT_IGNORES, True))
    assert fake_dic.calls["embed"] == []
    assert fake_dic.calls["chat"] == []


def test_rag_with_expand_rewrites_before_searching(repo, mk, fake_dic):
    mk("a.py", "xyzzy the answer\n")
    found = list(select.rag("anything", [str(repo)], DEFAULT_READMES,
                            DEFAULT_IGNORES, True, expand=1))
    assert fake_dic.calls["chat"]
    # the rewrite is part of the query, so bm25 can find the answer
    # whatever the original asked for: the join is an OR and not an
    # implicit AND, so "anything" and "xyzzy" are alternatives and not
    # both required.
    assert any(c.path.endswith("a.py") for c in found)
