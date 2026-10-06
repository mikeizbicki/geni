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

Naming:
- all scripts should be sourced
- they should include a primary function with the same name as the script
- helper functions should follow the script-* convention

Output formatting:
- output of a program should "look like" a shell session
    - alternating commands (with PS4-like prompt) and their output
      (nothing else should be output)
    - we should be able to copy/paste transcript into llm for debugging
      and it will include all needed context
- only "major" commands get traced via dic-run
    - standard set -x is too noisy
    - we don't want conditional branches, variable declarations, etc in the trace
