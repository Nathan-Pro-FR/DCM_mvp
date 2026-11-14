from datetime import date, datetime, timedelta
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models import DailyChannelStat, ReportRecord, GuildConfig
from app.analytics import TZ, aggregate_period
from app.exports import generate_csv_bytes, generate_report_pdf

app = FastAPI(title="Discord Community Manager - Dashboard")
app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET)
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
async def startup():
    await init_db()

def require_auth(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Authentification Discord requise")
    return user

def verify_guild_admin(user: dict, guild_id: int):
    guilds = {int(g["id"]): int(g["permissions"]) for g in user.get("guilds", [])}
    perms = guilds.get(guild_id, 0)
    if (perms & 0x20) != 0x20 and (perms & 0x8) != 0x8:
        raise HTTPException(status_code=403, detail="Droits insuffisants sur ce serveur")

@app.get("/auth/login")
async def auth_login():
    params = urlencode({
        "client_id": settings.DISCORD_CLIENT_ID,
        "redirect_uri": settings.DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
    })
    return RedirectResponse(f"https://discord.com/api/oauth2/authorize?{params}")

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str):
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": settings.DISCORD_CLIENT_ID,
                "client_secret": settings.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Échec OAuth2 Discord")

        headers = {"Authorization": f"Bearer {access_token}"}
        user_info = (await client.get("https://discord.com/api/users/@me", headers=headers)).json()
        guilds_info = (await client.get("https://discord.com/api/users/@me/guilds", headers=headers)).json()

    request.session["user"] = {"profile": user_info, "guilds": guilds_info}
    return RedirectResponse("/")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": request.session.get("user")})

@app.get("/api/guilds/{guild_id}/overview")
async def api_overview(guild_id: int, days: int = 14, user: dict = Depends(require_auth)):
    verify_guild_admin(user, guild_id)
    end_d = datetime.now(TZ).date()
    start_d = end_d - timedelta(days=days - 1)

    async with AsyncSessionLocal() as session:
        summary = await aggregate_period(session, guild_id, start_d, end_d)
        series_q = await session.execute(
            select(
                DailyChannelStat.stat_date,
                func.sum(DailyChannelStat.messages),
                func.sum(DailyChannelStat.media_posts),
            )
            .where(DailyChannelStat.guild_id == guild_id, DailyChannelStat.stat_date >= start_d)
            .group_by(DailyChannelStat.stat_date)
            .order_by(DailyChannelStat.stat_date.asc())
        )
        timeline = [
            {"date": r[0].isoformat(), "messages": int(r[1]), "media_posts": int(r[2])}
            for r in series_q.all()
        ]
    return {"summary": summary, "timeline": timeline}

@app.get("/api/guilds/{guild_id}/reports")
async def api_reports(guild_id: int, user: dict = Depends(require_auth)):
    verify_guild_admin(user, guild_id)
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(ReportRecord)
            .where(ReportRecord.guild_id == guild_id)
            .order_by(ReportRecord.generated_at.desc())
            .limit(20)
        )
        items = res.scalars().all()
    return [
        {
            "id": r.id,
            "type": r.report_type,
            "start": r.start_date.isoformat(),
            "end": r.end_date.isoformat(),
            "metrics": r.payload["metrics"],
            "comparisons": r.payload["comparisons"],
        }
        for r in items
    ]

@app.get("/api/guilds/{guild_id}/export/csv")
async def api_export_csv(guild_id: int, days: int = 30, user: dict = Depends(require_auth)):
    verify_guild_admin(user, guild_id)
    start_d = datetime.now(TZ).date() - timedelta(days=days - 1)
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(DailyChannelStat).where(
                DailyChannelStat.guild_id == guild_id, DailyChannelStat.stat_date >= start_d
            )
        )
        rows = [
            {
                "date": r.stat_date.isoformat(),
                "salon": r.channel_name,
                "messages": r.messages,
                "medias": r.media_posts,
                "images": r.images,
                "videos": r.videos,
                "gifs": r.gifs,
                "fichiers": r.files,
            }
            for r in res.scalars().all()
        ]
    return Response(
        content=generate_csv_bytes(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=stats_{guild_id}.csv"},
    )
