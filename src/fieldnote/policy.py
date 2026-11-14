"""Policy engine: decides whether an action may run, using only the policy table.

The color (tier) of an action always comes from the table, looked up by the
action type. There is no way to pass a color in: ActionRequest has no such
field, so nothing produced by a model can change the classification.

    green   run and log
    yellow  the owner of the record must have confirmed
    red     the person the table names must approve, and that person
            cannot be the requester (self-approval is refused)

An action type missing from the table is refused. Every decision, allowed or
refused, is written to the audit log.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from fieldnote.audit import AuditLog


class Tier(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class Rule:
    """One row of the policy table. approver is required for red rules and
    not allowed for green or yellow ones."""

    tier: Tier
    approver: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tier, Tier):
            raise ValueError(f"tier must be a Tier, got {self.tier!r}")
        if self.tier is Tier.RED:
            if not isinstance(self.approver, str) or not self.approver.strip():
                raise ValueError("a red rule must name an approver")
        elif self.approver is not None:
            raise ValueError(f"only red rules name an approver, not {self.tier.value}")


@dataclass(frozen=True)
class ActionRequest:
    """What the caller wants to do. Deliberately has no tier field.

    approved_by is whoever said yes: the confirmer for a yellow action, the
    approver for a red one. details is recorded in the audit log and never
    read by the decision logic.
    """

    action_type: str
    requester: str
    record_owner: str | None = None
    approved_by: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("action_type", "requester"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tier: Tier | None  # None when the action type is not in the table
    reason: str


def validate_table(table: Mapping[str, Rule]) -> None:
    """Fail at startup if the table is malformed, rather than at decision time."""
    for action_type, rule in table.items():
        if not isinstance(action_type, str) or not action_type.strip():
            raise ValueError(f"action type must be a non-blank string, got {action_type!r}")
        if not isinstance(rule, Rule):
            raise ValueError(f"{action_type}: value must be a Rule, got {rule!r}")


class PolicyEngine:
    def __init__(self, audit_log: AuditLog, table: Mapping[str, Rule]) -> None:
        validate_table(table)
        # Copy into a read-only view, so changing the original dict later
        # cannot change what this engine decides.
        self._table = MappingProxyType(dict(table))
        self._audit = audit_log

    def check(self, request: ActionRequest) -> Decision:
        """Decide, log the decision, and return it."""
        decision = self._decide(request)
        self._audit.append(
            actor=request.requester,
            action_type=request.action_type,
            outcome="allowed" if decision.allowed else "refused",
            details={
                "check": "policy",
                "tier": decision.tier.value if decision.tier else None,
                "reason": decision.reason,
                "record_owner": request.record_owner,
                "approved_by": request.approved_by,
                "request_details": request.details,
            },
        )
        return decision

    def _decide(self, request: ActionRequest) -> Decision:
        # Exact match only: "CRM.UPSERT" or " crm.upsert" are unknown types.
        rule = self._table.get(request.action_type)
        if rule is None:
            return Decision(False, None, "action type not in policy table")

        if rule.tier is Tier.GREEN:
            return Decision(True, Tier.GREEN, "green: runs and is logged")

        if rule.tier is Tier.YELLOW:
            if request.record_owner is None:
                return Decision(False, Tier.YELLOW, "record owner unknown")
            if request.approved_by is None:
                return Decision(False, Tier.YELLOW, "needs confirmation by the record owner")
            if request.approved_by != request.record_owner:
                return Decision(False, Tier.YELLOW, "confirmed by someone other than the record owner")
            return Decision(True, Tier.YELLOW, "confirmed by the record owner")

        # Red. Self-approval is checked first, so it is reported as such even
        # when the requester is the named approver.
        if request.approved_by is None:
            return Decision(False, Tier.RED, f"needs approval by {rule.approver}")
        if request.approved_by == request.requester:
            return Decision(False, Tier.RED, "self-approval is not allowed")
        if request.approved_by != rule.approver:
            return Decision(False, Tier.RED, f"approved by someone other than {rule.approver}")
        return Decision(True, Tier.RED, f"approved by {rule.approver}")
