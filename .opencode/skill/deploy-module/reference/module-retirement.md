# Retiring a module

Read this when removing a module from the framework — archiving it, deleting it, or
clearing `./validate`'s orphan-module warning.

## Confirm it deploys nowhere first

No `<module>:` feature blocks may remain in `hosts.conf`. `./validate`'s orphan-module
check must come back clean afterward — it warns while a registered module has zero hosts
enabling it, which is the signal that registration and inventory have diverged.

## Archive or delete

Prefer archiving over deleting when there is meaningful implementation history; trivial or
obsolete modules can be `git rm`'d outright. Precedent exists both ways in git log:
`1639d7a` archives, `f5dbb5f`/`0e492c2` delete.

## Archive layout

`git mv` into `archive/retired-modules/` so history is preserved:

- orchestrator -> `archive/retired-modules/src/homelab/modules/`
- the module's top-level dir (`scripts/`, `templates/`, `configs/`, `README.md`, but not
  the gitignored `build/`) -> `archive/retired-modules/top-level/<module-dir>/`
- dedicated tests -> `archive/retired-modules/tests/` or `.../reference/`, **renamed out
  of the `test_*.py` pattern** so pytest stops collecting them

## Deregister

Remove from `src/homelab/modules/__init__.py` (the import, `MODULES`, and `MODULE_ORDER`),
the README module list, and any `hosts.py` schema validation.

## Verify

Run `./validate`: the orphan warning clears and nothing else references the module.
