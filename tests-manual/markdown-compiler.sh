#!/bin/bash

set -e

# source the geni.sh script
#scriptpath=$(readlink -f "$0")
#genipath="$(dirname "$scriptpath")/../shell/geni.sh"
#source "$genipath"
eval "$(dic --init)"

# move to a temp folder
tmpdir=$(mktemp -d) && cd "$tmpdir"
#tmpdir=$(mktemp -d) && cd "$tmpdir"&& trap "rm -rf $tmpdir" EXIT 

# print a prompt and run a command
run() {
    echo "\$ $*"
    "$@" || exit $?
    echo 
}

# the actual shell session starts here
run pwd
run git clone https://github.com/mikeizbicki/markdown-compiler.git .
run sh -c "echo 'pytest --doctest-modules .' > test.sh"
run chmod a+x test.sh
run itera --allow-red -c 'fix the test cases'
