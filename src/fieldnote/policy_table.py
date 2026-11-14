"""The policy table: every action type the app may perform, and its color.

Decided by the developer, not generated. policy.py only looks things up here.
Anything missing from this table is refused.

    GREEN   runs and is logged
    YELLOW  the owner of the record must confirm
    RED     the named approver must approve; the requester never can

There is deliberately no action type for sending anything to a customer or
for posting an invoice. Those paths do not exist in this app.
"""

from types import MappingProxyType

from fieldnote.policy import Rule, Tier

POLICY_TABLE = MappingProxyType({
    # Salesperson's note and the model's suggestions
    "note.submit":       Rule(Tier.GREEN),
    "extraction.run":    Rule(Tier.GREEN),

    # Salesperson reviewing the suggestions
    "candidate.edit":    Rule(Tier.GREEN),
    "candidate.reject":  Rule(Tier.GREEN),
    "candidate.confirm": Rule(Tier.GREEN),

    # Writing confirmed records to Zoho CRM
    "crm.upsert":        Rule(Tier.YELLOW),

    # Quotes
    "quote.draft":              Rule(Tier.GREEN),
    "quote.validate":           Rule(Tier.GREEN),
    "quote.release":            Rule(Tier.YELLOW),
    "quote.release_discounted": Rule(Tier.RED, approver="owner"),

    # Audit
    "audit.export":      Rule(Tier.GREEN),
})
