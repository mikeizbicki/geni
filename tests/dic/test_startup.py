"""Startup, measured where it can be seen: in a process of its own.

Every other test in this directory calls dic() inside pytest, where the
interpreter is already up and every import is already paid -- which is
exactly the cost this program exists to keep small.  So these tests start a
process instead, and one of them refuses site-packages outright.
"""
import os
import pytest

from dic.store import db

STARTUP_MS = 60     # a budget, not a measurement: a vendor SDK costs ~50ms


def test_startup_needs_only_the_standard_library(dic_run):
    """-S drops site-packages, so an SDK import cannot even load."""
    result = dic_run("--help", python_flags=["-S"])

    assert result.stdout.startswith(b"usage: dic")


@pytest.mark.slow
def test_startup_fits_its_budget(dic_run, env, model):
    """t_request - t_start is dic's startup, stamped before the response.

    The fake server's latency is therefore not in it, the adaptor import and
    the config and history queries are, and the minimum of a few runs keeps a
    busy machine from being charged to dic.  A smoke alarm for a new
    dependency, not a benchmark.
    """
    for _ in range(3):
        dic_run("hi", "-m", model)

    conn = db(env)
    elapsed = conn.execute(
        "SELECT min(t_request - t_start) FROM messages").fetchone()[0]
    conn.close()
    assert elapsed < STARTUP_MS * 1e6


def test_init_names_a_dic_sh_that_is_there(dic_run):
    """--init names a dic.sh a shell can source, in a checkout as in an install.

    The scripts are the package `dic.scripts` in an install and a sibling
    directory of src/ in a checkout, and --init has to name them in both,
    because what it prints is a `source` line a .bashrc evals: a path that
    is not there is a shell that starts with no dic at all.
    """
    line = dic_run("--init").stdout.decode()
    assert line.startswith("source ")
    path = line.split()[1].strip("'")
    assert os.path.basename(path) == "dic.sh"
    assert os.path.isfile(path)


@pytest.mark.slow
def test_a_prompt_with_no_at_does_not_import_accio(dic_run, model):
    """The @@ import is lazy: a prompt with no mention pays nothing.

    -X importtime writes one line per module the process loads, and the
    line for accio.include is the one this test is about: dic imports it
    only when a prompt names a file with @@, so a call that does not
    does not carry one of accio's modules on its ttft path.
    """
    result = dic_run("hi", "-m", model, python_flags=["-X", "importtime"])
    assert b"accio.include" not in result.stderr