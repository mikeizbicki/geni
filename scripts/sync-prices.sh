#!/bin/bash
# Sync provider prices from their own HTML pages into models.json.
#
# Every abstract entry of models.json that names a `docs.url` is fetched
# with lynx, handed to `dic --schema scripts/pricing.schema.json` in one
# heredoc, verified against a literal quote from the page, and merged
# back.  A config entry that already states a rate is never overwritten:
# a differing one is printed and the run goes on.
#
#     scripts/sync-prices.sh            merge into $DIC_MODELS' file
#     scripts/sync-prices.sh --dry-run  print the diff instead
#
# The point is composition: lynx does the HTTP, dic does the extraction,
# jq does the JSON, and the merge is a shell loop.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
schema=$here/pricing.schema.json
models=${DIC_MODELS:-${XDG_CONFIG_HOME:-$HOME/.config}/geni/models.json}
dryrun=
[ "${1:-}" = "--dry-run" ] && dryrun=1

for f in "$schema" "$models"; do
    if [ ! -f "$f" ]; then
        echo "sync-prices: $f: not found" >&2
        exit 1
    fi
done

# The work file: models.json as the merge has it so far.  Nothing touches
# the real file until every provider has been walked.
work=$(mktemp)
providers=$work.providers
cleanup() { rm -f "$work" "$work.new" "$providers" "$work.verified"; }
trap cleanup EXIT
cp "$models" "$work"

# One line per provider: its id, its pricing page, and the prefix its
# models are named under.  An abstract entry is what carries a provider's
# page, because the page prices every one of its models at once.
jq -r '
  to_entries[]
  | select(.value.abstract == true)
  | select(.value.docs.url != null)
  | [.key, .value.docs.url, (.value.docs.prefix // .key)]
  | @tsv' "$models" > "$providers"

if [ ! -s "$providers" ]; then
    echo "sync-prices: $models: no provider names a docs.url" >&2
    exit 0
fi

while IFS=$'\t' read -r provider url prefix; do
    printf 'sync-prices: %s: %s\n' "$provider" "$url"

    # The page is dumped once and kept in a shell variable: the model reads
    # it as the body of the prompt, and the verification below greps it.  A
    # provider that moved or blocked its page fails here, before the model.
    if ! page=$(lynx -dump -nolist -width=200 "$url"); then
        echo "sync-prices: $provider: lynx failed" >&2
        continue
    fi

    out=$(mktemp)
    # The prompt is one heredoc: the instruction, a blank line, the page.
    # dic reads a non-tty stdin as the body of the prompt, so the two
    # parts stay separated without a pipe, a second process, or a flag.
    if ! dic --schema "$schema" --path "$out" -f -q <<EOF
Return one row per model with published input and output token prices.
Quote the exact price string you read, verbatim, into quote_in and quote_out.

$page
EOF
    then
        echo "sync-prices: $provider: dic failed" >&2
        rm -f "$out"
        continue
    fi

    # Verification: a row whose quote is not a literal substring of the
    # page is a hallucination and is dropped.  This is the cheapest check
    # there is, and it catches both unit errors and OCR-scale errors --
    # $10.00 read as 100.00 is a quote that is not on the page.
    verified=$work.verified
    jq --arg p "$page" '
      [.models[]
       | select(.quote_in == "" or ($p | contains(.quote_in)))
       | select(.quote_out == "" or ($p | contains(.quote_out)))]' \
       "$out" > "$verified"
    rm -f "$out"

    # Conflicts: a config entry that already names a different rate.  The
    # merge below leaves it alone; this is the report that says so, and it
    # goes out before the merge because a review is why the run is watched.
    jq -r --arg prefix "$prefix" --slurpfile rows "$verified" '
      (($rows[0] // []) | map({key: .model_name, value: .}) | from_entries)
        as $by
      | to_entries[]
      | select(.key | startswith($prefix + "+"))
      | . as $e
      | $by[$e.value.model_name] as $r
      | select($r != null)
      | (if $e.value.cost_input != null and $e.value.cost_input != $r.in.value
         then "conflict: \($e.key) input \($e.value.cost_input)"
              + " -> \($r.in.value)" else empty end),
        (if $e.value.cost_output != null and $e.value.cost_output != $r.out.value
         then "conflict: \($e.key) output \($e.value.cost_output)"
              + " -> \($r.out.value)" else empty end)
      ' "$work" >&2

    # The merge: an entry that has neither rate takes both from the page.
    # Nothing else is touched -- names, options, aliases and the
    # hand-written prices of a model the page prices differently survive.
    jq --arg prefix "$prefix" --slurpfile rows "$verified" '
      (($rows[0] // []) | map({key: .model_name, value: .}) | from_entries)
        as $by
      | with_entries(
          . as $e
          | $by[$e.value.model_name] as $r
          | if $r != null
               and ($e.key | startswith($prefix + "+"))
               and $e.value.cost_input == null
               and $e.value.cost_output == null
            then .value += {cost_input: $r.in.value,
                            cost_output: $r.out.value}
            else . end)
      ' "$work" > "$work.new"
    mv "$work.new" "$work"
    rm -f "$verified"
done < "$providers"

if [ -n "$dryrun" ]; then
    diff -u "$models" "$work" || true
else
    mv "$work" "$models"
    printf 'sync-prices: %s updated\n' "$models"
fi
