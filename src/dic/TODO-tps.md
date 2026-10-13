# TODO-tps

`--trace` is the flag that *records* the byte-rate samples, and
`--tps-csv` / `--tps-svg` name the two readouts that render them.
I don't like the verb "trace" here for byte level records because it is too generic; --trace is often used for adding debug output similar to a --verbose for example.
So --trace should be renamed and `DIC_TRACE` as well.

A second, smaller question: `--tps-csv` and `--tps-svg` say "and a
third format might exist".  Every other readout is a bare noun, and a
`--format csv|svg` on a bare `--tps` is the shape that says the same
thing in one flag.  Decide before adding a third format; today the two
readouts render one shape apiece, so the choice is still open.
