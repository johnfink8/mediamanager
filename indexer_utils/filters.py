from indexer_utils.models import FilterRule, IgnoreItem
from indexer_utils.session import db_session


def should_ignore_by_rules(item: IgnoreItem) -> bool:
    """
    Returns True if any enabled FilterRule matches the given IgnoreItem's type and attributes.
    Supported operators: eq, neq, lt, gt, lte, gte, in, notin, contains, not_contains
    """
    session = db_session()
    rules = (
        session.query(FilterRule)
        .filter_by(item_type=item.item_type, enabled=True)
        .all()
    )
    attributes = item.attributes or {}
    for rule in rules:
        attr_val = attributes.get(rule.attribute)
        if attr_val is None:
            continue
        # Always treat as list for uniformity
        if isinstance(attr_val, list):
            values = [str(v) for v in attr_val]
        else:
            values = [str(attr_val)]
        rule_val = str(rule.value)
        op = rule.operator
        if op == "eq":
            if any(v == rule_val for v in values):
                return True
        elif op == "neq":
            if all(v != rule_val for v in values):
                return True
        elif op == "lt":
            try:
                if any(float(v) < float(rule_val) for v in values):
                    return True
            except Exception:
                continue
        elif op == "gt":
            try:
                if any(float(v) > float(rule_val) for v in values):
                    return True
            except Exception:
                continue
        elif op == "lte":
            try:
                if any(float(v) <= float(rule_val) for v in values):
                    return True
            except Exception:
                continue
        elif op == "gte":
            try:
                if any(float(v) >= float(rule_val) for v in values):
                    return True
            except Exception:
                continue
        elif op == "in":
            if rule_val in values:
                return True
        elif op == "notin":
            if rule_val not in values:
                return True
        elif op == "contains":
            if any(rule_val in v for v in values):
                return True
        elif op == "not_contains":
            if all(rule_val not in v for v in values):
                return True
    return False
