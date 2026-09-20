"""Zero-cost deterministic operator scenario for signed sticky Rollout cohorts."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Artifact:
    build_id: str
    health: str = "ready"


class CohortRouter:
    def __init__(self, stable: Artifact, candidate: Artifact, generation: int = 1):
        self.artifacts = {"stable": stable, "candidate": candidate}
        self.weights = {"stable": 100, "candidate": 0}
        self.generation = generation
        self.key = hashlib.sha256(f"rollout-key-{generation}".encode()).digest()
        self.direct_probes: list[str] = []
        self.public_probes = 0

    def signature(self, release: str) -> str:
        identity = self.artifacts[release].build_id.encode()
        return hmac.new(self.key, identity, hashlib.sha256).hexdigest()

    def cookie(self, release: str) -> str:
        return f"{release}.{self.signature(release)}"

    def valid_cookie(self, cookie: str | None) -> str | None:
        if cookie is None or "." not in cookie:
            return None
        release, signature = cookie.split(".", 1)
        if release not in self.artifacts or not hmac.compare_digest(
            signature, self.signature(release)
        ):
            return None
        if self.weights[release] == 0 or self.artifacts[release].health != "ready":
            return None
        return release

    @staticmethod
    def safe_retry(method: str, connection_failed: bool) -> bool:
        return connection_failed and method in {"GET", "HEAD"}

    def select(self, client: str, cookie: str | None = None) -> tuple[str, str]:
        sticky = self.valid_cookie(cookie)
        if sticky is not None:
            return sticky, cookie or ""
        bucket = int.from_bytes(hashlib.sha256(client.encode()).digest()[:8], "big") % 100
        release = "stable" if bucket < self.weights["stable"] else "candidate"
        if self.weights[release] == 0 or self.artifacts[release].health != "ready":
            release = "candidate" if release == "stable" else "stable"
        return release, self.cookie(release)

    def transition(self, stable: int, candidate: int) -> None:
        if stable + candidate != 100 or min(stable, candidate) < 0:
            raise ValueError("invalid weights")
        for release in ("stable", "candidate"):
            self.direct_probes.append(release)
            if self.artifacts[release].health != "ready":
                raise RuntimeError(f"{release}_health_failed")
        self.weights = {"stable": stable, "candidate": candidate}
        self.public_probes += 1

    def rotate(self) -> None:
        self.generation += 1
        self.key = hashlib.sha256(f"rollout-key-{self.generation}".encode()).digest()


def run_scenario() -> dict[str, object]:
    stable = Artifact("build_v1_" + "1" * 64)
    candidate = Artifact("build_v1_" + "2" * 64)
    router = CohortRouter(stable, candidate, generation=17)
    stages: list[dict[str, object]] = []
    sticky_cookie = None
    sticky_release = None

    for stable_weight, candidate_weight in ((90, 10), (50, 50), (0, 100)):
        router.transition(stable_weight, candidate_weight)
        assignments = [router.select(f"client-{index}")[0] for index in range(200)]
        if stable_weight and candidate_weight:
            assert set(assignments) == {"stable", "candidate"}
        if sticky_cookie is None:
            sticky_release, sticky_cookie = router.select("sticky-client")
        elif router.weights[sticky_release] > 0:
            assert router.select("changed-client", sticky_cookie)[0] == sticky_release
        stages.append({
            "weights": [stable_weight, candidate_weight],
            "stable": assignments.count("stable"),
            "candidate": assignments.count("candidate"),
        })

    assert all(
        router.select(f"zero-{index}", sticky_cookie)[0] == "candidate"
        for index in range(20)
    )
    assert router.valid_cookie("candidate." + "0" * 64) is None
    old_cookie = router.cookie("candidate")
    router.rotate()
    assert router.valid_cookie(old_cookie) is None
    assert router.safe_retry("GET", True)
    assert router.safe_retry("HEAD", True)
    assert not router.safe_retry("POST", True)
    assert not router.safe_retry("GET", False)

    report = {
        "artifacts": [stable.build_id, candidate.build_id],
        "stages": stages,
        "direct_health": router.direct_probes,
        "public_health_checks": router.public_probes,
        "completion": {"sole_web": "candidate", "background_owner": "candidate"},
        "reversal": {"sole_web": "stable", "background_owner": "stable"},
        "affinity_rotated": True,
    }
    encoded = json.dumps(report, sort_keys=True)
    assert "cookie" not in encoded
    assert "client-" not in encoded
    assert "rollout-key" not in encoded
    return report


if __name__ == "__main__":
    print(json.dumps(run_scenario(), sort_keys=True, separators=(",", ":")))
