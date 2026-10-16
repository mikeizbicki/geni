# TODO

## Nested script format

The `+` line is the transcript's fixed output: uncoloured, so a transcript
reads the same in a terminal and in a file.  A nested harness's *output* is
repainted in the `muted` role (see `AGENTS.md`), so it recedes behind the
trace -- but the trace itself does not yet say how deep a command sits.

### The problem

`committe` inside `itera` prints the same `+` as a top-level `committe`:

    + itera 'a request'
    + committe 'a request'
    + dic --prepend-prompt '...' '...'
    cost: $0.0019 (input: $0.0007, output: $0.0012) --mid=01M...
    + committe-apply-patch .git/geni/committe/patchfile auto
    + git commit -m '[geni] add fib.py'

Nothing in the trace says the second and third lines belong to the first.
A reader has to read the muted output below them to find out -- which is
exactly the output the muted role exists to let them skip.

### Options

1. **Repeat the `+`.**  `++` at depth 1, `+++` at depth 2.  One character
   per level, so the eye counts without reading.  Costs: the `+` is no
   longer a single fixed byte, so a transcript's prefix is a regexp and
   not a literal.

2. **Indent the trace.**  `+   ` at depth 1, `+      ` at depth 2.  The
   `+` stays one byte, so the transcript rule is untouched.  Costs: a deep
   call wraps on a narrow terminal, and the indentation is ambiguous with
   a command whose own output is indented.

3. **Number the levels.**  `+1`, `+2`.  Unambiguous, and a machine reads
   the depth off the line without counting.  Costs: it reads as a count
   and not as a prompt.

4. **A depth column.**  A fixed-width gutter left of every traced line,
   as `git log --graph` draws one.  The gutter can draw the nesting of
   *commands* and not only their depth.  Costs: the gutter is on every
   line including the top-level ones, and `+ ` is no longer at column one.

5. **Colour the `+`.**  Rejected: colour never carries information the text
   does not already have, and a transcript written to a file loses the
   level entirely.

### What is not settled

- **Where the level comes from.**  The slashes in `DIC_SESSION` give the
  session depth for free, but a harness does not always fork: `itera` does
  not, and `committe` now sets the child session on the one `dic` call and
  never exports it, so the `git commit` beside it runs at the caller's
  depth.  A `DIC_DEPTH` variable bumped at each harness entry would make
  the trace depth and the session depth agree; the cost is a second piece
  of state to keep in step with the first, and a way to restore it on
  every return path.  `FUNCNAME` is the other source: counting the harness
  frames on the bash call stack needs no state at all, at the cost of
  being magical and of depending on the harness functions keeping their
  names.
- **Whether the level belongs on the trace, on the output, or on both.**
- **Whether a transcript pasted into a model keeps the levels.**  The
  model reads the muted output as flat, so the levels are for the human
  and not for the model.
