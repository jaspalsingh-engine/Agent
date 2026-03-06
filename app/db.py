import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text,
    DateTime, Boolean, ForeignKey
)
from sqlalchemy.orm import DeclarativeBase, relationship, Session

os.makedirs("data", exist_ok=True)
engine = create_engine("sqlite:///data/agent.db", echo=False)


class Base(DeclarativeBase):
    pass


class WeeklyBatch(Base):
    __tablename__ = "weekly_batches"

    id = Column(Integer, primary_key=True)
    token = Column(String, unique=True, nullable=False)
    week_start = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    digest_sent = Column(Boolean, default=False)

    accounts = relationship("Account", back_populates="batch")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("weekly_batches.id"))
    apollo_org_id = Column(String, unique=True, nullable=False)

    name = Column(String, nullable=False)
    domain = Column(String)
    industry = Column(String)
    employee_count = Column(Integer)
    annual_revenue = Column(String)
    city = Column(String)
    state = Column(String)
    linkedin_url = Column(String)
    description = Column(Text)

    propensity_score = Column(Float, default=0)
    score_reasoning = Column(Text)
    trigger_signal = Column(String)

    # pending | approved | rejected
    status = Column(String, default="pending")
    approved_at = Column(DateTime)
    rejected_at = Column(DateTime)

    selected_email_variant = Column(Integer)
    selected_li_variant = Column(Integer)

    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("WeeklyBatch", back_populates="accounts")
    contacts = relationship("Contact", back_populates="account")
    variants = relationship("OutreachVariant", back_populates="account")
    touches = relationship("TouchTask", back_populates="account")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    apollo_person_id = Column(String)

    first_name = Column(String)
    last_name = Column(String)
    title = Column(String)
    email = Column(String)
    linkedin_url = Column(String)
    rank = Column(Integer, default=1)
    rank_reason = Column(String)
    revealed = Column(Boolean, default=False)

    account = relationship("Account", back_populates="contacts")


class OutreachVariant(Base):
    __tablename__ = "outreach_variants"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    channel = Column(String)          # email | linkedin
    variant_index = Column(Integer)
    style_label = Column(String)
    subject = Column(String)
    body = Column(Text)

    account = relationship("Account", back_populates="variants")


class TouchTask(Base):
    __tablename__ = "touch_tasks"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    touch_number = Column(Integer)
    channel = Column(String)          # email | linkedin
    scheduled_date = Column(DateTime)
    # pending | sent | manual_pending | manual_done | skipped
    status = Column(String, default="pending")
    sent_at = Column(DateTime)
    resend_email_id = Column(String)  # Resend message ID

    account = relationship("Account", back_populates="touches")


class ReplyEvent(Base):
    __tablename__ = "reply_events"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    touch_task_id = Column(Integer, ForeignKey("touch_tasks.id"))
    from_address = Column(String)
    subject = Column(String)
    snippet = Column(Text)           # pasted by user
    # hot | neutral | unsubscribe | out_of_office
    sentiment = Column(String)
    flagged_at = Column(DateTime, default=datetime.utcnow)
    alert_sent = Column(Boolean, default=False)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    with Session(engine) as session:
        yield session
