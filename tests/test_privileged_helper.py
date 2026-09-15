import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_helper() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-provision-stack").read_text()
    source = source.replace(
        '"__GIMME_STATE_PATH__"', '"/srv/gimme/apps/.gimme/stack.json"'
    )
    source = source.replace(
        '"__GIMME_ALLOWED_PACKAGES_JSON__"',
        json.dumps(json.dumps(["caddy", "python3"])),
    )
    source = source.replace('"__GIMME_HOSTNAME__"', '"192.0.2.10"')
    source = source.replace('"__GIMME_MDNS_NAME__"', '"devbox"')
    namespace: dict[str, object] = {"__name__": "gimme_helper_test"}
    exec(compile(source, "gimme-provision-stack", "exec"), namespace)
    return namespace


def test_helper_template_has_fixed_privilege_policy() -> None:
    helper = load_helper()

    assert helper["EXPECTED_APPS_ROOT"] == Path("/srv/gimme/apps")
    assert helper["EXPECTED_HOSTNAME"] == "192.0.2.10"
    assert helper["EXPECTED_MDNS_NAME"] == "devbox"
    assert helper["ALLOWED_PACKAGES"] == {"caddy", "python3"}


def test_php_site_resolves_deployer_current_symlink() -> None:
    render_caddy_site = load_helper()["render_caddy_site"]

    site = render_caddy_site(
        "example-app",
        "devbox",
        Path("/srv/gimme/apps/example-app/current/public"),
        "laravel",
    )

    assert (
        "php_fastcgi unix//run/php/php-fpm.sock {\n"
        "        resolve_root_symlink\n"
        "    }"
    ) in site


def test_state_file_must_not_be_accessible_to_other_users(tmp_path: Path) -> None:
    helper = load_helper()
    state_path = tmp_path / "stack.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "package_manager": "apt",
                "remote_user": "deployer",
                "apps_root": "/srv/gimme/apps",
            }
        )
    )
    state_path.chmod(0o644)
    helper["STATE_PATH"] = state_path

    with pytest.raises(RuntimeError, match="must not be group or world accessible"):
        helper["load_state"]("deployer", os.getuid())


def test_mdns_service_has_a_restricted_systemd_sandbox() -> None:
    unit = load_helper()["render_mdns_unit"](
        "example-app", "example-app.devbox.local", "192.0.2.10"
    )

    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "CapabilityBoundingSet=" in unit


def test_php_renders_python_safe_helper_state_path() -> None:
    recipe = (ROOT / "deploy.php").read_text()
    state_replacement = recipe.split('"__GIMME_STATE_PATH__"', 1)[1].split(
        "$helperTemplate", 1
    )[0]
    assert "JSON_UNESCAPED_SLASHES" in state_replacement

    result = subprocess.run(
        [
            "php",
            "-r",
            "echo json_encode($argv[1], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);",
            "/srv/gimme/apps/.gimme/stack.json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    source = (
        (ROOT / "scripts" / "gimme-provision-stack")
        .read_text()
        .replace('"__GIMME_STATE_PATH__"', result.stdout)
    )
    source = source.replace(
        '"__GIMME_ALLOWED_PACKAGES_JSON__"', json.dumps(json.dumps(["python3"]))
    )
    source = source.replace('"__GIMME_HOSTNAME__"', '"192.0.2.10"')
    source = source.replace('"__GIMME_MDNS_NAME__"', '"devbox"')
    namespace: dict[str, object] = {"__name__": "gimme_helper_render_test"}
    exec(compile(source, "gimme-provision-stack", "exec"), namespace)

    assert namespace["STATE_PATH"] == Path("/srv/gimme/apps/.gimme/stack.json")


def test_helper_rejects_unsafe_static_document_roots() -> None:
    helper = load_helper()
    validate_apps = helper["validate_apps"]

    with pytest.raises(RuntimeError, match="unsafe frontend output directory"):
        validate_apps(
            {
                "dashboard": {
                    "framework": "static",
                    "frontend": {"output_dir": "../../etc"},
                }
            }
        )
