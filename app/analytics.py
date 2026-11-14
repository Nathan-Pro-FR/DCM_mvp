from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models import DailyChannelStat, DailyMemberStat, DailyMemberFlow, ReportRecord

TZ = ZoneInfo(settings.TIMEZONE)

def format_evolution(current: int, previous: int) -> dict:
    diff = current - previous
    if previous == 0:
        pct_str = "Nouvelle activité" if current > 0 else "0 %"
    else:
        pct = ((current - previous) / previous) * 100
        pct_str = f"{pct:+.1f} %"
    return {"current": current, "previous": previous, "diff": diff, "evolution": pct_str}

def get_civil_periods(report_type: str, ref_date: date | None = None) -> tuple[tuple[date, date], tuple[date, date]]:
    """Retourne ((début_actuel, fin_actuelle), (début_précédent, fin_précédent)) en dates civiles."""
    today = ref_date or datetime.now(TZ).date()

    if report_type == "daily":
        end_cur = today - timedelta(days=1)
        start_cur = end_cur
        end_prev = start_cur - timedelta(days=1)
        start_prev = end_prev

    elif report_type == "weekly":
        days_since_monday = today.weekday()
        end_cur = today - timedelta(days=days_since_monday + 1)
        start_cur = end_cur - timedelta(days=6)
        end_prev = start_cur - timedelta(days=1)
        start_prev = end_prev - timedelta(days=6)

    elif report_type == "monthly":
        first_this_month = today.replace(day=1)
        end_cur = first_this_month - timedelta(days=1)
        start_cur = end_cur.replace(day=1)
        end_prev = start_cur - timedelta(days=1)
        start_prev = end_prev.replace(day=1)

    elif report_type == "quarterly":
        current_q = (today.month - 1) // 3 + 1
        first_month_cur_q = 3 * (current_q - 1) + 1
        first_day_cur_q = today.replace(month=first_month_cur_q, day=1)
        end_cur = first_day_cur_q - timedelta(days=1)
        start_cur = end_cur.replace(month=((end_cur.month - 1) // 3) * 3 + 1, day=1)
        end_prev = start_cur - timedelta(days=1)
        start_prev = end_prev.replace(month=((end_prev.month - 1) // 3) * 3 + 1, day=1)
    else:
        raise ValueError("Type de rapport invalide")

    return (start_cur, end_cur), (start_prev, end_prev)

async def aggregate_period(session: AsyncSession, guild_id: int, start_d: date, end_d: date) -> dict:
    ch_q = await session.execute(
        select(
            func.coalesce(func.sum(DailyChannelStat.messages), 0),
            func.coalesce(func.sum(DailyChannelStat.media_posts), 0),
            func.coalesce(func.sum(DailyChannelStat.images), 0),
            func.coalesce(func.sum(DailyChannelStat.videos), 0),
            func.coalesce(func.sum(DailyChannelStat.gifs), 0),
            func.coalesce(func.sum(DailyChannelStat.files), 0),
        ).where(
            DailyChannelStat.guild_id == guild_id,
            DailyChannelStat.stat_date >= start_d,
            DailyChannelStat.stat_date <= end_d,
        )
    )
    msgs, medias, imgs, vids, gifs, files = ch_q.one()

    active_q = await session.execute(
        select(func.count(func.distinct(DailyMemberStat.user_id))).where(
            DailyMemberStat.guild_id == guild_id,
            DailyMemberStat.stat_date >= start_d,
            DailyMemberStat.stat_date <= end_d,
            DailyMemberStat.messages > 0,
        )
    )
    active_members = active_q.scalar() or 0

    flow_q = await session.execute(
        select(
            func.coalesce(func.sum(DailyMemberFlow.joins), 0),
            func.coalesce(func.sum(DailyMemberFlow.leaves), 0),
        ).where(
            DailyMemberFlow.guild_id == guild_id,
            DailyMemberFlow.stat_date >= start_d,
            DailyMemberFlow.stat_date <= end_d,
        )
    )
    joins, leaves = flow_q.one()

    top_ch_q = await session.execute(
        select(
            DailyChannelStat.channel_name,
            func.sum(DailyChannelStat.messages).label("total"),
        )
        .where(
            DailyChannelStat.guild_id == guild_id,
            DailyChannelStat.stat_date >= start_d,
            DailyChannelStat.stat_date <= end_d,
        )
        .group_by(DailyChannelStat.channel_name)
        .order_by(func.sum(DailyChannelStat.messages).desc())
        .limit(10)
    )
    top_channels = [{"channel": r[0], "messages": int(r[1])} for r in top_ch_q.all()]

    return {
        "messages": int(msgs),
        "media_posts": int(medias),
        "images": int(imgs),
        "videos": int(vids),
        "gifs": int(gifs),
        "files": int(files),
        "active_members": int(active_members),
        "joins": int(joins),
        "leaves": int(leaves),
        "top_channels": top_channels,
    }

async def build_and_save_report(session: AsyncSession, guild_id: int, report_type: str) -> ReportRecord:
    (s_cur, e_cur), (s_prev, e_prev) = get_civil_periods(report_type)
    cur_data = await aggregate_period(session, guild_id, s_cur, e_cur)
    prev_data = await aggregate_period(session, guild_id, s_prev, e_prev)

    comparisons = {
        k: format_evolution(cur_data[k], prev_data[k])
        for k in ["messages", "media_posts", "active_members", "joins", "leaves"]
    }

    now_paris = datetime.now(TZ)
    payload = {
        "period": {"start": s_cur.isoformat(), "end": e_cur.isoformat()},
        "previous_period": {"start": s_prev.isoformat(), "end": e_prev.isoformat()},
        "metrics": cur_data,
        "comparisons": comparisons,
        "generated_at": now_paris.isoformat(),
    }

    record = ReportRecord(
        guild_id=guild_id,
        report_type=report_type,
        start_date=s_cur,
        end_date=e_cur,
        generated_at=now_paris,
        payload=payload,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record
