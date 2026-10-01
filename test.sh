#!/bin/sh
# this is the CI entry point to prevent regressions;
# it runs *all* tests everywhere in the project
set -eux

pytest -q
bats --trace --print-output-on-failure tests/shell/*.bats
