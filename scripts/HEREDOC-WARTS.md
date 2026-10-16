I commonly use heredocs to build prompts; but heredocs have several problems.
They fall into 2 categories: problems with inputting and problems with storing the prompts.

INPUTTING:

1. Heredocs do not buffer like pipes, so large documents take up lots of memory (not too big a deal), but the dic program they get piped to does not start right away consuming the output.

2. Any program that fails in the heredoc doesn't fail the overall command.  Worse, the dic command will still run and make an API call with bad inputs and we will get charged and corrupt conversation history and it may not be obvious to the user that the input was bad. I would like a failure in the heredoc command substitution to fail the whole command before doing the API call.  Similarly, a failure on variable substitution to fail the whole command.

3. Many useful commands that we want in the llm's context write to stderr (e.g. any error messages that we want help debugging).  Stderr doesn't get substituted into the heredocs without putting 2>1 after the command, which is both archane and combersome.


STORAGE:

1. When reviewing command history (e.g. in dic log), the user is more likely to care about the way they typed the command than the way it literally got passed to the llm. For example, it is common to type something like:
    ```
    $$(accio src/ tests/)

    $$(git ls-files)

    $$(sandbox ./test.sh)
    ```
    and it would be nice to be able to "save" this raw form somehow without the actual results of the commands because the commands have lots of output (potentially thousands of lines) and this output masks the real semantics of the query.  

2. Relatedly, it would be nice to be able to "rerun" the same command and have the $() syntax actually rerun the commands.  This would require storing the raw unprocessed heredoc somewhere. Simonw's llm has similar features via templates and fragments, but we started the dic/geni project specifically wanting to reject these features because they greatly slow down llm.  But there is a real tension here, and so it might make sense to get this type of feature added in.

---

I've brainstormed some possible solutions.  An incomplete list is:
- vim plus appropriate plugins could make inputting more ergonomic
