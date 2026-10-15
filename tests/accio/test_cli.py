"""The CLI: a flag reaches the pipeline it names."""
from accio.__main__ import main


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
