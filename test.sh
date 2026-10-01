#!/bin/bash
#
# The one command a developer types, and the command itera runs.
#
# Every stage is labelled, and every stage's tool must be installed: a
# missing pytest or bats is a failure that names how to get it, never a
# skip.  A skip reads as a green run that covered less than it claims, and
# itera's pre-flight -- which asserts this tree is green before the model
# touches it -- would then pass on a tree nothing checked.
#
# A stage is added here, and not to a list elsewhere, once the tree is
# clean under it: mypy and flake8 belong on this list when the tree passes
# them, and a stage added before that is a pre-flight no itera can pass.
#
# set -x is the shell's failure frame: the command that failed is echoed
# with its variables expanded, so the last line of a log is the line to
# read, and set -e stops there instead of burying it.

set -eux

cd "$(dirname "$0")"

need() {
    command -v "$1" >/dev/null || {
        printf 'test: %s is not installed; %s\n' "$1" "$2" >&2
        exit 1
    }
}

echo '=== pytest ==='
need pytest 'pip install pytest'
pytest --doctest-modules src/ tests/

echo '=== bats ==='
need bats 'git clone https://github.com/bats-core/bats-core && sudo bats-core/install.sh /usr/local'
bats --trace --print-output-on-failure tests/shell/*.bats
