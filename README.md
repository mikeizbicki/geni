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

## Security Model

The latin names are intended to remind us about the potentially dangerous nature of working with LLMs.
The threat model is that the LLM responses are maximally malicious (a nation state level actor, or worse: a literal devil) and so they must never be trusted.
We must be secure against everything up to kernel level exploits.

This results in a more secure environment that Codex or Claude Code.
Both of these frameworks, for example, allow their agents full read access to the file system,
which allows exfiltration of sensitive data.
Our sandbox allows only read access to the local git repo by default.

## Included tools

Tools are divided into two categories:

1. Tools that directly invoke the LLM are written in latin (typically second person imperative singular).

    1. `dic` (latin for speak) - a thin CLI wrapper around LLM apis
        1. similar in spirit to simonw's `llm` package, but: faster, more composable, and more unixy; see <./src/dic/README.md>
    1. `committe` (latin for commit) - create a git commit
    1. `itera` (latin for iterate) - run committe and a test script in a ralph loop
    1. `geni` - the main interface for the coding agent
    1. `accio` (latin for fetch) - a tool for building rag-like prompts

1. English names are for deterministic machinery.
    1. `git-apply-fuzzy` - like `git apply` but uses fuzzy matching for llm generated patches
    1. `sandbox` - a thin wrapper around `bwrap` with strong defaults for a coding agent

## Install

The project is divided into a suite of python and shell scripts.
```
$ pip3 install git+https://github.com/mikeizbicki/geni
$ eval "$(dic --init)"
```
It is recommended to put the eval line in your .bashrc.
