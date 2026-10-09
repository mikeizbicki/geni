# TODO-tps

`--trace` names the flag that *records* the byte-rate samples, and
`--tps-csv` / `--tps-svg` name the two readouts that draw them.
"Trace" reads as the flag that draws the plot, which it is not; it
should be renamed, so that one verb records and another reports.
`DIC_TRACE` goes with it.

A second, smaller question: `--tps-csv` and `--tps-svg` say "and a
third format might exist".  Every other readout is a bare noun, and a
`--format csv|svg` on a bare `--tps` is the shape that says the same
thing in one flag.  Decide before adding a third format.
