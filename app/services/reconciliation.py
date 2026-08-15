import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ChargeRecord,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationSubscription,
)
from app.notifications.service import (
    enqueue_event,
    new_charge_payload,
    tenant_dedupe_key,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

RECONCILIATION_EVENT_TYPE = "new_charge"
_reconciliation_lock = threading.Lock()


@dataclass(slots=True)
class ReconciliationSummary:
    """One glance at what a reconciliation run did."""

    scanned: int = 0
    missing_outbox: int = 0
    outbox_rebuilt: int = 0
    missing_deliveries: int = 0
    deliveries_created: int = 0
    requeued: int = 0
    already_complete: int = 0
    skipped: int = 0
    errors: int = 0
    concurrency_skipped: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class NotificationReconciliationService:
    """Reconcile the notification pipeline against committed business facts.

    Scans recently stored charge records and repairs silent notification gaps:

    * missing ``new_charge`` outbox events are rebuilt with the exact payload and
      dedupe key the normal collection flow would have produced;
    * missing per-channel deliveries are created for enabled subscriptions;
    * outbox events that already delivered but gained new channels are requeued
      so the existing delivery service can pick the new channels up.

    Guarantees:

    * never calls the Bilibili API;
    * never modifies charge records, amounts or ownership;
    * never deletes notification rows;
    * never resends succeeded deliveries;
    * never bypasses the delivery retry policy (failed deliveries and exhausted
      budgets are audited, not force-sent).
    """

    def __init__(
        self,
        lookback_hours: int | None = None,
        max_records: int | None = None,
    ) -> None:
        settings = get_settings()
        self.lookback_hours = (
            lookback_hours or settings.notification_reconciliation_lookback_hours
        )
        self.max_records = max_records or settings.notification_reconciliation_max_records

    def run(
        self,
        db: Session,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> ReconciliationSummary:
        """Run one reconciliation pass.

        ``user_id`` optionally narrows the scan to a single tenant; when omitted the
        scan covers all tenants but every repair keeps the strict tenant boundary of
        the record/outbox it repairs.
        """
        if not _reconciliation_lock.acquire(blocking=False):
            logger.warning(
                "notification_reconciliation_skipped",
                extra={"reason": "another reconciliation is already running"},
            )
            return ReconciliationSummary(concurrency_skipped=True)
        try:
            return self._run_locked(db, user_id, now or datetime.now(UTC))
        finally:
            _reconciliation_lock.release()

    def _run_locked(
        self,
        db: Session,
        user_id: str | None,
        now: datetime,
    ) -> ReconciliationSummary:
        summary = ReconciliationSummary()
        cutoff = now - timedelta(hours=self.lookback_hours)
        query = select(ChargeRecord).where(ChargeRecord.created_at >= cutoff)
        if user_id is not None:
            query = query.where(ChargeRecord.user_id == user_id)
        records = list(
            db.scalars(query.order_by(ChargeRecord.created_at.desc()).limit(self.max_records))
        )
        summary.scanned = len(records)
        for record in records:
            try:
                with db.begin_nested():
                    self._reconcile_record(db, record, now, summary)
            except IntegrityError:
                # Concurrent repair hit a unique constraint; another run owns it.
                summary.errors += 1
            except SQLAlchemyError:
                summary.errors += 1
                logger.warning(
                    "reconciliation record failed",
                    extra={"record_id": record.id},
                    exc_info=True,
                )
        db.commit()
        return summary

    def _reconcile_record(
        self,
        db: Session,
        record: ChargeRecord,
        now: datetime,
        summary: ReconciliationSummary,
    ) -> None:
        outbox, rebuilt = self._ensure_outbox(db, record)
        if outbox is None:
            summary.errors += 1
            return
        if rebuilt:
            summary.missing_outbox += 1
            summary.outbox_rebuilt += 1
        if outbox.status == "failed":
            # Retry budget exhausted: audit only, never force a resend.
            summary.skipped += 1
            return
        created = self._ensure_deliveries(db, outbox, now)
        if created:
            summary.missing_deliveries += created
            summary.deliveries_created += created
            if outbox.status == "delivered":
                # A delivered event gained new channels; requeue it so the existing
                # delivery service handles only the missing ones. Succeeded
                # deliveries are skipped by that service, so this never resends.
                outbox.status = "retry"
                outbox.available_at = now
                summary.requeued += 1
        elif not rebuilt:
            summary.already_complete += 1

    def _ensure_outbox(
        self, db: Session, record: ChargeRecord
    ) -> tuple[NotificationOutbox | None, bool]:
        dedupe_key = f"charge:{record.bili_account_id}:{record.event_id}"
        tenant_key = tenant_dedupe_key(record.user_id, dedupe_key)
        existing = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.user_id == record.user_id,
                NotificationOutbox.dedupe_key == tenant_key,
            )
        )
        if existing is not None:
            return existing, False
        event = enqueue_event(
            db,
            record.user_id,
            RECONCILIATION_EVENT_TYPE,
            dedupe_key,
            new_charge_payload(record),
        )
        if event is not None:
            return event, True
        # A concurrent run created it after our lookup; reuse theirs.
        existing = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.user_id == record.user_id,
                NotificationOutbox.dedupe_key == tenant_key,
            )
        )
        return existing, False

    def _ensure_deliveries(
        self, db: Session, outbox: NotificationOutbox, now: datetime
    ) -> int:
        """Create one pending delivery per enabled subscription+channel pair.

        The query keeps both subscription and channel scoped to the outbox owner so
        a tenant can never receive another tenant's notification.
        """
        rows = db.execute(
            select(NotificationSubscription, NotificationChannel)
            .join(
                NotificationChannel,
                NotificationChannel.id == NotificationSubscription.channel_id,
            )
            .where(
                NotificationSubscription.user_id == outbox.user_id,
                NotificationSubscription.event_type == RECONCILIATION_EVENT_TYPE,
                NotificationSubscription.enabled.is_(True),
                NotificationChannel.user_id == outbox.user_id,
                NotificationChannel.enabled.is_(True),
            )
        ).all()
        created = 0
        for _subscription, channel in rows:
            delivery = db.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.outbox_id == outbox.id,
                    NotificationDelivery.channel_id == channel.id,
                )
            )
            if delivery is not None:
                # succeeded / failed / pending deliveries are left to the normal
                # retry policy; reconciliation never resends or force-retries.
                continue
            db.add(
                NotificationDelivery(
                    user_id=outbox.user_id,
                    outbox_id=outbox.id,
                    channel_id=channel.id,
                    status="pending",
                    available_at=now,
                )
            )
            created += 1
        return created
