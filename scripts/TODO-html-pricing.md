# TODO: parse the HTML pricing pages

`pricing_url` names the page a provider publishes its own prices and model
descriptions on, and it is HTML.  scripts/prices.sh reads the OpenAI-shaped
ones today; fal and OpenRouter publish theirs only there, so a synced model
comes back with no price at all (fal) or with a chat-shaped price that does
not describe its images and video (OpenRouter), and with no prose to put in
`dic -m NAME --help`.

What it would buy:

1. a description for `--help` for the providers whose API publishes none --
   OpenAI's and Groq's `/models` returns `{id, object, created, owned_by}`
   and nothing more, while their price pages name each model and say what it
   is;
2. a price for the endpoints the catalog API cannot rate: fal's
   `/models/pricing` returns one flat `unit_price` per endpoint with no
   resolution axis, so a 480p and a 720p video are one number, and the
   per-second arithmetic fal's own page states -- resolution, duration, the
   multi-input discount -- exists nowhere in the API.

The pieces, in the order they are worth doing:

- a `pricing_url` per abstract provider: groq, cerebras, anthropic and
  openai name one already and fal and openrouter are named in models.json;
- a parser per page shape, where scripts/prices.sh already is: a page is a
  fetcher, so a new shape is a new module rather than a new program;
- the result written where dic reads it, so that `--help` and the `price`
  table show a number the page agrees with rather than one dic guessed.

An HTML page is not an API: it is unversioned, it changes without notice,
and a parse that silently returns nothing must not be written as a price of
zero.  A page that no longer parses is a failure to report and not an empty
table.
