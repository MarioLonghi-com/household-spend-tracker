<!-- One or two sentences: what changed, and why the obvious thing was wrong.
     A sentence, not a label. Name the issue it fixes ("Fixes #N"). -->

## Checklist

- [ ] `make lint` passes
- [ ] `make test` passes, and the new test asserts a changed value rather than a 200
- [ ] `make e2e`, if this touches the UI
- [ ] A new migration declares `Reversible: clean|lossy -- <what>` in its docstring
- [ ] A line under `## Unreleased` in `CHANGELOG.md`
- [ ] No real data: no real names, IBANs, card or account numbers, amounts or statements in fixtures, tests, docs or this description
- [ ] No tool-generated trailers (`Co-Authored-By:` for an assistant, "Generated with …") in the commits or here
