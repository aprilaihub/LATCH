"""WP-014C common-Q envelope sized to the Action Surface v3 context."""

from __future__ import annotations

import hashlib
from typing import Any

from .contract_v5 import QUERY_SESSION_MAX_BYTES
from .observation_v3 import CommonQuerySurfaceV3
from .specs import canonical_json_bytes


QUERY_Q_V4 = "dof.single-submission.query-q.v4"


class CommonQuerySurfaceV4(CommonQuerySurfaceV3):
    session_result_max_bytes = QUERY_SESSION_MAX_BYTES

    @property
    def catalogue(self) -> dict[str, Any]:
        value = super().catalogue
        value.pop("catalogue_sha256")
        value["schema_version"] = QUERY_Q_V4
        return value | {"catalogue_sha256": hashlib.sha256(canonical_json_bytes(value)).hexdigest()}


__all__ = ["CommonQuerySurfaceV4", "QUERY_Q_V4"]
