from __future__ import annotations

from typing import NoReturn

from gimme.control import AWSElastiCacheValkeyResource
from gimme.resources_postgres import ResourceError


def validate_update(
    current: AWSElastiCacheValkeyResource, proposed: AWSElastiCacheValkeyResource
) -> None:
    """Refuse the updates ADR 0009 says need a new Resource. Local: never calls AWS.
    Same-major engine, node type, windows, and retention are allowed and applied later."""
    def forbid(field: str) -> NoReturn:
        raise ResourceError(f"aws_elasticache_update_forbidden_{field}")

    if proposed.aws_network != current.aws_network:
        forbid("aws_network")
    if proposed.engine_version.split(".")[0] != current.engine_version.split(".")[0]:
        forbid("engine_major")
    if proposed.security_group_id != current.security_group_id:
        forbid("security_group_id")
