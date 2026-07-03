# media-mover

Retired media cache mover for `tower`.

This archived module used to own media sync, cache promotion, and cache eviction for an older SnapRAID/MergerFS media layout.

## Status

**Status:** Retired

**Future note:** Direct Plex signal collection is coupled into the runtime script. If that coupling becomes a problem, split Plex signal collection into a separate Docker container and keep `media-mover` as the storage writer.

## Historical Usage

```bash
./deploy media-mover tower
./deploy --dry-run media-mover tower
```

## Historical Notes

- This module is retained only as migration history.
- Do not treat the SnapRAID orchestration notes here as current runtime documentation.
