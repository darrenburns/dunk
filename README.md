# dunk

Pipe your `git diff` output into `dunk` to make it prettier!

![image](https://user-images.githubusercontent.com/5740731/162084469-718a8b48-a176-4657-961a-f45e157ff562.png)

> ⚠️ This project is **very** early stages - expect crashes, bugs, and confusing output!

## Quick Start

I recommend you install using `pipx`, which will allow you to use `dunk` from anywhere.

```
pipx install dunk
```

## Basic Usage

Pipe the output of `git diff` into `dunk`:

```
git diff | dunk
```

or add it to git as an alias:
```
git config --global alias.dunk '!a() { git diff $@ | dunk; } ; a'
git dunk HEAD~1
```

### Paging

You can pipe output from `dunk` into a pager such as `less`:

```
git diff | dunk | less -R
```
