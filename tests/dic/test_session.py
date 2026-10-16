"""The ref verbs: `rev-parse`, `branch`, `update-ref`.

A session is a ref into the message tree -- a file whose contents are a mid,
exactly as a git branch is a file whose contents are a commit -- and these
are the verbs that read, fork, move and delete one.  test_client.py tests
the call that *writes* a ref; these test the plumbing a harness script uses
instead of deriving the name-to-file rule for itself.

Nearly every test here is still one `dic()` call plus one ref command,
because the property worth asserting is that the two agree: a fork is
transparent only if `-c` in the child reads what `-c` in the parent read.
"""
import os, subprocess, sys

import pytest

from dic import commands
from dic.client import dic
from dic.store import db, session_head, session_move, session_names
from dic.tty import DicError

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   os.pardir, os.pardir, "src")


def ref(env, command, **kwargs):
    """One ref command through the dispatch, as the CLI reaches it."""
    return commands.COMMANDS[command](db(env), env, **kwargs)


def cli(env, *argv):
    """One `python -m dic`, however it exits: the parser, not the function."""
    return subprocess.run(
        [sys.executable, "-m", "dic", *argv],
        env={**os.environ, **env, "PYTHONPATH": SRC},
        stdin=subprocess.DEVNULL, capture_output=True)


def test_rev_parse_answers_what_continue_would(streams, env, sse, model):
    """The readout is the ref, and the ref is what -c reads."""
    reply = dic("hi", model=model, env=env, out=streams.out, err=streams.err)

    assert ref(env, "rev-parse") == reply.mid + "\n"
    assert session_head(env) == reply.mid


def test_rev_parse_refuses_a_session_that_has_none(env):
    with pytest.raises(DicError, match="no such session"):
        ref(env, "rev-parse", name="never-used")


def test_a_fork_copies_the_ref_so_continue_reads_it(streams, env, sse, model,
                                                    events):
    """A child session is transparent: -c in it continues the parent's turn.

    The ref is copied and not linked, and that is the whole point -- the
    child's own file holds the parent's mid, so `-c`, `--mid HEAD` and a
    bare `dic` all resolve exactly as they did in the parent, and none of
    them has to know that a fork happened.
    """
    dic("one", model=model, env=env, out=streams.out, err=streams.err)
    ref(env, "branch", name="child")

    sse.events = events("second")
    dic("two", model=model, cont=True, env={**env, "DIC_SESSION": "child"},
        out=streams.out, err=streams.err)

    assert sse.received[-1].body["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "two"}]
    # and the parent's ref did not move: a fork is a branch and not a rename
    assert session_head(env, "child") != session_head(env)


def test_update_ref_moves_a_ref_and_refuses_a_stale_old(env):
    """OLD is the compare-and-swap a merge-back needs, and it is git's own.

    The case it is for is the one `--ff-only` is for: a fork that started
    when the parent held one mid, and a merge that ends after something else
    moved it.
    """
    session_move(env, "01A", "parent")
    ref(env, "branch", name="child", start="parent")
    session_move(env, "01B", "child")

    ref(env, "update-ref", name="parent", new="01B", old="01A")
    assert session_head(env, "parent") == "01B"

    session_move(env, "01C", "parent")
    with pytest.raises(DicError, match="expected 01A"):
        ref(env, "update-ref", name="parent", new="01D", old="01A")
    assert session_head(env, "parent") == "01C"     # and nothing moved


def test_update_ref_without_old_moves_whatever_it_holds(env):
    session_move(env, "01A", "s")
    ref(env, "update-ref", name="s", new="01B")
    assert session_head(env, "s") == "01B"


def test_update_ref_d_removes_a_ref(env):
    session_move(env, "01A", "child")
    ref(env, "update-ref", name="child", delete=True)
    assert session_names(env) == []


def test_branch_delete_refuses_this_shells_own_session(env):
    """git refuses to delete the branch it is on, and so does this."""
    session_move(env, "01A", "test")            # the env fixture's session
    with pytest.raises(DicError, match="this shell's session"):
        ref(env, "branch", name="test", delete=True)
    assert session_head(env, "test") == "01A"

    ref(env, "branch", name="test", force=True)
    assert session_head(env, "test") is None


def test_branch_delete_refuses_a_parent_with_children(env):
    """The hierarchy is the point, so deleting a level is refused by default."""
    session_move(env, "01A", "parent")
    session_move(env, "01B", "parent/child")

    with pytest.raises(DicError, match="sub-sessions"):
        ref(env, "branch", name="parent", delete=True)
    assert session_names(env) == ["parent", "parent/child"]


def test_branch_draws_the_tree_the_names_spell(env):
    for name in ("a", "a/b", "a/b/c", "x"):
        session_move(env, "01A", name)
    assert ref(env, "branch") == "a\n  a/b\n    a/b/c\nx\n"


def test_the_verbs_are_reachable_from_the_cli(env):
    """The dispatch and the parser, which a direct call to the function skips.

    A `dest` that does not match the parameter name is the kind of mistake
    only the real parser can make, so this is the one test that starts a
    process.
    """
    session_move(env, "01A", "parent")

    assert cli(env, "rev-parse", "parent").stdout == b"01A\n"
    assert cli(env, "branch").stdout == b"parent\n"
    assert cli(env, "branch", "child", "parent").stdout == b""
    assert cli(env, "update-ref", "child", "01B", "01A").stdout == b""
    assert cli(env, "rev-parse", "child").stdout == b"01B\n"
