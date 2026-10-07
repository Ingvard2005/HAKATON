from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    DateTime,
    Boolean,
    Float,
    ForeignKey
)

from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship
)

from database import Base


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    name: Mapped[str] = mapped_column(
        String(255)
    )

    phone: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now
    )

    calls: Mapped[list["Call"]] = relationship(
        back_populates="client"
    )


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id")
    )

    call_datetime: Mapped[datetime] = mapped_column(
        DateTime
    )

    original_filename: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True
    )

    audio_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    transcript: Mapped[str] = mapped_column(
        Text
    )

    transcript_segments: Mapped[str] = mapped_column(
        Text
    )

    summary: Mapped[str] = mapped_column(
        Text
    )

    next_action: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    follow_up_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now
    )

    client: Mapped["Client"] = relationship(
        back_populates="calls"
    )

    agreements: Mapped[list["Agreement"]] = relationship(
        back_populates="call",
        cascade="all, delete-orphan"
    )


class Agreement(Base):
    __tablename__ = "agreements"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id")
    )

    description: Mapped[str] = mapped_column(
        Text
    )

    responsible: Mapped[str] = mapped_column(
        String(30)
    )

    deadline: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )

    deadline_original: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True
    )

    evidence: Mapped[str] = mapped_column(
        Text
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now
    )

    call: Mapped["Call"] = relationship(
        back_populates="agreements"
    )


class AgreementDetails(Base):
    """Additive metadata: existing call tables need no destructive migration."""
    __tablename__ = "agreement_details"
    agreement_id: Mapped[int] = mapped_column(ForeignKey("agreements.id"), primary_key=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    kind: Mapped[str] = mapped_column(String(20), default="task")
    date_only: Mapped[bool] = mapped_column(Boolean, default=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class EditRevision(Base):
    __tablename__ = "edit_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column()
    before_json: Mapped[str] = mapped_column(Text)
    after_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ExternalLink(Base):
    __tablename__ = "external_links"
    # One durable record per agreement/provider/account prevents cross-account reuse.
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    agreement_id: Mapped[int] = mapped_column(ForeignKey("agreements.id"))
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="new")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgreementReview(Base):
    __tablename__ = "agreement_reviews"
    agreement_id: Mapped[int] = mapped_column(ForeignKey("agreements.id"), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    confirmed_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_only: Mapped[bool] = mapped_column(Boolean)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime)


class CalendarBinding(Base):
    """Additive reconciliation state for a previously confirmed Google transfer."""
    __tablename__ = "calendar_bindings"
    key: Mapped[str] = mapped_column(ForeignKey("external_links.key"), primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String(255))
    baseline: Mapped[str] = mapped_column(Text, default="{}")
    conflicts: Mapped[str] = mapped_column(Text, default="{}")
    local_options: Mapped[str] = mapped_column(Text, default="{}")
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    title: Mapped[str] = mapped_column(Text)
    amount: Mapped[str | None] = mapped_column(String(80), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class DealOutcomeEvent(Base):
    __tablename__ = "deal_outcome_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id"))
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    actor: Mapped[str] = mapped_column(String(255))


class DealLink(Base):
    __tablename__ = "deal_links"
    entity_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    entity_id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id"), primary_key=True)


class IntegrationNotice(Base):
    __tablename__ = "integration_notices"
    agreement_id: Mapped[int] = mapped_column(ForeignKey("agreements.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    success: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)


class OAuthAttempt(Base):
    __tablename__ = "oauth_attempts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    expires: Mapped[float] = mapped_column(Float)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ClientDetails(Base):
    __tablename__ = "client_details"
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    crm_manager_id: Mapped[int | None] = mapped_column(nullable=True)


class CrmTaskOptions(Base):
    __tablename__ = "crm_task_options"
    agreement_id: Mapped[int] = mapped_column(ForeignKey("agreements.id"), primary_key=True)
    responsible_id: Mapped[int | None] = mapped_column(nullable=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), nullable=True)


class CrmBinding(Base):
    __tablename__ = "crm_bindings"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    portal: Mapped[str] = mapped_column(String(255))
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[int] = mapped_column()
    external_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    baseline: Mapped[str] = mapped_column(Text, default="{}")
    conflicts: Mapped[str] = mapped_column(Text, default="{}")
    state: Mapped[str] = mapped_column(String(30), default="new")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
