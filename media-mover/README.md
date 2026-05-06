# media-mover

Active media cache mover for `tower`.

This module owns media sync, cache promotion, and cache eviction while `tower` uses the current SnapRAID/MergerFS media layout.

## Status

**Status:** Active

**Future note:** Direct Plex signal collection is coupled into the runtime script. If that coupling becomes a problem, split Plex signal collection into a separate Docker container and keep `media-mover` as the storage writer.

## Current Usage

```bash
./deploy media-mover tower
./deploy --dry-run media-mover tower
```

## Migration Notes

- Keep `media-mover` as the only process that writes, promotes, or evicts media until a replacement is tested.
- Do not move files through `/mnt/user/media`; use the raw cache and HDD-only paths.
- If Plex signal collection is split out, have the container produce a state/intent file and let this mover consume it behind a feature flag.
- SnapRAID currently orchestrates mover runs before sync/scrub on `tower`.
