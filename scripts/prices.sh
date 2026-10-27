#!/bin/bash
# Fetch each provider's pricing page and extract its prices with dic.
#
# Reads the provider URLs out of models.json itself: an abstract entry
# whose docs.url is set is one this script fetches.  Writes one JSON per
# provider under prices/, in dic's own price-table shape, and merges
# nothing -- the output is there to be read.
#
#     scripts/prices.sh

set -euo pipefail

models=${DIC_MODELS:-${XDG_CONFIG_HOME:-$HOME/.config}/geni/models.json}
schema=$(dirname "$0")/pricing.schema.json

mkdir -p prices

jq -r 'to_entries[]
       | select(.value.abstract == true)
       | select(.value.docs.url != null)
       | [.key, .value.docs.url] | @tsv' "$models" |
while IFS=$'\t' read -r provider url; do
    printf '### %s  %s\n' "$provider" "$url" >&2
    dic --schema "$schema" -q --path "prices/$provider.json" -f <<EOF
Return every published price for every model on this page, in dic's own
price-table shape.  'in' and 'out' are USD per million tokens; a
refinement like 'in.cache_read', 'in.cache_write.5m' or 'out.reasoning'
is a rule of its own, and one that depends on the request -- a long
context threshold, a service tier -- carries 'when' or 'tier'.  A rate
that is not per token carries 'unit'.  Give each rate's exact text as
quote_in or quote_out.

$(lynx -dump -nolist "$url")
EOF
done
