# dic

<img src=../../img/unix-magic-poster.jpg align=right width=200px />

`dic` is a minimalist CLI tool for working with chat LLMs models.
It is designed to be a "thin wrapper" around API endpoints.

`dic` is similar to simonw's `llm` tool but with an emphasis on speed and Unix-style composability.

> **Etymology:**
>
> "Dic" is Latin for the command "speak".
> The idea is that working with AIs is like working with demons and magic,
> and Latin is the traditional language for controlling demons and casting spells.
>
> `dic` should be pronounced using classical Latin pronunciation.
> It sounds like English "deek" and not "dick" or "dyke".

## Why fork `llm`?

### Speed

Recent versions of `llm` are very slow.
This slowness is due to three factors:
1. Slow dependencies.

    `llm` has many dependencies

    ```
    $ python3 -m venv venv
    $ . venv/bin/activate
    (venv) $ pip3 install llm
    (venv) $ pip3 freeze | wc -l
    28
    ```

    Many of these dependencies are very slow.
    Just importing the `openai` library takes longer than the time-to-first-token of most providers.

    ```
    (venv) $ time python3 -c 'import openai'

    real	0m0.632s
    user	0m0.588s
    sys	0m0.044s
    ```

1. Complicated plugin system.

    1. Using a new provider requires adding a new plugin for that provider.
        This makes it difficult to use the latest models as they're released because we must wait for the plugin to be updated.

    1. Plugins are also slow.
        (And there are many [github issues](https://github.com/simonw/llm/issues/732) about this slowness.)

1. Lack of "unixyness".

    1. `llm` supports many features for constructing complicated system prompts liek [fragments](https://llm.datasette.io/en/stable/fragments.html) and [templates](https://llm.datasette.io/en/stable/templates.html).
        These features could live in separate tools (like `files-to-prompt`) and simplify/speed up the code.

    1. `llm` also supports many different types of models like [embedding models](https://llm.datasette.io/en/stable/embeddings/index.html).
        The "mental model" for working with embedding models and text models is essentially none, and so this feature adds complexity (and hence bugs/slowness) to the code.

### Convenience

The following features make `dic` more comfortable to use on the command line.

1. `dic` is FAST because it uses no external dependencies and directly posts messages to the API endpoints.
    `dic` adds an overhead of about 10ms to the time-to-first-token (TTFT) provider.

1. `dic` tracks time-to-first-token (TTFT) and thinking tokens and outputs them to stderr to measure progress

1. `dic` tracks total spend for each model and outputs it to stderr

1. `dic` is backwards compatible with the `llm` API in most important ways.
    For example:
    - the system prompt (`-s`)
    - attaching binary files (`-a`)
    - continuing a conversation (`-c`)
    - extracting markdown blocks (`-x`)
    - passing options to the model (`-o`)
