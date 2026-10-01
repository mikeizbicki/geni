"""Run the bats suites for the shell tools.

The tools under scripts/ are bash, and bats is what tests bash.
tests/shell/*.bats is one file per tool, and this module runs each one as a
parametrized case so that pytest stays the one command a developer types.

Nothing here is skipped, and nothing here is collected only when a runner
happens to be installed.  A skip and an empty parameter list are both
invisible in a green run, and this module is the whole of the shell
testing: a missing bats, or a tests/shell with no suites in it, fails and
names the fix.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SHELL = HERE / "shell"
SUITES = sorted(SHELL.glob("*.bats"))

# The runner, by name, whether or not it is there: a missing one is then a
# clear message and not a None where a command should be.
BATS = shutil.which("bats")

# A suite list with nothing in it still leaves one case to run, so that a
# tests/shell which lost its files is a failure and not an empty collection
# that a green run would hide.
CASES = SUITES or [None]
IDS = [p.stem if p is not None else "no-suites" for p in CASES]

# bats is one script and not a library, and /usr/local is where it has to
# be: sandbox(1) binds /usr read-only and next to nothing under $HOME, so a
# bats in ~/.local/bin runs under plain pytest and is missing under
# `sandbox pytest`, which is the run that has to cover the shell too.
NO_BATS = (
    "bats is not installed, so the shell tests cannot run: "
    "git clone https://github.com/bats-core/bats-core && "
    "sudo bats-core/install.sh /usr/local"
)

NO_SUITES = f"no .bats suites found under {SHELL}"


@pytest.mark.parametrize("suite", CASES, ids=IDS)
def test_bats(suite):
    if suite is None:
        pytest.fail(NO_SUITES)
    if BATS is None:
        pytest.fail(NO_BATS)

    # check=True would report a status and nothing else.  The suite prints
    # the report, so it is captured and shown with the failure, and
    # --print-output-on-failure follows each failing `run` with the stdout
    # and stderr it captured -- the half of the report that says what the
    # tool under test actually printed.
    done = subprocess.run([BATS, "--print-output-on-failure", str(suite)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.fail(
            f"{suite.name} failed ({done.returncode}):\n"
            f"{done.stdout}{done.stderr}",
            pytrace=False,
        )
"""Run the bats suites for the shell tools.

The tools under scripts/ are bash, and bats is what tests bash.
tests/shell/*.bats is one file per tool, and this module runs each one as a
parametrized case so that pytest stays the one command a developer types.  A
missing bats is a skip and not a failure: the shell tests are the only thing
it runs, and there is nothing here to see without it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SUITES = sorted((HERE / "shell").glob("*.bats"))

BATS = shutil.which("bats")


