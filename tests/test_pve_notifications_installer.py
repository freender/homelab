"""pve-notifications: remote installer (freender/homelab-ops#30).

`pvesh` is replaced by an in-memory model of `/cluster/notifications` that applies
create / set / delete the way PVE does, so each test asserts on the resulting
config rather than on which commands happened to run.

What carries the risk, and what these pin:

* **A converged config writes nothing.** The bash `set` both objects every deploy;
  an unchanged digest is the canary's evidence, so a spurious write is a failure.
* **`set` has to take away as well as add.** A property this module no longer sets
  -- an emptied severity list, a hand-added `disable` -- must not survive.
* **The new route exists before the old ones go**, and a failed removal or disable
  fails the deploy instead of being swallowed.
* **The bot token leaves no trace**: not on disk after any exit, not in an error.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "pve-notifications" / "scripts" / "install.py"

# The exact body PVE holds on ace and osiris today, as the bash wrote it. The port
# must reproduce it byte for byte or the first deploy rewrites a converged target.
LIVE_ALERTMANAGER_BODY = (
    "W3sibGFiZWxzIjp7ImFsZXJ0bmFtZSI6IlByb3htb3hOb3RpZmljYXRpb24iLCJzZXZlcml0eSI6ImNyaXRpY2Fs"
    "Iiwic291cmNlIjoicHZlIiwiaG9zdCI6Int7IGZpZWxkcy5ob3N0bmFtZSB9fSIsIm5hbWUiOiJ7eyBmaWVsZHMu"
    "dHlwZSB9fSIsInZtaWQiOiJ7eyBmaWVsZHMudm1pZCB9fSIsInB2ZV9zZXZlcml0eSI6Int7IHNldmVyaXR5IH19"
    "In0sImFubm90YXRpb25zIjp7InN1bW1hcnkiOiJ7eyBlc2NhcGUgdGl0bGUgfX0iLCJkZXNjcmlwdGlvbiI6Int7"
    "IGVzY2FwZSBtZXNzYWdlIH19In19XQ=="
)
JSON_HEADER = "name=Content-Type,value=YXBwbGljYXRpb24vanNvbg=="
URL = "http://helm.freender.internal:9093/api/v2/alerts"

ENV = {
    "NOTIFY_TARGET": "alertmanager",
    "ALERTMANAGER_URL": "http://helm.freender.internal:9093",
    "ALERTMANAGER_SEVERITY": "critical",
    "ALERTMANAGER_ALERTNAME": "ProxmoxNotification",
    "TARGET_NAME": "Alertmanager",
    "MATCHER_NAME": "alertmanager-matcher",
    "MATCHER_COMMENT": "Route notifications to Alertmanager",
    "DISABLE_MAIL_TO_ROOT": "true",
    "DISABLE_DEFAULT_MATCHER": "true",
    "MATCH_SEVERITY": "error",
    "REMOVE_MATCHERS": "backup-errors telegram-matcher",
    "REMOVE_WEBHOOK_TARGETS": "telegram Telegram",
}

TELEGRAM_ENV = {
    **ENV,
    "NOTIFY_TARGET": "telegram",
    "TARGET_NAME": "Telegram",
    "MATCHER_NAME": "telegram-matcher",
    "MATCHER_COMMENT": "Route all notifications to Telegram",
    "REMOVE_MATCHERS": "backup-errors alertmanager-matcher",
    "REMOVE_WEBHOOK_TARGETS": "telegram Alertmanager",
}

ROOTS = {
    "/cluster/notifications/endpoints/webhook": "webhook",
    "/cluster/notifications/matchers": "matchers",
    "/cluster/notifications/endpoints/sendmail": "sendmail",
}


def load_installer():
    spec = importlib.util.spec_from_file_location("notifications_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def converged() -> dict[str, dict[str, dict]]:
    return {
        "webhook": {
            "Alertmanager": {
                "name": "Alertmanager",
                "url": URL,
                "method": "post",
                "header": [JSON_HEADER],
                "body": LIVE_ALERTMANAGER_BODY,
            }
        },
        "matchers": {
            "default-matcher": {
                "name": "default-matcher",
                "disable": 1,
                "target": ["mail-to-root"],
            },
            "alertmanager-matcher": {
                "name": "alertmanager-matcher",
                "mode": "all",
                "target": ["Alertmanager"],
                "comment": "Route notifications to Alertmanager",
                "match-severity": ["error"],
            },
        },
        "sendmail": {"mail-to-root": {"name": "mail-to-root", "disable": 1}},
    }


class FakePvesh:
    """`/cluster/notifications` as PVE would hold it, plus a log of every write."""

    LIST_KEYS = {"header", "secret", "target", "match-severity", "match-field", "match-calendar"}

    def __init__(self, state: dict[str, dict[str, dict]]) -> None:
        self.state = state
        self.writes: list[list[str]] = []
        self.fail: dict[tuple[str, str], str] = {}
        self.shredded: list[str] = []

    def __call__(self, argv, **_kwargs):
        if argv[0] == "shred":
            self.shredded.append(argv[-1])
            Path(argv[-1]).unlink()
            return subprocess.CompletedProcess(argv, 0)
        assert argv[0] == "pvesh"
        verb, path, *options = argv[1:]
        if verb != "get":
            self.writes.append(list(argv[1:]))
        if (verb, path) in self.fail:
            return subprocess.CompletedProcess(argv, 255, "", self.fail[(verb, path)])
        return subprocess.CompletedProcess(argv, 0, getattr(self, verb)(path, options), "")

    def split(self, path: str) -> tuple[dict[str, dict], str]:
        root, _, name = path.rpartition("/")
        if path in ROOTS:
            return self.state[ROOTS[path]], ""
        return self.state[ROOTS[root]], name

    @classmethod
    def parse(cls, options: list[str]) -> dict[str, object]:
        values: dict[str, object] = {}
        for flag, value in zip(options[::2], options[1::2], strict=True):
            key = flag.removeprefix("--")
            if key in cls.LIST_KEYS:
                values.setdefault(key, []).append(value)  # type: ignore[union-attr]
            else:
                values[key] = value
        return values

    def get(self, path: str, _options: list[str]) -> str:
        objects, name = self.split(path)
        if name:
            return json.dumps(objects[name])
        return json.dumps(list(objects.values()))

    def create(self, path: str, options: list[str]) -> str:
        values = self.parse(options)
        self.split(path)[0][str(values["name"])] = values
        return ""

    def set(self, path: str, options: list[str]) -> str:
        objects, name = self.split(path)
        values = self.parse(options)
        for key in str(values.pop("delete", "")).split(","):
            objects[name].pop(key, None)
        if "disable" in values:
            values["disable"] = int(str(values["disable"]))
        objects[name].update(values)
        return ""

    def delete(self, path: str, _options: list[str]) -> str:
        objects, name = self.split(path)
        del objects[name]
        return ""


class Host:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state: dict[str, dict[str, dict]] | None = None,
    ) -> None:
        self.installer = load_installer()
        self.pvesh = FakePvesh(converged() if state is None else state)
        self.build = tmp_path / "build" / "ace"
        self.build.mkdir(parents=True)
        monkeypatch.setattr(self.installer, "_run", self.pvesh)
        monkeypatch.setattr(self.installer, "_which", lambda name: f"/usr/bin/{name}")

    def write_secret(self, token: str = "123:abc", chat_id: str = "-1001") -> Path:
        path = self.build / "telegram.env"
        path.write_text(
            f'# Telegram Bot Credentials\nTELEGRAM_TOKEN={token}\nTELEGRAM_CHATID="{chat_id}"\n',
            encoding="utf-8",
        )
        return path

    def deploy(self, env: dict[str, str] | None = None, force: bool = False) -> None:
        ctx = InstallContext(
            host="ace",
            script_dir=self.build.parent.parent,
            build_dir=self.build,
            env=dict(ENV if env is None else env),
            deploy_env={},
            file_map={},
            force_update=force,
        )
        self.installer.install(ctx)

    @property
    def webhook(self) -> dict[str, dict]:
        return self.pvesh.state["webhook"]

    @property
    def matchers(self) -> dict[str, dict]:
        return self.pvesh.state["matchers"]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


def test_a_converged_config_writes_nothing(host: Host) -> None:
    host.deploy()

    assert host.pvesh.writes == []


def test_force_rewrites_both_objects_without_changing_them(host: Host) -> None:
    before = converged()

    host.deploy(force=True)

    assert [write[:2] for write in host.pvesh.writes] == [
        ["set", "/cluster/notifications/endpoints/webhook/Alertmanager"],
        ["set", "/cluster/notifications/matchers/alertmanager-matcher"],
    ]
    assert host.pvesh.state == before


def test_an_empty_host_is_built_from_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch, {"webhook": {}, "matchers": {}, "sendmail": {}})

    host.deploy()

    assert host.pvesh.state["webhook"]["Alertmanager"] == converged()["webhook"]["Alertmanager"]
    assert host.matchers["alertmanager-matcher"] == converged()["matchers"]["alertmanager-matcher"]
    # And a second run finds it converged.
    host.pvesh.writes.clear()
    host.deploy()
    assert host.pvesh.writes == []


def test_the_alertmanager_body_is_the_one_pve_already_holds(host: Host) -> None:
    state = {"webhook": {}, "matchers": {}, "sendmail": {}}
    host.pvesh.state = state

    host.deploy()

    assert state["webhook"]["Alertmanager"]["body"] == LIVE_ALERTMANAGER_BODY


def test_an_alert_name_with_a_quote_still_produces_valid_json(host: Host) -> None:
    host.deploy({**ENV, "ALERTMANAGER_ALERTNAME": 'PVE "event"'})

    body = json.loads(base64.b64decode(str(host.webhook["Alertmanager"]["body"])))
    assert body[0]["labels"]["alertname"] == 'PVE "event"'


def test_a_drifted_url_is_restored(host: Host) -> None:
    host.webhook["Alertmanager"]["url"] = "http://elsewhere:9093/api/v2/alerts"

    host.deploy()

    assert host.webhook["Alertmanager"]["url"] == URL


def test_a_hand_disabled_endpoint_is_re_enabled(host: Host) -> None:
    host.webhook["Alertmanager"]["disable"] = 1

    host.deploy()

    assert "disable" not in host.webhook["Alertmanager"]


def test_stale_telegram_secrets_are_cleared_from_an_alertmanager_target(host: Host) -> None:
    host.webhook["Alertmanager"]["secret"] = ["name=token"]

    host.deploy()

    assert "secret" not in host.webhook["Alertmanager"]
    assert host.webhook["Alertmanager"]["body"] == LIVE_ALERTMANAGER_BODY


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("disable", 1),
        ("invert-match", 1),
        ("match-field", ["exact:type=vzdump"]),
        ("match-calendar", ["mon..fri 8-17"]),
    ],
)
def test_a_hand_added_matcher_restriction_is_removed(host: Host, key: str, value: object) -> None:
    host.matchers["alertmanager-matcher"][key] = value

    host.deploy()

    assert host.matchers["alertmanager-matcher"] == converged()["matchers"]["alertmanager-matcher"]


def test_an_emptied_severity_list_stops_filtering(host: Host) -> None:
    # The bash passed no --match-severity and `set` kept the old one: the matcher
    # went on filtering to `error` while the deploy reported success.
    host.deploy({**ENV, "MATCH_SEVERITY": ""})

    assert "match-severity" not in host.matchers["alertmanager-matcher"]


def test_severities_are_passed_as_a_list(host: Host) -> None:
    host.deploy({**ENV, "MATCH_SEVERITY": "warning error"})

    assert host.matchers["alertmanager-matcher"]["match-severity"] == ["warning", "error"]


def test_match_fields_are_passed_as_a_list(host: Host) -> None:
    # The de-route in homelab-ops#44: `type=replication` is kept off Alertmanager
    # by allowing the other types, since PVE can negate neither a field nor one
    # half of a matcher.
    allowlist = "regex:type=^(package-updates|fencing|vzdump|system-mail)$"

    host.deploy({**ENV, "MATCH_FIELD": f"{allowlist} exact:hostname=ace"})

    assert host.matchers["alertmanager-matcher"]["match-field"] == [
        allowlist,
        "exact:hostname=ace",
    ]


def test_an_emptied_match_field_list_stops_filtering(host: Host) -> None:
    """The declare-or-unset contract `match-severity` already had. Without it,
    removing `match_field` from `hosts.conf` would leave the allowlist in place
    and go on dropping replication events while the deploy reported success --
    the same class of bug the port found for `match-severity`."""
    host.matchers["alertmanager-matcher"]["match-field"] = ["exact:type=vzdump"]

    host.deploy({**ENV, "MATCH_FIELD": ""})

    assert "match-field" not in host.matchers["alertmanager-matcher"]


def test_a_converged_match_field_is_not_rewritten(host: Host) -> None:
    """A rewrite bumps the notification config digest cluster-wide, so an
    unchanged matcher must produce no write at all -- the property that makes the
    canary deploy meaningful."""
    allowlist = "regex:type=^(package-updates|fencing|vzdump|system-mail)$"
    host.matchers["alertmanager-matcher"]["match-field"] = [allowlist]

    host.deploy({**ENV, "MATCH_FIELD": allowlist})

    assert host.pvesh.writes == []


def test_stale_objects_are_removed_only_after_the_new_route_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {
        "webhook": {"Telegram": {"name": "Telegram", "secret": ["name=token"]}},
        "matchers": {"telegram-matcher": {"name": "telegram-matcher", "target": ["Telegram"]}},
        "sendmail": {},
    }
    host = Host(tmp_path, monkeypatch, state)

    host.deploy()

    ops = [(write[0], write[1].rpartition("/")[2]) for write in host.pvesh.writes]
    assert ops == [
        ("create", "webhook"),
        ("create", "matchers"),
        ("delete", "telegram-matcher"),
        ("delete", "Telegram"),
    ]
    assert set(state["webhook"]) == {"Alertmanager"}
    assert set(state["matchers"]) == {"alertmanager-matcher"}


def test_the_active_names_are_never_removed_even_when_listed(host: Host) -> None:
    host.deploy(
        {**ENV, "REMOVE_MATCHERS": "alertmanager-matcher", "REMOVE_WEBHOOK_TARGETS": "Alertmanager"}
    )

    assert host.pvesh.writes == []
    assert "Alertmanager" in host.webhook


def test_a_failed_removal_fails_the_deploy(host: Host) -> None:
    host.matchers["backup-errors"] = {"name": "backup-errors"}
    host.pvesh.fail[("delete", "/cluster/notifications/matchers/backup-errors")] = "locked"

    with pytest.raises(InstallError, match="locked"):
        host.deploy()


def test_builtins_are_disabled_when_enabled(host: Host) -> None:
    host.pvesh.state["sendmail"]["mail-to-root"].pop("disable")
    host.matchers["default-matcher"]["disable"] = 0

    host.deploy()

    assert host.pvesh.state["sendmail"]["mail-to-root"]["disable"] == 1
    assert host.matchers["default-matcher"]["disable"] == 1


def test_a_failed_disable_fails_the_deploy(host: Host) -> None:
    host.pvesh.state["sendmail"]["mail-to-root"].pop("disable")
    host.pvesh.fail[("set", "/cluster/notifications/endpoints/sendmail/mail-to-root")] = "denied"

    with pytest.raises(InstallError, match="denied"):
        host.deploy()


def test_false_leaves_the_builtins_alone(host: Host) -> None:
    host.pvesh.state["sendmail"]["mail-to-root"].pop("disable")

    host.deploy({**ENV, "DISABLE_MAIL_TO_ROOT": "false", "DISABLE_DEFAULT_MATCHER": "false"})

    assert "disable" not in host.pvesh.state["sendmail"]["mail-to-root"]
    assert host.pvesh.writes == []


def test_a_typo_in_a_flag_fails_before_any_write(host: Host) -> None:
    host.webhook["Alertmanager"]["url"] = "http://elsewhere"

    with pytest.raises(InstallError, match="DISABLE_MAIL_TO_ROOT"):
        host.deploy({**ENV, "DISABLE_MAIL_TO_ROOT": "ture"})

    assert host.pvesh.writes == []


@pytest.mark.parametrize("missing", ["TARGET_NAME", "MATCHER_NAME", "ALERTMANAGER_URL"])
def test_a_truncated_env_file_refuses(host: Host, missing: str) -> None:
    env = {key: value for key, value in ENV.items() if key != missing}

    with pytest.raises(InstallError, match=missing):
        host.deploy(env)

    assert host.pvesh.writes == []


def test_an_unknown_target_refuses(host: Host) -> None:
    with pytest.raises(InstallError, match="NOTIFY_TARGET"):
        host.deploy({**ENV, "NOTIFY_TARGET": "gotify"})


def test_a_missing_pvesh_refuses(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.installer, "_which", lambda name: None)

    with pytest.raises(InstallError, match="pvesh command not found"):
        host.deploy()


def test_a_failed_listing_is_not_mistaken_for_absence(host: Host) -> None:
    # The bash read any failed `pvesh get` as "does not exist" and tried to create.
    host.pvesh.fail[("get", "/cluster/notifications/endpoints/webhook")] = "cfs lock timeout"

    with pytest.raises(InstallError, match="cfs lock timeout"):
        host.deploy()

    assert host.pvesh.writes == []


def test_telegram_sets_secrets_every_run_and_shreds_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    secret = host.write_secret(token="123:abc", chat_id="-1001")

    host.deploy(TELEGRAM_ENV)

    telegram = host.webhook["Telegram"]
    assert telegram["url"] == "https://api.telegram.org/bot{{ secrets.token }}/sendMessage"
    assert telegram["secret"] == [
        f"name=token,value={base64.b64encode(b'123:abc').decode()}",
        f"name=chat_id,value={base64.b64encode(b'-1001').decode()}",
    ]
    assert not secret.exists()
    assert host.pvesh.shredded == [str(secret)]
    # The superseded Alertmanager pipeline is gone.
    assert set(host.webhook) == {"Telegram"}
    assert set(host.matchers) == {"default-matcher", "telegram-matcher"}

    # A token rotation cannot be seen through the API, so the set happens again.
    host.write_secret()
    host.pvesh.writes.clear()
    host.deploy(TELEGRAM_ENV)
    assert ["set", "/cluster/notifications/endpoints/webhook/Telegram"] == host.pvesh.writes[0][:2]


def test_the_telegram_body_matches_what_the_bash_sent(host: Host) -> None:
    host.write_secret()

    host.deploy(TELEGRAM_ENV)

    body = base64.b64decode(str(host.webhook["Telegram"]["body"])).decode()
    assert body == (
        '{"chat_id":"{{ secrets.chat_id }}",'
        '"text":"{{ escape title }}\\n\\n{{ escape message }}","parse_mode":"Markdown"}'
    )


def test_the_secret_is_destroyed_even_when_the_deploy_refuses(host: Host) -> None:
    secret = host.write_secret()

    with pytest.raises(InstallError):
        host.deploy({**TELEGRAM_ENV, "DISABLE_MAIL_TO_ROOT": "ture"})

    assert not secret.exists()


@pytest.mark.parametrize(
    "contents", ["TELEGRAM_TOKEN=\nTELEGRAM_CHATID=1\n", "TELEGRAM_CHATID=1\n"]
)
def test_an_incomplete_telegram_secret_refuses(host: Host, contents: str) -> None:
    (host.build / "telegram.env").write_text(contents, encoding="utf-8")

    with pytest.raises(InstallError, match="TELEGRAM_TOKEN or TELEGRAM_CHATID missing"):
        host.deploy(TELEGRAM_ENV)

    assert host.pvesh.writes == []


def test_a_missing_telegram_secret_refuses(host: Host) -> None:
    with pytest.raises(InstallError, match="missing Telegram secret"):
        host.deploy(TELEGRAM_ENV)


def test_a_failed_call_carrying_secrets_does_not_echo_them(host: Host) -> None:
    host.write_secret(token="999:supersecret")
    host.pvesh.fail[("create", "/cluster/notifications/endpoints/webhook")] = (
        "400 Parameter verification failed. secret: name=token,value=OTk5OnN1cGVyc2VjcmV0"
    )

    with pytest.raises(InstallError) as excinfo:
        host.deploy(TELEGRAM_ENV)

    assert "OTk5" not in str(excinfo.value)
    assert "withheld" in str(excinfo.value)
