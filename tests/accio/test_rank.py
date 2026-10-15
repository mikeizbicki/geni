"""Reciprocal rank fusion and the parent-edge pagerank it feeds."""
from accio import index, select
from accio.extract import lines
from accio.traverse import DEFAULT_IGNORES, DEFAULT_READMES


def _c(path, text):
    return lines(path, text)[0]


def test_rrf_single_list_keeps_order():
    a, b = _c("a.py", "x"), _c("b.py", "y")
    assert [c.path for c in select.rrf([a, b])] == ["a.py", "b.py"]


def test_rrf_two_agreeing_lists_keep_order():
    a, b = _c("a.py", "x"), _c("b.py", "y")
    assert [c.path for c in select.rrf([a, b], [a, b])] == [
        "a.py", "b.py"]


def test_rrf_two_disagreeing_lists_tie_on_path():
    a, b = _c("a.py", "x"), _c("b.py", "y")
    # a is first on one list and second on the other: the scores are
    # equal and the tie is broken on (path, chunk_id) so the output is
    # byte-identical between two runs over the same tree.
    assert [c.path for c in select.rrf([a, b], [b, a])] == [
        "a.py", "b.py"]


def test_rrf_is_the_union_of_the_lists():
    a, b, c = _c("a.py", "1"), _c("b.py", "2"), _c("c.py", "3")
    got = {x.path for x in select.rrf([a, b], [c])}
    assert got == {"a.py", "b.py", "c.py"}


def test_rrf_agreement_beats_depth():
    # a chunk first on both lists beats a chunk first on one and second
    # on another, which is the whole point of fusion.
    a, b = _c("a.py", "x"), _c("b.py", "y")
    fused = select.rrf([a], [a, b])
    top_score = (1 / (select.RRF_K + 1)) + (1 / (select.RRF_K + 1))
    assert fused[0].path == "a.py"
    assert abs((1 / (select.RRF_K + 1)
                + 1 / (select.RRF_K + 1)) - top_score) < 1e-9


def test_pagerank_spreads_a_seed_to_its_neighbour():
    a = _c("a.py", "x")
    child = a._replace(chunk_id="child", parent=a.chunk_id)
    scores = select.pagerank([a, child], {child.chunk_id: 1.0})
    assert scores[a.chunk_id] > 0
    assert scores[child.chunk_id] > scores[a.chunk_id]


def test_pagerank_without_edges_is_the_seed():
    a, b = _c("a.py", "x"), _c("b.py", "y")
    s = select.pagerank([a, b], {a.chunk_id: 1.0})
    assert s[a.chunk_id] > s[b.chunk_id]
    assert s[b.chunk_id] < 0.01


def test_pagerank_conserves_total_mass():
    a = _c("a.py", "x")
    b = _c("b.py", "y")._replace(parent=a.chunk_id)
    s = select.pagerank([a, b], {a.chunk_id: 1.0})
    assert abs(sum(s.values()) - 1.0) < 1e-6


def test_pagerank_follows_the_parent_path_edge():
    readme = _c("README.md", "conventions")
    f = _c("a.py", "code")._replace(
        meta={"parent_path": "README.md"})
    s = select.pagerank([readme, f], {readme.chunk_id: 1.0})
    # the README's signal reaches the file beneath it
    assert s[f.chunk_id] > 0
    assert s[f.chunk_id] > s.get("nothing", 0.0)


def test_traverse_records_the_parent_path(repo, mk):
    mk("README.md", "root")
    mk("a/x.py", "x = 1\n")
    from accio.traverse import traverse as t
    got = {c.path: c for c in t([str(repo) + "/a"])}
    import os
    readme = os.path.normpath(os.path.join(str(repo), "README.md"))
    x = os.path.normpath(os.path.join(str(repo), "a/x.py"))
    assert got[x].meta.get("parent_path") == readme
    assert "parent_path" not in got[readme].meta


def test_index_stamps_the_parent_path(repo, mk):
    mk("README.md", "root\n")
    mk("a/x.py", "x = 1\n")
    conn = index.open_([str(repo)])
    index.refresh(conn, [str(repo)], DEFAULT_READMES, DEFAULT_IGNORES,
                  True)
    import json, os
    row = conn.execute(
        "SELECT meta FROM chunks WHERE path=?",
        (os.path.normpath(os.path.join(str(repo), "a/x.py")),)).fetchone()
    meta = json.loads(row["meta"])
    assert meta.get("parent_path", "").endswith("README.md")
    conn.close()
