from __future__ import annotations

import hashlib
import hmac
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def load_helper():
    path = ROOT / "scripts" / "gimme-provision-rollout"
    loader = SourceFileLoader("gimme_provision_rollout_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def policy(stable: int = 90, candidate: int = 10) -> dict[str, object]:
    return {
        "instance": "example-local",
        "generation": 17,
        "phase": "active",
        "affinity_generation": 17,
        "stable_weight": stable,
        "candidate_weight": candidate,
        "route_fingerprint": "rollout_" + "1" * 64,
        "framework": "laravel",
        "site_host": "example.test",
        "network_mode": "local_mdns",
        "deploy_path": "/srv/gimme/example-local",
        "php_version": "8.4",
        "health": [{
            "path": "/health",
            "expected_status": 200,
            "timeout_seconds": 5,
            "attempts": 3,
            "delay_seconds": 1,
        }],
        "stable_identity": "rollout_" + "2" * 64,
        "candidate_identity": "rollout_" + "3" * 64,
    }


def test_signed_cookie_route_is_derived_sticky_and_zero_weight_excludes_backend() -> None:
    helper = load_helper()
    key = "a" * 64
    route = helper.public_site(policy(), 21001, 21002, key)

    assert "reverse_proxy 127.0.0.1:21001 127.0.0.1:21002" in route
    assert "lb_policy cookie __Host-gimme-" in route
    assert key in route
    assert "fallback weighted_round_robin 90 10" in route
    assert "Path=/; Secure; HttpOnly); SameSite=None" in route
    assert "$1; SameSite=Lax" in route
    assert "method GET HEAD" in route
    assert "health_uri /health" in route

    stable_only = helper.public_site(policy(100, 0), 21001, 21002, key)
    assert "reverse_proxy 127.0.0.1:21001 {" in stable_only
    assert "127.0.0.1:21002" not in stable_only
    candidate_only = helper.public_site(policy(0, 100), 21001, 21002, key)
    assert "reverse_proxy 127.0.0.1:21002 {" in candidate_only
    assert "127.0.0.1:21001" not in candidate_only


def test_affinity_signatures_are_deterministic_and_reject_forgery_or_rotation() -> None:
    upstream = b"127.0.0.1:21002"
    first_key = bytes.fromhex("11" * 32)
    rotated_key = bytes.fromhex("22" * 32)
    signature = hmac.new(first_key, upstream, hashlib.sha256).hexdigest()

    assert signature == hmac.new(first_key, upstream, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(signature, "0" * 64)
    assert signature != hmac.new(rotated_key, upstream, hashlib.sha256).hexdigest()


def test_signing_key_is_root_local_reused_per_generation_and_rotated(
    tmp_path: Path, monkeypatch
) -> None:
    helper = load_helper()
    monkeypatch.setattr(helper, "ROOT_STATE", tmp_path)
    keys = iter(["1" * 64, "2" * 64])
    monkeypatch.setattr(helper.secrets, "token_hex", lambda _size: next(keys))

    first = helper.signing_key("example-local", 17)
    assert helper.signing_key("example-local", 17) == first
    second = helper.signing_key("example-local", 18)

    assert second != first
    assert (tmp_path / "example-local.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "example-local.generation").stat().st_mode & 0o777 == 0o600


def test_root_state_rejects_unbounded_instance_path() -> None:
    helper = load_helper()

    with pytest.raises(RuntimeError, match="invalid rollout identity"):
        helper.root_state("../../private")


def transition_harness(tmp_path: Path, monkeypatch, *, fail_runs: set[int]):
    helper = load_helper()
    public_root = tmp_path / "gimme"
    internal_root = tmp_path / "gimme-rollouts"
    public_root.mkdir()
    internal_root.mkdir()
    public = public_root / "example-local.caddy"
    prior = "example.test { reverse_proxy 127.0.0.1:9000 }\n"
    public.write_text(prior)
    real_path = Path

    def mapped_path(value):
        text = str(value)
        if text == "/etc/caddy/gimme-rollouts":
            return internal_root
        if text == "/etc/caddy/gimme":
            return public_root
        return real_path(value)

    calls = 0

    def run(_argv):
        nonlocal calls
        calls += 1
        if calls in fail_runs:
            raise RuntimeError("fixed failure")
        return type("Result", (), {"stdout": "200"})()

    monkeypatch.setattr(helper, "Path", mapped_path)
    monkeypatch.setattr(helper, "ROOT_STATE", tmp_path / "root-state")
    monkeypatch.setattr(helper, "load", lambda *_args: policy())
    monkeypatch.setattr(helper, "internal_sites", lambda _value: ("internal\n", 21001, 21002))
    monkeypatch.setattr(helper, "signing_key", lambda *_args: "a" * 64)
    monkeypatch.setattr(helper, "probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "emit_state", lambda _value: None)
    monkeypatch.setattr(helper, "run", run)
    monkeypatch.setattr(helper.os, "chown", lambda *_args: None)
    monkeypatch.setattr(
        helper.pwd, "getpwnam", lambda _name: type("Account", (), {"pw_gid": 33})()
    )
    return helper, public, prior


def test_route_reload_failure_restores_exact_prior_route(tmp_path: Path, monkeypatch) -> None:
    helper, public, prior = transition_harness(tmp_path, monkeypatch, fail_runs={3})

    with pytest.raises(RuntimeError, match="rollout route transition failed"):
        helper.main_weights("example-local")

    assert public.read_text() == prior


def test_successful_route_is_not_world_readable(tmp_path: Path, monkeypatch) -> None:
    helper, public, _ = transition_harness(tmp_path, monkeypatch, fail_runs=set())

    helper.main_weights("example-local")

    assert public.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("failure_probe", [1, 2])
def test_direct_backend_failure_leaves_public_route_unchanged(
    tmp_path: Path, monkeypatch, failure_probe: int
) -> None:
    helper, public, prior = transition_harness(tmp_path, monkeypatch, fail_runs=set())
    probes = 0

    def fail_selected_probe(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        if probes == failure_probe:
            raise RuntimeError("rollout health failed")

    monkeypatch.setattr(helper, "probe", fail_selected_probe)

    with pytest.raises(RuntimeError, match="rollout health failed"):
        helper.main_weights("example-local")

    assert public.read_text() == prior


def test_route_rollback_failure_is_fixed_and_redacted(tmp_path: Path, monkeypatch) -> None:
    helper, public, prior = transition_harness(tmp_path, monkeypatch, fail_runs={3, 4})

    with pytest.raises(RuntimeError, match="^rollout route rollback failed$"):
        helper.main_weights("example-local")

    assert public.read_text() == prior
