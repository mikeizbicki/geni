# AGENTS.md

Latin language scripts are all wrappers around dic (or other scripts that wrap dic).
- A wrapper must never swallow a flag meant for what it wraps.
- Parse the wrapper's flags left to right.
- The first `--` ends them; the rest goes verbatim to the wrapped command.
- An unknown flag passes through, not rejected, so a flag added to `dic`
  works here unedited.
- `committe -- -f` names the wrapped `-f` when both define one.
- Every word `dic` does not read as an option is a prompt word, wherever it
  sits on the line, so a wrapper may pass its own instructions as one more
  argument in front of a request and never has to know which of the rest are
  flags and which are the request.
