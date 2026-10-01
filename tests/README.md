# tests

The tree is split by what it tests: `tests/dic` holds the python tests of
the `dic` program, and everything beside this file is the shell side, where
the tools under `scripts/` are tested.

`./test.sh` is the one command a developer types and the command `itera`
runs; it labels a stage per tool.  A new stage -- mypy, flake8 -- goes
there once the tree is clean under it, because itera's pre-flight runs this
script and a stage the tree cannot pass is a stage no round can start from.

## dic

Most of `dic` is pure functions, and those are tested where they live: a
docstring with a `>>>` in it is the test, and `pytest --doctest-modules` runs
them.  A new example goes next to the function it describes, not in here.

What is left is what a doctest cannot show: sqlite, a config file, a socket, a
thread, a terminal.  That is what `tests/dic` holds, and nearly every test in
it is one `dic()` call -- a prompt in, an answer out, a row written --
because that is the unit a user has and the unit a regression breaks.

The rules:

* **fake the network, and nothing else.**  `conftest.py` runs a real HTTP
  server on 127.0.0.1 answering canned SSE, so the config parse, the sqlite
  file, the request body, the stream and the row are all the real thing;
* **one test, one call, one reason to fail.**  A comment that says "and now
  also" means the test is two tests;
* **assert on what a user sees**: stdout, stderr, the row, the request that
  went out.  Never a local variable;
* **libraries are fine here.**  Nothing in this directory is on the latency
  path, so timings do not matter -- though the fixtures still need none:
  `http.server` and `threading` are stdlib.
* **start a process only to test the process.**  An in-process `dic()` call
  cannot see startup, because pytest has paid every import before the test
  runs; `test_startup.py` therefore runs a real `python -m dic`.  It is
  `@pytest.mark.slow`, so the default run leaves it out.
* **one fixture set per shape of protocol.**  `test_client.py`'s server answers
  every POST with SSE and is the right server for every protocol that is one
  request and one stream; `test_multimedia.py` answers GET and PUT too, because
  a job that is submitted, polled and downloaded is a different shape and not
  a different assertion.

If a test needs three sentences of setup to be believed, the setup is the bug.


## shell

The tools under `scripts/` are bash, and bats is what tests bash.
`tests/shell/*.bats` is one file per tool, and `./test.sh` runs each one
directly, so a bats report reaches the log without pytest's report around
it.  A missing bats, or a `tests/shell` with no suites in it, is a failure
and not a skip: an empty collection reads as a green run that covered less
than it claims.

Two things are faked in a shell test and nothing else is.  `tests/bin/dic`
replays a recorded reply, and `tests/bin/bwrap` records the argument list
`sandbox()` builds.  The temporary `git init` repository the test tears
down, the `git` a developer has, the script under test, and the shell that
runs them are all real.

`tests/bin/bwrap` is there because a real jail cannot be made from inside
`sandbox pytest`: the outer sandbox installs a filter that refuses the
`unshare` and `mount` a jail is made of.  The argument list is the whole
contract with bwrap, since bwrap is what does the jailing, so that is what
the sandbox suite checks.


## recording

A transcript is one reply from the real model, captured once and replayed
thereafter.  `tests/record.sh` is what captures it: it calls `dic` the way
`committe` calls it -- the same `-s "$(committe-prompt)"` -- and writes what
came back to `tests/fixtures/transcripts/<case>/dic.stdout`, with the status
it exited with in `dic.exit` beside it.  A test names the case through
`FAKE_DIC_CASE`; `tests/bin/dic` replays it and never touches a provider.

Everything after the case name is the request, so the arguments are the
ones a `committe` invocation would take:

    $ tests/record.sh add-file -m groq+qwen 'create a file primes.py'
    $ tests/record.sh question -m anthropic+sonnet 'make the tests faster'
    $ tests/record.sh offline -m no-such-model 'add a docstring to add()'

Record when a new case is wanted -- the reply a live model gives is the one
to freeze, not a reply written by hand -- when `committe-prompt` changed and
the old reply no longer matches what a live call would say, or when a
failure has to be pinned, since `dic.exit` carries the status and a
rate-limited or unknown-model call is a transcript too.

Do not record to make a failing test pass.  A fixture that no longer
matches the prompt is the bug the test found, so look at both sides before
refreshing either.  Do not put `record.sh` in CI: it calls the model, and
the replay side is the only part that is free.
