"""Trusted local principals for manually exercising the mail workflow CLI.

This module intentionally models *local smoke-test configuration*, not a
production authentication mechanism.  A caller selects a named profile and
the process resolves the identity, roles, and mailbox scope from trusted
environment configuration rather than accepting those values as CLI flags.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

_ALLOWED_ROLES: Final[frozenset[str]] = frozenset({"author", "reviewer"})
_PROFILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PROFILE_FIELDS: Final[frozenset[str]] = frozenset({"actor_id", "roles", "allowed_account_ids"})


@dataclass(frozen=True, kw_only=True)
class WorkflowPrincipal:
    """A trusted local identity and authorization scope for workflow commands."""

    actor_id: str
    roles: frozenset[str]
    allowed_account_ids: frozenset[int]

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("workflow CLI actor_id must be a non-empty string")

        normalized_actor_id = self.actor_id.strip()
        if len(normalized_actor_id) > 128:
            raise ValueError("workflow CLI actor_id must be at most 128 characters")
        object.__setattr__(self, "actor_id", normalized_actor_id)

        if isinstance(self.roles, str):
            raise ValueError("workflow CLI roles must be a collection of role names")
        try:
            roles = frozenset(self.roles)
        except TypeError as exc:
            raise ValueError("workflow CLI roles must be a collection of role names") from exc
        if not roles:
            raise ValueError("workflow CLI roles must not be empty")
        if any(type(role) is not str or role not in _ALLOWED_ROLES for role in roles):
            raise ValueError("workflow CLI roles must contain only author or reviewer")
        object.__setattr__(self, "roles", roles)

        if isinstance(self.allowed_account_ids, (str, bytes)):
            raise ValueError("workflow CLI allowed_account_ids must be a collection of integers")
        try:
            account_ids = frozenset(self.allowed_account_ids)
        except TypeError as exc:
            raise ValueError(
                "workflow CLI allowed_account_ids must be a collection of integers"
            ) from exc
        if not account_ids:
            raise ValueError("workflow CLI allowed_account_ids must not be empty")
        if any(type(account_id) is not int or account_id <= 0 for account_id in account_ids):
            raise ValueError("workflow CLI allowed_account_ids must contain positive integers")
        object.__setattr__(self, "allowed_account_ids", account_ids)


def validate_workflow_cli_profile_name(profile_name: str) -> str:
    """Validate a profile selector without normalizing it silently."""
    if type(profile_name) is not str or _PROFILE_NAME_PATTERN.fullmatch(profile_name) is None:
        raise ValueError("workflow CLI profile name must match [a-z][a-z0-9_-]{0,63}")
    return profile_name


def parse_workflow_cli_profiles(raw: str | None) -> Mapping[str, WorkflowPrincipal]:
    """Parse ``WORKFLOW_CLI_PROFILES_JSON`` into an immutable principal map."""
    if raw is None or not raw.strip():
        return MappingProxyType({})

    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("WORKFLOW_CLI_PROFILES_JSON must be valid JSON") from exc
    except _DuplicateJsonKeyError as exc:
        raise ValueError("WORKFLOW_CLI_PROFILES_JSON must not contain duplicate fields") from exc

    if type(payload) is not dict:
        raise ValueError("WORKFLOW_CLI_PROFILES_JSON must be a JSON object")

    profiles: dict[str, WorkflowPrincipal] = {}
    for profile_name, raw_principal in payload.items():
        try:
            validated_name = validate_workflow_cli_profile_name(profile_name)
        except ValueError as exc:
            raise ValueError("WORKFLOW_CLI_PROFILES_JSON contains an invalid profile name") from exc

        if type(raw_principal) is not dict:
            raise ValueError(
                f"WORKFLOW_CLI_PROFILES_JSON profile {validated_name!r} must be an object"
            )

        _validate_profile_fields(validated_name, raw_principal)
        profiles[validated_name] = WorkflowPrincipal(
            actor_id=_parse_actor_id(validated_name, raw_principal["actor_id"]),
            roles=_parse_roles(validated_name, raw_principal["roles"]),
            allowed_account_ids=_parse_account_ids(
                validated_name, raw_principal["allowed_account_ids"]
            ),
        )

    return MappingProxyType(profiles)


def resolve_workflow_cli_profile(
    profiles: Mapping[str, WorkflowPrincipal], profile_name: str
) -> WorkflowPrincipal:
    """Resolve one configured profile without revealing the configured profile list."""
    validate_workflow_cli_profile_name(profile_name)
    try:
        return profiles[profile_name]
    except KeyError as exc:
        raise ValueError(f"unknown workflow CLI profile: {profile_name!r}") from exc


class _DuplicateJsonKeyError(ValueError):
    """Internal marker raised by ``json.loads(..., object_pairs_hook=...)``."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise _DuplicateJsonKeyError(key)
        parsed[key] = value
    return parsed


def _validate_profile_fields(profile_name: str, raw_principal: dict[str, object]) -> None:
    actual_fields = frozenset(raw_principal)
    missing_fields = _PROFILE_FIELDS - actual_fields
    if missing_fields:
        missing = ", ".join(sorted(missing_fields))
        raise ValueError(
            f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
            f"is missing required fields: {missing}"
        )

    unknown_fields = actual_fields - _PROFILE_FIELDS
    if unknown_fields:
        unknown = ", ".join(sorted(unknown_fields))
        raise ValueError(
            f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} has unknown fields: {unknown}"
        )


def _parse_actor_id(profile_name: str, raw_actor_id: object) -> str:
    if type(raw_actor_id) is not str or not raw_actor_id.strip():
        raise ValueError(
            f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
            "actor_id must be a non-empty string"
        )
    return raw_actor_id.strip()


def _parse_roles(profile_name: str, raw_roles: object) -> frozenset[str]:
    if type(raw_roles) is not list or not raw_roles:
        raise ValueError(
            f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} roles must be a non-empty array"
        )

    roles: list[str] = []
    for role in raw_roles:
        if type(role) is not str or role not in _ALLOWED_ROLES:
            raise ValueError(
                f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
                "roles must contain only author or reviewer"
            )
        if role in roles:
            raise ValueError(
                f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
                "roles must not contain duplicates"
            )
        roles.append(role)
    return frozenset(roles)


def _parse_account_ids(profile_name: str, raw_account_ids: object) -> frozenset[int]:
    if type(raw_account_ids) is not list or not raw_account_ids:
        raise ValueError(
            f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
            "allowed_account_ids must be a non-empty array"
        )

    account_ids: list[int] = []
    for account_id in raw_account_ids:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError(
                f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
                "allowed_account_ids must contain positive integers"
            )
        if account_id in account_ids:
            raise ValueError(
                f"WORKFLOW_CLI_PROFILES_JSON profile {profile_name!r} "
                "allowed_account_ids must not contain duplicates"
            )
        account_ids.append(account_id)
    return frozenset(account_ids)
