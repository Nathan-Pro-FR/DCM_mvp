from datetime import date, datetime
from sqlalchemy import BigInteger, Integer, String, Date, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class GuildConfig(Base):
    __tablename__ = "guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stats_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reports_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    alerts_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    live_embed_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    spike_window_minutes: Mapped[int] = mapped_column(Integer, default=60)
    spike_threshold_pct: Mapped[int] = mapped_column(Integer, default=150)
    retention_days: Mapped[int] = mapped_column(Integer, default=365)

class DailyChannelStat(Base):
    """Compteurs agrégés journaliers par salon (aucun texte de message stocké)."""
    __tablename__ = "daily_channel_stats"
    __table_args__ = (UniqueConstraint("guild_id", "channel_id", "stat_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_name: Mapped[str] = mapped_column(String(120))
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    media_posts: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    videos: Mapped[int] = mapped_column(Integer, default=0)
    gifs: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[int] = mapped_column(Integer, default=0)

class DailyMemberStat(Base):
    """Compteurs agrégés journaliers par membre."""
    __tablename__ = "daily_member_stats"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "stat_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    media_posts: Mapped[int] = mapped_column(Integer, default=0)

class DailyMemberFlow(Base):
    """Arrivées et départs journaliers."""
    __tablename__ = "daily_member_flows"
    __table_args__ = (UniqueConstraint("guild_id", "stat_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    joins: Mapped[int] = mapped_column(Integer, default=0)
    leaves: Mapped[int] = mapped_column(Integer, default=0)

class ReportRecord(Base):
    """Historique des rapports générés."""
    __tablename__ = "report_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    report_type: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)
