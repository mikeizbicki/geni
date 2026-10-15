"""The walk, its ancestor context, and the chunks it yields."""
import os

from accio.traverse import ancestors, canvassed, walk
from accio.traverse import traverse as t


def rel(root, path):
    return os.path.relpath(path, str(root))


def test_walk_files_before_dirs(repo, mk):
    mk("z.py", "z")
    mk("a/b.py", "b")
    got = [rel(repo, p) for p in walk(str(repo), gitignore=False)]
    # files at each level sort, and each level is depth-first
    assert got == ["z.py", "a/b.py"]


def test_walk_sorts_files_at_each_level(repo, mk):
    mk("b.py", "b")
    mk("a.py", "a")
    mk("c.py", "c")
    got = [rel(repo, p) for p in walk(str(repo), gitignore=False)]
    assert got == ["a.py", "b.py", "c.py"]


def test_ancestors_outermost_first(repo, mk):
    mk("README.md", "root")
    mk("a/README.md", "sub")
    mk("a/b/c.py", "c")
    tops = ancestors(os.path.join(str(repo), "a/b/c.py"))
    root = os.path.join(str(repo), "README.md")
    sub = os.path.join(str(repo), "a/README.md")
    assert root in tops
    assert sub in tops
    assert tops.index(root) < tops.index(sub)


def test_traverse_dedups_ancestors(repo, mk):
    mk("README.md", "root")
    mk("a/x.py", "x")
    mk("a/y.py", "y")
    chunks = list(t([str(repo) + "/a"]))
    paths = [c.path for c in chunks]
    root = os.path.normpath(os.path.join(str(repo), "README.md"))
    assert paths.count(root) == 1
    assert paths.count(os.path.normpath(
        os.path.join(str(repo), "a/x.py"))) == 1
    assert paths.count(os.path.normpath(
        os.path.join(str(repo), "a/y.py"))) == 1


def test_traverse_yields_readme_first(repo, mk):
    mk("README.md", "root")
    mk("a/x.py", "x")
    chunks = [c.path for c in t([str(repo) + "/a"])]
    root = os.path.normpath(os.path.join(str(repo), "README.md"))
    x = os.path.normpath(os.path.join(str(repo), "a/x.py"))
    assert chunks.index(root) < chunks.index(x)


def test_canvassed_matches_traverse(repo, mk):
    mk("README.md", "root")
    mk("a/x.py", "x")
    paths = set(canvassed([str(repo) + "/a"]))
    walked = {c.path for c in t([str(repo) + "/a"])}
    assert paths == walked


def test_gitignore_consulted(repo, mk):
    mk(".gitignore", "secret.txt\n")
    mk("kept.py", "ok")
    mk("secret.txt", "no")
    got = [rel(repo, p) for p in walk(str(repo), gitignore=True)]
    assert "kept.py" in got
    assert "secret.txt" not in got


def test_gitignore_not_consulted(repo, mk):
    mk(".gitignore", "secret.txt\n")
    mk("secret.txt", "no")
    got = [rel(repo, p) for p in walk(str(repo), gitignore=False)]
    assert "secret.txt" in got


def test_binary_skipped_in_silence(repo, mk, mkbytes):
    mk("kept.py", "ok")
    mkbytes("bin.dat", b"\x00\x01\x02")
    # the walk sees bin.dat -- it is a file -- and traverse reads it,
    # finds the NUL, and yields nothing for it.
    assert "bin.dat" in [rel(repo, p) for p in walk(
        str(repo), gitignore=False)]
    paths = [c.path for c in t([str(repo)])]
    assert not any(p.endswith("bin.dat") for p in paths)
    assert any(p.endswith("kept.py") for p in paths)
