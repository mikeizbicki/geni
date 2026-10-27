"""The CLI: a flag reaches the pipeline it names."""
import sys, types

import pytest

from accio.__main__ import main


@pytest.fixture
def fake_dic(monkeypatch):
    """A free, offline `dic`, so a paid flag can be tested in pytest."""
    mod = types.ModuleType("dic")
    mod.embed = lambda model, texts: [[1.0, 0.0] for _ in texts]
    mod.chat = lambda model, prompt: "rewritten"
    monkeypatch.setitem(sys.modules, "dic", mod)
    return mod


def test_target_truncates_the_prompt(repo, mk, capsys):
    mk("a.py", "x = 1\n")
    mk("b.py", "y = 2\n")
    mk("c.py", "z = 3\n")
    assert main(["--target", "1", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.endswith("a.py\n---\nx = 1\n---\n")
    assert out.count("---\n") == 2


def test_no_target_keeps_everything(repo, mk, capsys):
    mk("a.py", "x = 1\n")
    mk("b.py", "y = 2\n")
    assert main([str(repo)]) == 0
    assert capsys.readouterr().out.count("---\n") == 4


def test_bad_target_names_the_flag(repo, mk, capsys):
    mk("a.py", "x = 1\n")
    assert main(["--target", "eight", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "--target" in err
    assert "eight" in err


def test_fudge_widens_the_cap(repo, mk, capsys, monkeypatch):
    mk("a.py", "x = 1\n")
    mk("b.py", "y = 2\n")
    # an absolute path makes the breadcrumb the whole /tmp path, which
    # dwarfs the target; chdir first so the arithmetic is a.py's.
    # "a.py\n---\nx = 1\n---\n" is 19 bytes: 19 + 19 = 38 against 35,
    # three over, inside a quarter.
    monkeypatch.chdir(repo)
    assert main(["--target", "35", "."]) == 0
    assert capsys.readouterr().out.count("---\n") == 4
    assert main(["--target", "35", "--fudge", "0", "."]) == 0
    assert capsys.readouterr().out.count("---\n") == 2


def test_embed_alone_builds_and_exits(repo, mk, capsys, monkeypatch,
                                      fake_dic):
    mk("a.py", "x = 1\n")
    monkeypatch.chdir(repo)
    assert main(["--embed", "m", "."]) == 0
    out = capsys.readouterr().out
    assert "embedded" in out
    assert "---\n" not in out       # no prompt


def test_bad_expand_names_the_flag(repo, mk, capsys):
    mk("a.py", "x = 1\n")
    assert main(["--rag", "q", "--expand", "three", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "--expand" in err
    assert "three" in err


def test_expand_only_asks_the_model(repo, mk, capsys, fake_dic):
    # the fake's chat returns "rewritten"; the file must contain it,
    # or the rewrite is invisible to the ranking and the test asserts
    # on an empty prompt that would pass with the bug and fail without.
    mk("a.py", "rewritten x = 1\n")
    assert main(["--rag", "q", "--expand", "1", str(repo)]) == 0
    assert capsys.readouterr().out


def test_elide_body_at_the_cli(repo, mk, capsys, monkeypatch):
    mk("a.py", "def f(a):\n    '''doc.'''\n    return a\n")
    monkeypatch.chdir(repo)
    assert main(["--elide", "kind=body", "a.py"]) == 0
    out = capsys.readouterr().out
    assert "def f(a):" in out
    assert "'''doc.'''" in out
    # the def chunk's body is gone; the file chunk is empty here
    assert "return a" not in out


def test_elide_body_without_body_kind_is_a_noop(repo, mk, capsys, monkeypatch):
    mk("a.py", "def f(a):\n    return a\n")
    monkeypatch.chdir(repo)
    assert main(["a.py"]) == 0
    before = capsys.readouterr().out
    assert main(["--elide", "somethingelse", "a.py"]) == 0
    after = capsys.readouterr().out
    assert before == after


def test_format_at_paths_at_the_cli(repo, mk, capsys, monkeypatch):
    mk("a.py", "x = 1\n")
    monkeypatch.chdir(repo)
    assert main(["--format", "at-paths", "a.py"]) == 0
    out = capsys.readouterr().out
    assert "@@a.py\n" in out
    assert "x = 1" not in out
