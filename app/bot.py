import io
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models import GuildConfig, DailyChannelStat, DailyMemberStat, DailyMemberFlow, ReportRecord
from app.analytics import TZ, aggregate_period, build_and_save_report, format_evolution
from app.exports import generate_report_pdf

SERVER_STRUCTURE = {
    "📌 ACCUEIL": ["règlement", "annonces", "présentations"],
    "💬 COMMUNAUTÉ": ["général", "discussions", "questions"],
    "🖼️ MÉDIAS": ["photos", "vidéos", "créations"],
    "🤖 BOTS": ["commandes-bot"],
    "📊 STATISTIQUES": ["activité", "rapports"],
    "🛡️ MODÉRATION": ["signalements", "journal-modération"],
}

ROLE_DEFINITIONS = [
    {
        "name": "Administrateur",
        "color": discord.Color.red(),
        "perms": discord.Permissions(
            manage_guild=True,
            manage_channels=True,
            manage_roles=True,
            manage_messages=True,
            view_audit_log=True,
            read_messages=True,
            send_messages=True,
        ),
    },
    {
        "name": "Modérateur",
        "color": discord.Color.orange(),
        "perms": discord.Permissions(
            manage_messages=True,
            moderate_members=True,
            view_audit_log=True,
            read_messages=True,
            send_messages=True,
        ),
    },
    {
        "name": "Membre",
        "color": discord.Color.blue(),
        "perms": discord.Permissions(
            read_messages=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    },
    {
        "name": "Nouveau",
        "color": discord.Color.light_grey(),
        "perms": discord.Permissions(
            read_messages=True,
            read_message_history=True,
            send_messages=False,
        ),
    },
    {
        "name": "Bots",
        "color": discord.Color.purple(),
        "perms": discord.Permissions(
            read_messages=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
        ),
    },
]

def classify_attachment(att: discord.Attachment) -> str:
    ct = (att.content_type or "").lower()
    fn = att.filename.lower()
    if "gif" in ct or fn.endswith(".gif"):
        return "gifs"
    if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "images"
    if ct.startswith("video/") or fn.endswith((".mp4", ".mov", ".webm", ".mkv")):
        return "videos"
    return "files"


class CommunityManagerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        super().__init__(command_prefix="!", intents=intents)

        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self.dirty_guilds: set[int] = set()
        self.recent_timestamps: dict[tuple[int, int], deque[datetime]] = defaultdict(deque)
        self.last_alert_sent: dict[tuple[int, int], datetime] = {}

    async def setup_hook(self):
        await init_db()
        await self.tree.sync()

        self.scheduler.add_job(self.run_scheduled_reports, "cron", hour=9, minute=0, args=["daily"])
        self.scheduler.add_job(self.run_scheduled_reports, "cron", day_of_week="mon", hour=9, minute=5, args=["weekly"])
        self.scheduler.add_job(self.run_scheduled_reports, "cron", day=1, hour=9, minute=10, args=["monthly"])
        self.scheduler.add_job(self.run_scheduled_reports, "cron", month="1,4,7,10", day=1, hour=9, minute=15, args=["quarterly"])
        self.scheduler.start()

        self.refresh_live_embeds.start()

    async def ensure_guild_config(self, session, guild_id: int) -> GuildConfig:
        cfg = await session.get(GuildConfig, guild_id)
        if not cfg:
            cfg = GuildConfig(guild_id=guild_id)
            session.add(cfg)
            await session.commit()
        return cfg

    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        now = datetime.now(TZ)
        today = now.date()
        gid = message.guild.id
        cid = message.channel.id
        uid = message.author.id

        counts = {"images": 0, "videos": 0, "gifs": 0, "files": 0}
        for att in message.attachments:
            counts[classify_attachment(att)] += 1
        has_media = 1 if message.attachments else 0

        async with AsyncSessionLocal() as session:
            stmt_ch = pg_insert(DailyChannelStat).values(
                guild_id=gid,
                channel_id=cid,
                channel_name=getattr(message.channel, "name", "inconnu"),
                stat_date=today,
                messages=1,
                media_posts=has_media,
                **counts,
            ).on_conflict_do_update(
                index_elements=["guild_id", "channel_id", "stat_date"],
                set_={
                    "channel_name": getattr(message.channel, "name", "inconnu"),
                    "messages": DailyChannelStat.messages + 1,
                    "media_posts": DailyChannelStat.media_posts + has_media,
                    "images": DailyChannelStat.images + counts["images"],
                    "videos": DailyChannelStat.videos + counts["videos"],
                    "gifs": DailyChannelStat.gifs + counts["gifs"],
                    "files": DailyChannelStat.files + counts["files"],
                },
            )
            await session.execute(stmt_ch)

            stmt_mem = pg_insert(DailyMemberStat).values(
                guild_id=gid,
                user_id=uid,
                stat_date=today,
                messages=1,
                media_posts=has_media,
            ).on_conflict_do_update(
                index_elements=["guild_id", "user_id", "stat_date"],
                set_={
                    "messages": DailyMemberStat.messages + 1,
                    "media_posts": DailyMemberStat.media_posts + has_media,
                },
            )
            await session.execute(stmt_mem)
            await session.commit()

        self.dirty_guilds.add(gid)
        await self.check_activity_spike(message.guild, message.channel, now)

    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        today = datetime.now(TZ).date()
        async with AsyncSessionLocal() as session:
            stmt = pg_insert(DailyMemberFlow).values(
                guild_id=member.guild.id, stat_date=today, joins=1, leaves=0
            ).on_conflict_do_update(
                index_elements=["guild_id", "stat_date"],
                set_={"joins": DailyMemberFlow.joins + 1},
            )
            await session.execute(stmt)
            await session.commit()

    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        today = datetime.now(TZ).date()
        async with AsyncSessionLocal() as session:
            stmt = pg_insert(DailyMemberFlow).values(
                guild_id=member.guild.id, stat_date=today, joins=0, leaves=1
            ).on_conflict_do_update(
                index_elements=["guild_id", "stat_date"],
                set_={"leaves": DailyMemberFlow.leaves + 1},
            )
            await session.execute(stmt)
            await session.commit()

    async def check_activity_spike(self, guild: discord.Guild, channel: discord.abc.GuildChannel, now: datetime):
        async with AsyncSessionLocal() as session:
            cfg = await self.ensure_guild_config(session, guild.id)
            if not cfg.alerts_channel_id:
                return

            key = (guild.id, channel.id)
            dq = self.recent_timestamps[key]
            dq.append(now)

            window = timedelta(minutes=cfg.spike_window_minutes)
            while dq and (now - dq[0]) > window:
                dq.popleft()

            last_sent = self.last_alert_sent.get(key)
            if last_sent and (now - last_sent) < window:
                return

            week_ago = now.date() - timedelta(days=7)
            hist = await aggregate_period(session, guild.id, week_ago, now.date() - timedelta(days=1))
            baseline_per_window = max(5.0, (hist["messages"] / 7.0) * (cfg.spike_window_minutes / 1440.0))

            if len(dq) >= baseline_per_window * (cfg.spike_threshold_pct / 100.0):
                self.last_alert_sent[key] = now
                alert_ch = guild.get_channel(cfg.alerts_channel_id)
                if isinstance(alert_ch, discord.TextChannel):
                    embed = discord.Embed(
                        title="⚠️ Pic d'activité détecté",
                        description=(
                            f"Salon : {channel.mention}\n"
                            f"Volume : **{len(dq)} messages** sur les dernières {cfg.spike_window_minutes} min "
                            f"(seuil : {cfg.spike_threshold_pct} % de la référence)."
                        ),
                        color=discord.Color.gold(),
                        timestamp=now,
                    )
                    await alert_ch.send(embed=embed)

    @tasks.loop(seconds=60)
    async def refresh_live_embeds(self):
        if not self.dirty_guilds:
            return
        guilds_to_update = list(self.dirty_guilds)
        self.dirty_guilds.clear()

        today = datetime.now(TZ).date()
        async with AsyncSessionLocal() as session:
            for gid in guilds_to_update:
                guild = self.get_guild(gid)
                if not guild:
                    continue
                cfg = await self.ensure_guild_config(session, gid)
                if not cfg.stats_channel_id:
                    continue

                channel = guild.get_channel(cfg.stats_channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue

                stats = await aggregate_period(session, gid, today, today)
                embed = discord.Embed(
                    title=f"📊 Activité du jour — {today.strftime('%d/%m/%Y')}",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(TZ),
                )
                embed.add_field(name="💬 Messages", value=str(stats["messages"]), inline=True)
                embed.add_field(name="👥 Membres actifs", value=str(stats["active_members"]), inline=True)
                embed.add_field(name="📎 Posts médias", value=str(stats["media_posts"]), inline=True)
                embed.add_field(name="🖼️ Images", value=str(stats["images"]), inline=True)
                embed.add_field(name="🎬 Vidéos", value=str(stats["videos"]), inline=True)
                embed.add_field(name="🎞️ GIF / 📄 Fichiers", value=f"{stats['gifs']} / {stats['files']}", inline=True)
                embed.set_footer(text="Mise à jour automatique • Compteurs agrégés sans contenu")

                msg = None
                if cfg.live_embed_message_id:
                    try:
                        msg = await channel.fetch_message(cfg.live_embed_message_id)
                        await msg.edit(embed=embed)
                    except discord.NotFound:
                        msg = None

                if msg is None:
                    new_msg = await channel.send(embed=embed)
                    cfg.live_embed_message_id = new_msg.id
                    await session.commit()

    async def run_scheduled_reports(self, report_type: str):
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(GuildConfig))
            configs = res.scalars().all()
            for cfg in configs:
                guild = self.get_guild(cfg.guild_id)
                if not guild or not cfg.reports_channel_id:
                    continue
                record = await build_and_save_report(session, cfg.guild_id, report_type)
                ch = guild.get_channel(cfg.reports_channel_id)
                if isinstance(ch, discord.TextChannel):
                    await ch.send(embed=self.build_report_embed(record))

    def build_report_embed(self, record: ReportRecord) -> discord.Embed:
        p = record.payload["period"]
        c = record.payload["comparisons"]
        embed = discord.Embed(
            title=f"📑 Rapport {record.report_type.capitalize()} ({p['start']} → {p['end']})",
            color=discord.Color.green(),
            timestamp=record.generated_at,
        )
        for key, label in [
            ("messages", "💬 Messages"),
            ("media_posts", "📎 Publications médias"),
            ("active_members", "👥 Membres actifs"),
            ("joins", "📥 Arrivées"),
            ("leaves", "📤 Départs"),
        ]:
            item = c[key]
            embed.add_field(
                name=label,
                value=f"**{item['current']}** (préc. {item['previous']} | {item['evolution']})",
                inline=True,
            )

        top = record.payload["metrics"].get("top_channels", [])
        if top:
            ranking = "\n".join(f"**{i+1}.** #{x['channel']} — {x['messages']} msg" for i, x in enumerate(top[:5]))
            embed.add_field(name="🏆 Top salons", value=ranking, inline=False)
        return embed


bot = CommunityManagerBot()


@bot.tree.command(name="setup", description="Créer les catégories, salons et rôles manquants sans doublons.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    created_roles, created_channels = [], []

    existing_roles = {r.name: r for r in guild.roles}
    for r_def in ROLE_DEFINITIONS:
        if r_def["name"] not in existing_roles:
            await guild.create_role(
                name=r_def["name"],
                color=r_def["color"],
                permissions=r_def["perms"],
                reason="Setup Discord Community Manager",
            )
            created_roles.append(r_def["name"])

    existing_cats = {c.name: c for c in guild.categories}
    stats_ch_id = reports_ch_id = alerts_ch_id = None

    for cat_name, channels in SERVER_STRUCTURE.items():
        category = existing_cats.get(cat_name)
        if not category:
            category = await guild.create_category(cat_name)
            created_channels.append(f"[Catégorie] {cat_name}")

        existing_text = {c.name: c for c in category.text_channels}
        for ch_name in channels:
            ch = existing_text.get(ch_name)
            if not ch:
                ch = await guild.create_text_channel(ch_name, category=category)
                created_channels.append(f"#{ch_name}")
            if ch_name == "activité":
                stats_ch_id = ch.id
            elif ch_name == "rapports":
                reports_ch_id = ch.id
            elif ch_name == "journal-modération":
                alerts_ch_id = ch.id

    async with AsyncSessionLocal() as session:
        cfg = await bot.ensure_guild_config(session, guild.id)
        cfg.stats_channel_id = cfg.stats_channel_id or stats_ch_id
        cfg.reports_channel_id = cfg.reports_channel_id or reports_ch_id
        cfg.alerts_channel_id = cfg.alerts_channel_id or alerts_ch_id
        await session.commit()

    bot.dirty_guilds.add(guild.id)
    await interaction.followup.send(
        f"✅ **Provisioning terminé**\n"
        f"• Rôles créés ({len(created_roles)}) : {', '.join(created_roles) or 'Aucun'}\n"
        f"• Éléments créés ({len(created_channels)}) : {', '.join(created_channels) or 'Aucun'}",
        ephemeral=True,
    )


@bot.tree.command(name="setup-check", description="Vérifier la conformité de la structure et des rôles.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_setup_check(interaction: discord.Interaction):
    guild = interaction.guild
    missing = []
    existing_roles = {r.name: r for r in guild.roles}
    for r_def in ROLE_DEFINITIONS:
        role = existing_roles.get(r_def["name"])
        if not role:
            missing.append(f"Rôle manquant : **{r_def['name']}**")
        elif r_def["name"] == "Administrateur" and role.permissions.administrator:
            missing.append("⚠️ Le rôle **Administrateur** possède la permission Discord `Administrator` !")

    existing_cats = {c.name: c for c in guild.categories}
    for cat_name, channels in SERVER_STRUCTURE.items():
        cat = existing_cats.get(cat_name)
        if not cat:
            missing.append(f"Catégorie manquante : **{cat_name}**")
            continue
        ch_names = {c.name for c in cat.text_channels}
        for ch in channels:
            if ch not in ch_names:
                missing.append(f"Salon manquant : **#{ch}** dans {cat_name}")

    status = "✅ Structure 100 % conforme." if not missing else "⚠️ **Anomalies détectées :**\n" + "\n".join(f"- {m}" for m in missing)
    await interaction.response.send_message(status, ephemeral=True)


@bot.tree.command(name="stats", description="Consulter les statistiques sur N jours.")
async def cmd_stats(interaction: discord.Interaction, jours: app_commands.Range[int, 1, 90] = 7):
    end_d = datetime.now(TZ).date()
    start_d = end_d - timedelta(days=jours - 1)
    async with AsyncSessionLocal() as session:
        data = await aggregate_period(session, interaction.guild.id, start_d, end_d)

    embed = discord.Embed(title=f"📊 Statistiques ({jours} derniers jours)", color=discord.Color.blurple())
    embed.add_field(name="Messages", value=str(data["messages"]))
    embed.add_field(name="Publications médias", value=str(data["media_posts"]))
    embed.add_field(name="Membres actifs", value=str(data["active_members"]))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats-member", description="Statistiques agrégées d'un membre.")
@app_commands.default_permissions(moderate_members=True)
async def cmd_stats_member(interaction: discord.Interaction, membre: discord.Member, jours: app_commands.Range[int, 1, 90] = 30):
    end_d = datetime.now(TZ).date()
    start_d = end_d - timedelta(days=jours - 1)
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(DailyMemberStat).where(
                DailyMemberStat.guild_id == interaction.guild.id,
                DailyMemberStat.user_id == membre.id,
                DailyMemberStat.stat_date >= start_d,
            )
        )
        rows = res.scalars().all()
    total_msg = sum(r.messages for r in rows)
    total_med = sum(r.media_posts for r in rows)
    await interaction.response.send_message(
        f"👤 **{membre.display_name}** ({jours}j) : **{total_msg}** messages, **{total_med}** posts médias.",
        ephemeral=True,
    )


@bot.tree.command(name="stats-channel", description="Statistiques agrégées d'un salon.")
@app_commands.default_permissions(moderate_members=True)
async def cmd_stats_channel(interaction: discord.Interaction, salon: discord.TextChannel, jours: app_commands.Range[int, 1, 90] = 30):
    end_d = datetime.now(TZ).date()
    start_d = end_d - timedelta(days=jours - 1)
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(DailyChannelStat).where(
                DailyChannelStat.guild_id == interaction.guild.id,
                DailyChannelStat.channel_id == salon.id,
                DailyChannelStat.stat_date >= start_d,
            )
        )
        rows = res.scalars().all()
    total_msg = sum(r.messages for r in rows)
    total_med = sum(r.media_posts for r in rows)
    await interaction.response.send_message(
        f"💬 **#{salon.name}** ({jours}j) : **{total_msg}** messages, **{total_med}** posts médias.",
        ephemeral=True,
    )


@bot.tree.command(name="report", description="Générer immédiatement un rapport périodique (avec PDF).")
@app_commands.choices(periode=[
    app_commands.Choice(name="Journalier", value="daily"),
    app_commands.Choice(name="Hebdomadaire", value="weekly"),
    app_commands.Choice(name="Mensuel", value="monthly"),
    app_commands.Choice(name="Trimestriel", value="quarterly"),
])
@app_commands.default_permissions(manage_guild=True)
async def cmd_report(interaction: discord.Interaction, periode: str):
    await interaction.response.defer(ephemeral=True)
    async with AsyncSessionLocal() as session:
        record = await build_and_save_report(session, interaction.guild.id, periode)

    pdf_bytes = generate_report_pdf(periode, record.payload)
    file = discord.File(io.BytesIO(pdf_bytes), filename=f"rapport_{periode}_{record.end_date}.pdf")
    await interaction.followup.send(embed=bot.build_report_embed(record), file=file, ephemeral=True)


@bot.tree.command(name="report-history", description="Consulter les 10 derniers rapports enregistrés.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_report_history(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(ReportRecord)
            .where(ReportRecord.guild_id == interaction.guild.id)
            .order_by(ReportRecord.generated_at.desc())
            .limit(10)
        )
        records = res.scalars().all()

    if not records:
        return await interaction.response.send_message("Aucun rapport archivé.", ephemeral=True)

    lines = [
        f"• **ID #{r.id}** [{r.report_type}] {r.start_date} → {r.end_date} ({r.payload['metrics']['messages']} msg)"
        for r in records
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="report-compare", description="Comparer deux périodes personnalisées (AAAA-MM-JJ).")
@app_commands.default_permissions(manage_guild=True)
async def cmd_report_compare(interaction: discord.Interaction, debut_a: str, fin_a: str, debut_b: str, fin_b: str):
    try:
        sa, ea = datetime.strptime(debut_a, "%Y-%m-%d").date(), datetime.strptime(fin_a, "%Y-%m-%d").date()
        sb, eb = datetime.strptime(debut_b, "%Y-%m-%d").date(), datetime.strptime(fin_b, "%Y-%m-%d").date()
    except ValueError:
        return await interaction.response.send_message("Format attendu : `AAAA-MM-JJ`.", ephemeral=True)

    async with AsyncSessionLocal() as session:
        data_a = await aggregate_period(session, interaction.guild.id, sa, ea)
        data_b = await aggregate_period(session, interaction.guild.id, sb, eb)

    ev_msg = format_evolution(data_b["messages"], data_a["messages"])
    ev_mem = format_evolution(data_b["active_members"], data_a["active_members"])
    await interaction.response.send_message(
        f"📈 **Comparaison B ({sb}→{eb}) vs A ({sa}→{ea})**\n"
        f"• Messages : **{ev_msg['current']}** vs {ev_msg['previous']} ({ev_msg['evolution']})\n"
        f"• Membres actifs : **{ev_mem['current']}** vs {ev_mem['previous']} ({ev_mem['evolution']})",
        ephemeral=True,
    )


@bot.tree.command(name="alerts-config", description="Configurer la détection des pics d'activité.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_alerts_config(
    interaction: discord.Interaction,
    fenetre_minutes: app_commands.Range[int, 5, 360],
    seuil_pourcent: app_commands.Range[int, 100, 1000],
    salon_alertes: discord.TextChannel | None = None,
):
    async with AsyncSessionLocal() as session:
        cfg = await bot.ensure_guild_config(session, interaction.guild.id)
        cfg.spike_window_minutes = fenetre_minutes
        cfg.spike_threshold_pct = seuil_pourcent
        if salon_alertes:
            cfg.alerts_channel_id = salon_alertes.id
        await session.commit()

    await interaction.response.send_message(
        f"🔔 Alertes configurées : fenêtre **{fenetre_minutes} min**, seuil **{seuil_pourcent} %**.",
        ephemeral=True,
    )


@bot.tree.command(name="config", description="Définir les salons de statistiques et de rapports.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_config(
    interaction: discord.Interaction,
    salon_stats: discord.TextChannel | None = None,
    salon_rapports: discord.TextChannel | None = None,
):
    async with AsyncSessionLocal() as session:
        cfg = await bot.ensure_guild_config(session, interaction.guild.id)
        if salon_stats:
            cfg.stats_channel_id = salon_stats.id
            cfg.live_embed_message_id = None
        if salon_rapports:
            cfg.reports_channel_id = salon_rapports.id
        await session.commit()

    bot.dirty_guilds.add(interaction.guild.id)
    await interaction.response.send_message("⚙️ Configuration enregistrée.", ephemeral=True)


@bot.tree.command(name="dashboard", description="Obtenir le lien vers le dashboard Web.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_dashboard(interaction: discord.Interaction):
    base_url = settings.DISCORD_REDIRECT_URI.replace("/auth/callback", "")
    await interaction.response.send_message(
        f"🌐 Accédez au dashboard communautaire : {base_url}/?guild_id={interaction.guild.id}",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(settings.DISCORD_TOKEN)
