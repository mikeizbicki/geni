Latin language scripts are all wrappers around dic (or other scripts that wrap dic).
- A wrapper must never swallow a flag meant for what it wraps.
- Parse the wrapper's flags left to right.
- The first `--` ends them; the rest goes verbatim to the wrapped command.
- An unknown flag passes through, not rejected, so a flag added to `dic`
  works here unedited.
- `committe -- -f` names the wrapped `-f` when both define one.
- `dic`'s prompt is one argument, so a wrapper's own instructions travel in
  `--prepend-prompt` and never as extra words on the line; an unknown flag
  is an error and is never read as prose.

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

Colour:
- every line belongs to a role, and the role is what is coloured, never the
  code path that wrote it
- colour amplifies; it never carries information the text does not already
  have
- the palette is GENI_COLORS, and the roles are:

      error    a failure
      warning  a recoverable one
      hint     an aside, fainter than what it follows
      info     progress: reasoning, a pv meter
      muted    a nested command's own output, stripped and repainted
      payload  the model's own output
      cost     the cost summary
      id       a message id, as git colours a hash
      ref      a decoration, as git colours a ref
      trace    reserved by the transcript rule, below

- `+` is reserved: it is the transcript's fixed output, so it carries no
  role and is never coloured.  The reset around it is what makes it the
  terminal's own default foreground, so a transcript reads the same in a
  terminal and in a file

Severity prefixes:
- `prog-error:` a failure, `prog-warning:` a recoverable one, `prog-hint:`
  an aside -- where `prog` names the command that printed it
- one function per prefix (dic-error, dic-warn, dic-hint), so the wording
  and the colour are each stated once

Tracing:
- only a "major" command is traced with dic-run.  A command is major when
  it invokes a model, mutates the git tree visibly, or runs user-supplied
  code
- set -x is not this: it prints a wrapper's conditional branches, variable
  declarations and other internals, which are not part of what it did
- a fallback or retry is one logical step: trace the step, not each attempt
- a subshell inside an argument is never traced
- a nested command's output is *replaced*, not dimmed: dic-run strips every
  escape the program wrote and wraps the whole block in one role, so a
  program that colours unconditionally cannot leak its codes into a
  transcript that is meant to read as one.  Only when stdout is a terminal
  -- a redirected stdout is a patchfile or a test's own output, and an
  escape in either is corruption of what the next stage reads

The four-step harness pattern:
- NAME-validate   pure: is this run possible at all
- NAME-prepare    writes only under .git/geni/NAME/
- NAME-run        calls dic
- NAME-record     the only step that moves a ref or the session pointer
- a fifth function, NAME-fail, is not a step: it prints the resume line
- committe is the worked example: validate is the clean-tree check, prepare
  writes the patchfile and records the session head, run is the dic call,
  and record applies the patch and commits
