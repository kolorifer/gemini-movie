"""Durable inputs for resuming a verified release at the Radarr library lookup."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .clients import RadarrStatusResponse, TriageEvalResult
from .security import redact_secrets
from .tmdb_verifier import IdentityGateResult, TmdbMovieRecord
from .torrentleech import TorrentLeechResult

MAX_RADARR_ATTEMPTS = 3
_TIMEOUT_NAMES = frozenset({"TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout",
                            "PoolTimeout", "TimeoutError"})


def is_timeout_status(status: Any, detail: Any) -> bool:
    if status != "lookup_failed" or not isinstance(detail, str):
        return False
    return (detail.removeprefix("radarr_read_failed:") in _TIMEOUT_NAMES
            or detail in {"actions_http_408", "actions_http_504", "radarr_http_408", "radarr_http_504"})


def is_lookup_timeout(radarr: RadarrStatusResponse) -> bool:
    return is_timeout_status(radarr.status, radarr.detail)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value, max_chars=None)
    if isinstance(value, (tuple, list)):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        # Apply the same credential-field rules as the intake text sanitizer to JSON keys.
        return {key: _scrub(item) for key, item in value.items()
                if key.lower() not in {"download_url", "headers"}
                and redact_secrets(f"{key}=probe", max_chars=None) == f"{key}=probe"}
    return value


def identification_checkpoint(
    tl: TorrentLeechResult, pre_gate: IdentityGateResult, gate: IdentityGateResult,
    evaluation: TriageEvalResult | None,
) -> dict[str, Any]:
    data = {"listing": asdict(tl), "pre_gate": asdict(pre_gate)}
    if evaluation is not None:
        data["evaluated_gate"] = asdict(gate)
        data["evaluation"] = asdict(evaluation)
    return _scrub(data)


def restore_gate(data: dict[str, Any]) -> IdentityGateResult:
    fields = dict(data)
    movie = fields["verified_movie"]
    if movie is not None:
        fields["verified_movie"] = TmdbMovieRecord(**{**movie, "alternative_titles": tuple(movie["alternative_titles"])})
    fields["checks_passed"] = tuple(fields["checks_passed"])
    fields["checks_failed"] = tuple(fields["checks_failed"])
    return IdentityGateResult(**fields)
