# operon-plugin -- project notes

## Syncing develop into main

`main` takes everything from `develop` except the `operon-runs/` work
products (tracked on `develop`, ignored on `main`). A plain merge
fast-forwards and drags `operon-runs/` across, so use a no-ff merge that
drops that tree:

```bash
git checkout main
git merge --no-ff --no-commit develop
git rm -rf operon-runs/
git commit --no-verify -m "Merge develop into main (exclude operon-runs/)"
git push origin main
git checkout develop
```

`--no-verify` is required: the `block-operon-runs-on-main` pre-commit hook
rejects any commit touching `operon-runs/`, including this deletion.

Full branch model: `docs/dev/contributing.md` -> "The two-branch model"
and "Recipe: sync develop into main".
