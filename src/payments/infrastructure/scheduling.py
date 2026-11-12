from sqlalchemy import and_, or_

from payments.infrastructure.orm import AttemptRow


def reconciliation_ready(*, now, max_checks):
    """SQL preselection, not a claim; callers supply DB time and count limit.

    A live owner wins over exhausted count/deadline. Without an owner, exhausted
    attempts are selected even before due so manual review can be persisted.
    Workers still recheck domain policy after payment then attempt locks.
    """
    return and_(
        AttemptRow.status == "unknown",
        AttemptRow.manual_review_required.is_(False),
        or_(AttemptRow.lease_until.is_(None), AttemptRow.lease_until <= now),
        or_(
            AttemptRow.check_count >= max_checks,
            AttemptRow.reconciliation_deadline <= now,
            AttemptRow.next_check_at <= now,
        ),
    )
