#!/bin/sh
# this is the CI entry point to prevent regressions;
# it runs *all* tests everywhere in the project
set -eux

ruff check .
bandit -c pyproject.toml -r . -ll

pytest -q
bats --trace --print-output-on-failure tests/shell/*.bats
