from __future__ import annotations

import pytest

from homelab.backup_excludes import matching_entry

ENTRIES = ["/plex/transcode", "/*/logs", "/crowdsec/data/GeoLite2-City.mmdb"]


@pytest.mark.parametrize(
    ("path", "entry"),
    [
        ("/plex/transcode", "/plex/transcode"),
        ("/plex/transcode/", "/plex/transcode"),
        ("/plex/transcode/session/1", "/plex/transcode"),
        ("/traefik/logs", "/*/logs"),
        ("/traefik/logs/access.log", "/*/logs"),
        ("/crowdsec/data/GeoLite2-City.mmdb", "/crowdsec/data/GeoLite2-City.mmdb"),
    ],
)
def test_path_equal_to_or_inside_an_entry_matches(path: str, entry: str) -> None:
    assert matching_entry(path, ENTRIES) == entry


@pytest.mark.parametrize(
    "path",
    [
        "/plex",  # an entry inside the path is not a match
        "/",
        "/crowdsec/data",
        "/plex/transcoder",
        "/logs",  # `*` needs a segment of its own
        "/a/b/logs",  # and covers exactly one
        "/traefik/log",
    ],
)
def test_path_outside_every_entry_does_not_match(path: str) -> None:
    assert matching_entry(path, ENTRIES) is None


def test_first_matching_entry_wins() -> None:
    assert matching_entry("/plex/logs", ["/plex", "/*/logs"]) == "/plex"
    assert matching_entry("/plex/logs", ["/*/logs", "/plex"]) == "/*/logs"


def test_no_entries_matches_nothing() -> None:
    assert matching_entry("/plex/transcode", []) is None
