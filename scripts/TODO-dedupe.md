# TODO

## Deduplicating a continued conversation

The same bytes are sent twice and paid for twice, in two places.

**committe.**  Every round hands `dic` the same instruction text through
`--prepend-prompt "$(committe-prompt)"`.  With `-c` the round-2 request
therefore carries round 1's instructions in the history *and* a fresh
copy in front of the new request: the model reads one prompt twice and
the caller buys its input tokens twice.

**accio.**  `bundle()` already deduplicates the ancestor walk *within*
one run, so `accio src/` prints `./README.md` once.  Nothing
deduplicates *across* runs.  In

    dic <<eof
    $(accio src/)
    ...
    eof

    dic -c <<eof
    $(accio tests/)
    ...
    eof

`./README.md` is in the first prompt's bytes and in the second's, so it
is in the conversation twice at twice the input rate.

### The shape of the problem

Both are the same thing.  A turn's prompt is a concatenation of named
pieces -- a prepended instruction text, a file's contents under its own
path in the `path` / `---` / `text` / `---` encoding, whatever the
caller typed, whatever arrived on stdin -- and a piece whose bytes an
ancestor turn already carries is sent again nonetheless.  A general
solution identifies a piece, compares it (or a hash of it) against what
the conversation already holds, and either omits it or replaces it with
a reference.

`dic` is the one that holds the conversation, so the fix is `dic`'s;
the callers only decide whether a piece can be named.

### Questions to answer before writing code

1. **What is the unit?**  The whole prompt, a `path` `---` block, a
   chunk within one (the content-addressed id `accio`'s SPEC already
   proposes, `sha256(breadcrumb + "\n" + text)`), or one prepended
   text?  Several of these appear twice in practice, and one mechanism
   may not cover them all.
2. **What identifies a piece across turns?**  Content hash alone loses
   the path -- two directories' `README.md` dedupe to one.  Path alone
   misses an edited file the model must see.  A pair `(path, hash)` is
   probably right, with `(path, mtime, hash)` as the conservative form;
   neither is available unless the caller declares the path, because
   `dic` cannot reliably parse `path` / `---` out of an arbitrary
   prompt -- the format is not escaped.
3. **Who declares the pieces?**  Either the caller does (`dic --src
   path/to/file` alongside the existing `--prepend-prompt`), or `dic`
   is taught the format (`--prompt-format accio`), or each caller
   dedupes on its own -- impossible for `accio`, which cannot see the
   conversation.
4. **Where is the omission applied?**  In the caller before the bytes
   are built, or in `dic` when the request body is built, where the
   ancestor chain is already one recursive query away.
5. **What does the model see in place of an omitted piece?**  Nothing
   (it may not know that a file it read a turn ago is still in its
   context), or a placeholder naming the piece and where it was
   (`[already read: a/README.md, above]`), which is itself tokens and
   whose wording the model must not read as a new fact.  Whatever the
   answer, never omit the *last* occurrence of a piece, and never omit
   one the model has asked about.
6. **What does the log say?**  `--log`'s prompt column and `--show`'s
   `prompt` label read `user`, the bytes that were sent.  A deduplicated
   turn sent less than the caller wrote, so the column either lies about
   what was written or loses what was sent.  Both can be kept.
7. **Caching.**  A `-c` that dedupes and appends only the new piece
   leaves the cached prefix byte-identical; nothing changes.  A dedupe
   that *rewrites* an ancestor -- the only way the model does not see
   the same bytes twice anywhere -- breaks the prefix and the next call
   pays a full cache write.  Which is cheaper depends on model and TTL,
   and is a question rather than an assumption.

### The shape the answer will probably take

A new column on `messages`, `prompt_blocks`: a JSON list of `{kind,
key, hash, bytes, ordinal}`, where `kind` is `prepend`, `stdin`,
`attach` or `source`, `key` is the path or the caller's label, `hash`
is a content hash, `bytes` a length, and `ordinal` says where in `user`
the block sits.  `dic` then, building a request, walks the ancestor
chain the way `history()` already does, collects the blocks those turns
held, and drops a current-turn block whose `(kind, key, hash)` an
ancestor already holds -- unless the caller says otherwise.  No second
ledger and no second rule; the same recursive walk answers "who is my
conversational past" and "what is already in it".

`committe` then changes by nothing at all: its instructions are one
`--prepend-prompt` whose key is constant, so the second call drops them
and the model reads them once.  `accio` gains a mode that names the
files it would have printed instead of printing them (`--src`), and
`dic --src` records each; a file whose hash matches an ancestor's is
sent as a reference and not as its bytes.  The `seen` set inside one
`accio` run is unaffected -- it is the cross-run case this adds.

None of this is decided.  Points 5, 6 and 7 are the ones whose answers
change the schema, and they are the ones a first commit should settle.

See `src/dic/SPEC.md` "Conversation History" for the table this would
extend, and `src/accio/SPEC.md` "The ancestor walk" for the within-run
dedupe it would generalize.
