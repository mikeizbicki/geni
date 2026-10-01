# Geni

<img align=center width="400" src="img/geni.png">

`geni` is a bare-bones command line coding agent.
It is designed to:
1. be easy to understand,
1. be usable on any project with zero setup, and
1. integrate with standard shell workflows.

**About the Name:**

In ancient Rome, a [genius](https://en.wiktionary.org/wiki/genius#Latin) was a protective spirit like the Greek [daemon](https://en.wiktionary.org/wiki/daemon#English).
A Latin speaker would address the genius as "geni" using the [Latin vocative case](https://en.wikipedia.org/wiki/Vocative_case).
The command `geni` is supposed to imply that you are commanding a protective spirit to do your bidding.
It should be pronounced with a hard-G using classical Latin pronunciation rules.

## Setup

Install dic, then let it name the shell file it ships:

```
$ pip3 install git+https://github.com/mikeizbicki/dic
$ echo 'eval "$(dic --init)"' >> ~/.bashrc
```

`dic --init` prints one line, `source /path/to/dic.sh`, and `eval` runs
it in the interactive shell that asked, so the functions and aliases
this directory defines -- `dic`, `committe`, `itera`, `geni`, `sandbox`,
the model aliases -- become available.  Nothing is cloned into `$HOME`.

An edited dic.sh is reread by the shell that asks, because `-f` reaches
it:

```
$ eval "$(dic --init -f)"
```

## Examples

The examples below will show output for a simple empty project created.
You can create one and follow along with the following commands.
```
$ mkdir example_project
$ cd example_project
$ git init
```

The `geni` command is a wrapper around the `llm` command.
It does two things.
First, it sets the system prompt to be appropriate for a coding agent.
You can view the prompt
```
$ geni_prompt
You are a coding agent. You will write files to achieve the tasks that the user specifies in their queries. Your response should always be in pure YAML and conform to the following schema:
...
```
The prompt forces the LLM to output YAML that contains a list of file edits to make and an appropriate commit message for those changes.

```
$ geni 'create a file primes.py that prints the first 100 prime numbers'
request sent... receiving... primes.py(full)...
cost: $0.0000 (input: $0.0000, output: $0.0000) --cid=01kqawy4dwayne80cnpzrdcv35

[geni] Add primes.py to print first 100 primes
 primes.py | 19 +++++++++++++++++++
 1 file changed, 19 insertions(+)
```

All of the command line arguments that get passed to `geni` get forwarded on to the call to `llm`.
This allows us to use the `-c` command to continue our previous conversation
```
$ geni -c 'add doctests'
request sent... receiving... primes.py(full)...
cost: $0.0000 (input: $0.0000, output: $0.0000) --cid=01kqawy4dwayne80cnpzrdcv35

[geni] Add doctests to primes.py
 primes.py | 27 +++++++++++++++++++++++++--
 1 file changed, 25 insertions(+), 2 deletions(-)
```

Running `git log` shows the changes made:
```
$ git log
commit 63a4c6a80c5b643dee532ca6be5f5ebea47fca4b (HEAD -> master)
Author: Mike Izbicki <mike@izbicki.me>
Date:   Tue Apr 28 13:37:28 2026 -0700

  [geni] Add doctests to primes.py

commit 738ef38e1b818528c1d2f392173e94672553c11f
Author: Mike Izbicki <mike@izbicki.me>
Date:   Tue Apr 28 13:36:54 2026 -0700

  [geni] Add primes.py to print first 100 primes
```

Other useful `llm` arguments include:
1. `-a` argument to attach images
1. `-m` command to change models
1. `--cid` to continue previous conversations
1. `-f` to add fragments to the prompt

Standard shell techniques can be used to generate prompts.
The example below uses `files-to-prompt` so that `geni` knows the contents of all files, then can update the files based on the instructions.
```
$ geni <<EOF
$(files-to-prompt .)

add a function to compute the first 30 fibonacci numbers.
EOF
```
