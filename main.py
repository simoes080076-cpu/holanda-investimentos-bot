import os
import asyncio
import logging
import random
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import Database
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_LOG_ID = 1541196829876682772
TWOPLACES = Decimal("0.01")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("holanda-bot")

CRYPTO = {
    "BTC": ("Bitcoin", Decimal("396932.39")),
    "ETH": ("Ethereum", Decimal("12863.12")),
    "USDT": ("Tether", Decimal("5.12")),
    "BNB": ("BNB", Decimal("3699.34")),
    "XRP": ("XRP", Decimal("7.02")),
}
STOCKS = {
    "NVDA": Decimal("1117.14"),
    "AAPL": Decimal("1700.00"),
    "GOOG": Decimal("1716.28"),
    "MSFT": Decimal("2535.50"),
    "AMZN": Decimal("1314.00"),
}
FIIS = {
    "KNCR11": Decimal("106.53"),
    "HGLG11": Decimal("146.00"),
    "BTLG11": Decimal("101.31"),
    "PLOG11": Decimal("66.71"),
}


def money(v: Decimal) -> Decimal:
    return Decimal(v).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def brl(v: Decimal) -> str:
    v = money(v)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def qty(v: Decimal) -> str:
    return f"{v:.8f}".rstrip("0").rstrip(".") or "0"


def parse_money(value: str) -> Decimal:
    value = value.strip().replace("R$", "").replace(" ", "")
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    amount = Decimal(value.strip())
    if amount <= 0:
        raise InvalidOperation
    return money(amount)


def sanitize_channel_name(name: str, user_id: int) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return (normalized[:95] or f"conta-{user_id}")


class HolandaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database("economia.db")
        self.recovery_lock = asyncio.Lock()

    async def setup_hook(self):
        self.db.initialize()
        self.price_loop.start()
        self.recovery_loop.start()
        synced = await self.tree.sync()
        log.info("Slash commands sincronizados: %s", len(synced))
        log.info("Banco SQLite inicializado.")

    async def on_ready(self):
        log.info("Conectado ao Discord como %s (%s)", self.user, self.user.id)

    async def on_disconnect(self):
        log.warning("Desconectado do Discord. O discord.py tentará reconectar.")

    async def on_resumed(self):
        log.info("Sessão do Discord retomada/reconectada.")

    async def close(self):
        self.price_loop.cancel()
        self.recovery_loop.cancel()
        self.db.close()
        await super().close()

    @tasks.loop(hours=1)
    async def price_loop(self):
        try:
            for symbol in list(CRYPTO) + list(STOCKS) + list(FIIS):
                old = self.db.get_price(symbol)
                if old is None or old <= 0:
                    if symbol in CRYPTO:
                        old = CRYPTO[symbol][1]
                    elif symbol in STOCKS:
                        old = STOCKS[symbol]
                    else:
                        old = FIIS[symbol]
                if random.random() < 0.5:
                    pct = Decimal(str(random.uniform(0.01, 0.10)))
                else:
                    pct = -Decimal(str(random.uniform(0.01, 0.05)))
                new = money(old * (Decimal("1") + pct))
                if new <= 0:
                    new = TWOPLACES
                self.db.set_price(symbol, new)
                self.db.add_price_history(symbol, new)
            log.info("Cotações globais atualizadas.")
        except Exception:
            log.exception("Erro isolado na atualização de cotações.")

    @price_loop.before_loop
    async def before_price_loop(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def recovery_loop(self):
        try:
            await self.recover_pending()
        except Exception:
            log.exception("Erro isolado na recuperação de operações.")

    @recovery_loop.before_loop
    async def before_recovery_loop(self):
        await self.wait_until_ready()

    async def recover_pending(self):
        async with self.recovery_lock:
            now = datetime.now(timezone.utc)
            for bet in self.db.pending_bets():
                try:
                    if datetime.fromisoformat(bet["ends_at"]) <= now:
                        await self.resolve_bet(bet)
                except Exception:
                    log.exception("Falha ao recuperar aposta #%s.", bet["id"])

            for cdb in self.db.mature_cdbs():
                try:
                    self.db.mature_cdb(cdb["id"])
                except Exception:
                    log.exception("Falha ao vencer CDB #%s.", cdb["id"])

    async def get_text_channel(self, channel_id):
        if not channel_id:
            return None
        try:
            channel = self.get_channel(int(channel_id))
            if channel is not None:
                return channel
            return await self.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return None

    async def resolve_bet(self, bet):
        if bet["type"] == "horse":
            last = self.db.get_setting("last_horse_winner")
            options = ["A", "B", "C", "D"]
            if last in options:
                options.remove(last)
            winner = random.choice(options)
            self.db.set_setting("last_horse_winner", winner)
            won = bet["selection"] == winner
            payout = money(Decimal(bet["amount"]) * Decimal("1.30")) if won else Decimal("0.00")
            resolved = self.db.resolve_bet(bet["id"], winner, payout, won)
            if not resolved:
                return

            user = self.get_user(bet["user_id"])
            if user:
                embed = discord.Embed(
                    title="🏇 Resultado da corrida",
                    color=discord.Color.green() if won else discord.Color.red()
                )
                embed.add_field(name="Corrida", value=f"#{bet['id']}", inline=True)
                embed.add_field(name="Seu cavalo", value=f"Cavalo {bet['selection']}", inline=True)
                embed.add_field(name="Vencedor", value=f"Cavalo {winner}", inline=True)
                embed.add_field(name="Aposta", value=brl(Decimal(bet["amount"])), inline=True)
                embed.add_field(name="Resultado", value="GANHOU" if won else "PERDEU", inline=True)
                embed.add_field(name="Recebido", value=brl(payout), inline=True)
                embed.set_footer(text=f"Saldo atual: {brl(self.db.get_balance(bet['user_id']))}")
                try:
                    await user.send(embed=embed)
                except discord.HTTPException:
                    log.warning("Não foi possível enviar DM do resultado da corrida para %s.", bet["user_id"])
            return

        last = self.db.get_setting("last_football_result")
        options = ["home", "away", "draw"]
        if last in options:
            options.remove(last)
            result = random.choice(options)
        else:
            r = random.random()
            result = "home" if r < 0.35 else "away" if r < 0.70 else "draw"

        self.db.set_setting("last_football_result", result)
        won = bet["selection"] == result
        multiplier = Decimal("1.35") if result == "draw" else Decimal("1.30")
        payout = money(Decimal(bet["amount"]) * multiplier) if won else Decimal("0.00")
        resolved = self.db.resolve_bet(bet["id"], result, payout, won)
        if not resolved:
            return

        result_text = {
            "home": "Vitória do mandante",
            "away": "Vitória do visitante",
            "draw": "Empate",
        }[result]
        pick_text = {
            "home": "Vitória do mandante",
            "away": "Vitória do visitante",
            "draw": "Empate",
        }[bet["selection"]]

        embed = discord.Embed(
            title="⚽ Resultado da partida",
            color=discord.Color.green() if won else discord.Color.red()
        )
        embed.add_field(name="Partida", value=f"{bet['home']} vs {bet['away']}", inline=False)
        embed.add_field(name="Sua aposta", value=pick_text, inline=True)
        embed.add_field(name="Valor", value=brl(Decimal(bet["amount"])), inline=True)
        embed.add_field(name="Resultado", value=result_text, inline=True)
        embed.add_field(name="Status", value="GANHOU" if won else "PERDEU", inline=True)
        embed.add_field(name="Recebido", value=brl(payout), inline=True)
        embed.set_footer(text=f"Saldo atual: {brl(self.db.get_balance(bet['user_id']))}")

        # O resultado do futebol é publicado no MESMO canal onde a aposta foi feita.
        channel = await self.get_text_channel(bet["channel_id"])
        if channel is None:
            log.warning(
                "Não foi possível localizar o canal %s da aposta de futebol #%s.",
                bet["channel_id"], bet["id"]
            )
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Não foi possível publicar o resultado da aposta #%s no canal %s.", bet["id"], bet["channel_id"])

bot = HolandaBot()


def account_required():
    return


async def ensure_private_account_channel(interaction: discord.Interaction, user_id: int, full_name: str):
    if interaction.guild is None:
        return None

    existing_id = bot.db.get_private_channel_id(user_id)
    if existing_id:
        existing = interaction.guild.get_channel(int(existing_id))
        if existing is None:
            existing = await bot.get_text_channel(existing_id)
        if existing is not None:
            return existing

    base_name = sanitize_channel_name(full_name, user_id)
    channel_name = base_name
    existing_names = {c.name for c in interaction.guild.text_channels}
    if channel_name in existing_names:
        suffix = f"-{str(user_id)[-6:]}"
        channel_name = f"{base_name[:100-len(suffix)]}{suffix}"

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    try:
        channel = await interaction.guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            topic=f"Canal privado da conta | Discord ID: {user_id}",
            reason="Criação automática do canal privado da conta"
        )
        bot.db.set_private_channel_id(user_id, channel.id)
        return channel
    except discord.Forbidden:
        log.warning("Sem permissão para criar canal privado para %s.", user_id)
    except discord.HTTPException:
        log.exception("Erro do Discord ao criar canal privado para %s.", user_id)
    return None


@bot.tree.command(name="abrir_conta", description="Abre sua conta na economia virtual.")
async def abrir_conta(interaction: discord.Interaction, nome_completo: str, id_informado: str, renda_atual: str, cpf_cnpj: str):
    if bot.db.has_account(interaction.user.id):
        account = bot.db.get_account(interaction.user.id)
        channel = None
        if account and not account["private_channel_id"]:
            channel = await ensure_private_account_channel(interaction, interaction.user.id, account["full_name"])
        if channel:
            return await interaction.response.send_message(
                f"Você já possui uma conta. Seu canal privado foi criado: {channel.mention}",
                ephemeral=True
            )
        return await interaction.response.send_message("Você já possui uma conta.", ephemeral=True)

    try:
        renda = parse_money(renda_atual)
    except Exception:
        return await interaction.response.send_message("Renda inválida. Use, por exemplo, 2500,00.", ephemeral=True)

    bot.db.create_account(interaction.user.id, nome_completo, id_informado, renda, cpf_cnpj)
    channel = await ensure_private_account_channel(interaction, interaction.user.id, nome_completo)

    message = "Conta criada com sucesso. Seu saldo inicial é R$ 0,00."
    if channel:
        message += f"\nSeu canal privado: {channel.mention}"
    else:
        message += "\nNão foi possível criar o canal privado automaticamente. Verifique se o bot possui permissão para gerenciar canais."

    await interaction.response.send_message(message, ephemeral=True)
    await send_admin_log(
        "CONTA CRIADA",
        interaction.user,
        f"Nome: {nome_completo}\nID informado: {id_informado}\nRenda: {brl(renda)}\nCPF/CNPJ: {cpf_cnpj}\nCanal privado: {channel.id if channel else 'não criado'}"
    )


@bot.tree.command(name="carteira", description="Mostra seu saldo e somente os investimentos que você possui.")
async def carteira(interaction: discord.Interaction):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta com /abrir_conta primeiro.", ephemeral=True)
    embed = discord.Embed(title=f"Carteira de {interaction.user.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Saldo bancário", value=brl(bot.db.get_balance(interaction.user.id)), inline=False)

    sections = []
    for title, holdings, prices in [
        ("Criptomoedas", bot.db.holdings(interaction.user.id, "crypto"), {k: bot.db.get_price(k) for k in CRYPTO}),
        ("Ações", bot.db.holdings(interaction.user.id, "stock"), {k: bot.db.get_price(k) for k in STOCKS}),
        ("Fundos Imobiliários", bot.db.holdings(interaction.user.id, "fii"), {k: bot.db.get_price(k) for k in FIIS}),
    ]:
        lines = []
        total = Decimal("0")
        for symbol, amount in holdings.items():
            if amount > 0:
                value = money(amount * prices[symbol])
                total += value
                lines.append(f"**{symbol}** — {qty(amount)} | Cotação: {brl(prices[symbol])} | Valor: {brl(value)}")
        if lines:
            sections.append((title, "\n".join(lines) + f"\n**Total: {brl(total)}**"))

    cdbs = bot.db.active_cdbs(interaction.user.id)
    if cdbs:
        lines = []
        for c in cdbs:
            lines.append(f"**#{c['id']} — {c['category'].capitalize()}** | Investido: {brl(Decimal(c['amount']))} | Vence: {c['ends_at'].replace('T',' ')[:16]} UTC")
        sections.append(("CDBs ativos", "\n".join(lines)))

    for title, value in sections:
        embed.add_field(name=title, value=value, inline=False)

    if not sections:
        embed.description = "Você ainda não possui criptomoedas, ações, FIIs ou CDBs."
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="preco", description="Mostra as cotações atuais dos ativos.")
async def preco(interaction: discord.Interaction):
    embed = discord.Embed(title="Cotações atuais", color=discord.Color.gold())
    for title, symbols in [("Criptomoedas", CRYPTO), ("Ações", STOCKS), ("FIIs", FIIS)]:
        lines = [f"**{s}** — {brl(bot.db.get_price(s))}" for s in symbols]
        embed.add_field(name=title, value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def investment(interaction, category, prices, symbol, amount, action_name):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta com /abrir_conta primeiro.", ephemeral=True)
    try:
        amount = parse_money(amount)
    except Exception:
        return await interaction.response.send_message("Valor inválido.", ephemeral=True)
    price = bot.db.get_price(symbol)
    if price <= 0:
        return await interaction.response.send_message("Cotação inválida.", ephemeral=True)
    try:
        units = amount / price
        old_balance, new_balance = bot.db.buy_asset(interaction.user.id, category, symbol, amount, units)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(
        f"Investimento realizado em **{symbol}**.\n"
        f"Valor: **{brl(amount)}**\nQuantidade: **{qty(units)}**\n"
        f"Saldo anterior: **{brl(old_balance)}**\nSaldo atual: **{brl(new_balance)}**",
        ephemeral=True
    )
    await send_admin_log(action_name, interaction.user, f"Ativo: {symbol}\nValor: {brl(amount)}\nQuantidade: {qty(units)}\nSaldo: {brl(old_balance)} → {brl(new_balance)}")


async def sell_asset(interaction, category, symbol, amount_units, action_name):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta com /abrir_conta primeiro.", ephemeral=True)
    try:
        units = Decimal(str(amount_units).replace(",", "."))
        if units <= 0:
            raise InvalidOperation
    except Exception:
        return await interaction.response.send_message("Quantidade inválida.", ephemeral=True)
    price = bot.db.get_price(symbol)
    try:
        old_balance, new_balance, received = bot.db.sell_asset(interaction.user.id, category, symbol, units, price)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(
        f"Venda realizada de **{symbol}**.\nQuantidade: **{qty(units)}**\n"
        f"Valor recebido: **{brl(received)}**\nSaldo anterior: **{brl(old_balance)}**\nSaldo atual: **{brl(new_balance)}**",
        ephemeral=True
    )
    await send_admin_log(action_name, interaction.user, f"Ativo: {symbol}\nQuantidade: {qty(units)}\nRecebido: {brl(received)}\nSaldo: {brl(old_balance)} → {brl(new_balance)}")


@bot.tree.command(name="investir_crypto", description="Investe em uma criptomoeda.")
@app_commands.choices(crypto=[app_commands.Choice(name=v[0], value=k) for k,v in CRYPTO.items()])
async def investir_crypto(interaction: discord.Interaction, crypto: app_commands.Choice[str], valor: str):
    await investment(interaction, "crypto", CRYPTO, crypto.value, valor, "COMPRA DE CRIPTO")


@bot.tree.command(name="vender_crypto", description="Vende uma criptomoeda que você possui.")
@app_commands.choices(crypto=[app_commands.Choice(name=v[0], value=k) for k,v in CRYPTO.items()])
async def vender_crypto(interaction: discord.Interaction, crypto: app_commands.Choice[str], quantidade: str):
    await sell_asset(interaction, "crypto", crypto.value, quantidade, "VENDA DE CRIPTO")


@bot.tree.command(name="investir_acoes", description="Investe em uma ação.")
@app_commands.choices(acao=[app_commands.Choice(name=k, value=k) for k in STOCKS])
async def investir_acoes(interaction: discord.Interaction, acao: app_commands.Choice[str], valor: str):
    await investment(interaction, "stock", STOCKS, acao.value, valor, "COMPRA DE AÇÃO")


@bot.tree.command(name="vender_acoes", description="Vende uma ação que você possui.")
@app_commands.choices(acao=[app_commands.Choice(name=k, value=k) for k in STOCKS])
async def vender_acoes(interaction: discord.Interaction, acao: app_commands.Choice[str], quantidade: str):
    await sell_asset(interaction, "stock", acao.value, quantidade, "VENDA DE AÇÃO")


@bot.tree.command(name="investir_fundos_imobiliarios", description="Investe em um FII.")
@app_commands.choices(fii=[app_commands.Choice(name=k, value=k) for k in FIIS])
async def investir_fundos_imobiliarios(interaction: discord.Interaction, fii: app_commands.Choice[str], valor: str):
    await investment(interaction, "fii", FIIS, fii.value, valor, "COMPRA DE FII")


@bot.tree.command(name="vender_fundos_imobiliarios", description="Vende um FII que você possui.")
@app_commands.choices(fii=[app_commands.Choice(name=k, value=k) for k in FIIS])
async def vender_fundos_imobiliarios(interaction: discord.Interaction, fii: app_commands.Choice[str], quantidade: str):
    await sell_asset(interaction, "fii", fii.value, quantidade, "VENDA DE FII")


@bot.tree.command(name="investir_cdb", description="Investe em um CDB virtual.")
@app_commands.choices(categoria=[
    app_commands.Choice(name="Diária — 24h — 1,5%", value="diaria"),
    app_commands.Choice(name="Semanal — 168h — 14%", value="semanal"),
    app_commands.Choice(name="Mensal — 5208h — 77,5%", value="mensal"),
])
async def investir_cdb(interaction: discord.Interaction, categoria: app_commands.Choice[str], valor: str):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta primeiro.", ephemeral=True)
    try:
        amount = parse_money(valor)
    except Exception:
        return await interaction.response.send_message("Valor inválido.", ephemeral=True)
    configs = {
        "diaria": (timedelta(hours=24), Decimal("0.015")),
        "semanal": (timedelta(hours=168), Decimal("0.14")),
        "mensal": (timedelta(hours=5208), Decimal("0.775")),
    }
    duration, rate = configs[categoria.value]
    start = datetime.now(timezone.utc)
    end = start + duration
    try:
        old, new, cdb_id = bot.db.create_cdb(interaction.user.id, categoria.value, amount, rate, start, end)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(f"CDB **#{cdb_id}** criado.\nInvestido: **{brl(amount)}**\nVencimento: **{end:%d/%m/%Y %H:%M} UTC**", ephemeral=True)
    await send_admin_log("CDB CRIADO", interaction.user, f"CDB #{cdb_id}\nCategoria: {categoria.value}\nValor: {brl(amount)}\nSaldo: {brl(old)} → {brl(new)}")


@bot.tree.command(name="cancelar_cdb", description="Cancela um CDB ativo e devolve apenas o principal.")
async def cancelar_cdb(interaction: discord.Interaction, cdb_id: int):
    try:
        old, new, amount = bot.db.cancel_cdb(interaction.user.id, cdb_id)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(f"CDB **#{cdb_id}** cancelado.\nDevolvido: **{brl(amount)}**\nSaldo atual: **{brl(new)}**", ephemeral=True)
    await send_admin_log("CDB CANCELADO", interaction.user, f"CDB #{cdb_id}\nPrincipal devolvido: {brl(amount)}\nSaldo: {brl(old)} → {brl(new)}")


@bot.tree.command(name="pix", description="Transfere dinheiro virtual para outro usuário.")
async def pix(interaction: discord.Interaction, quantia: str, usuario: discord.Member):
    if interaction.user.id == usuario.id:
        return await interaction.response.send_message("Você não pode enviar PIX para si mesmo.", ephemeral=True)
    try:
        amount = parse_money(quantia)
    except Exception:
        return await interaction.response.send_message("Quantia inválida.", ephemeral=True)
    try:
        result = bot.db.transfer_pix(interaction.user.id, usuario.id, amount)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(
        f"PIX enviado com sucesso para {usuario.mention}.\n"
        f"Valor: **{brl(amount)}**\nSaldo anterior: **{brl(result['sender_old'])}**\n"
        f"Saldo atual: **{brl(result['sender_new'])}", ephemeral=True
    )
    try:
        await usuario.send(f"Você recebeu um PIX de **{interaction.user.display_name}** no valor de **{brl(amount)}**.\nNovo saldo: **{brl(result['receiver_new'])}**")
    except discord.HTTPException:
        log.warning("DM do PIX não pôde ser enviada ao destinatário %s.", usuario.id)
    await send_admin_log("PIX", interaction.user, f"Destinatário: {usuario} ({usuario.id})\nValor: {brl(amount)}\nRemetente: {brl(result['sender_old'])} → {brl(result['sender_new'])}\nDestinatário: {brl(result['receiver_old'])} → {brl(result['receiver_new'])}")


@bot.tree.command(name="adicionar_dinheiro", description="Adiciona dinheiro virtual a uma conta. Administradores.")
@app_commands.default_permissions(administrator=True)
async def adicionar_dinheiro(interaction: discord.Interaction, usuario: discord.Member, quantia: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Apenas administradores podem usar este comando.", ephemeral=True)
    try:
        amount = parse_money(quantia)
    except Exception:
        return await interaction.response.send_message("Quantia inválida.", ephemeral=True)
    try:
        old, new = bot.db.add_money(usuario.id, amount)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(f"Adicionado **{brl(amount)}** para {usuario.mention}. Saldo: **{brl(new)}**.", ephemeral=True)
    await send_admin_log("DINHEIRO ADICIONADO", usuario, f"Administrador: {interaction.user} ({interaction.user.id})\nValor: {brl(amount)}\nSaldo: {brl(old)} → {brl(new)}")


@bot.tree.command(name="remover_dinheiro", description="Remove dinheiro virtual de uma conta. Administradores.")
@app_commands.default_permissions(administrator=True)
async def remover_dinheiro(interaction: discord.Interaction, usuario: discord.Member, quantia: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Apenas administradores podem usar este comando.", ephemeral=True)
    try:
        amount = parse_money(quantia)
    except Exception:
        return await interaction.response.send_message("Quantia inválida.", ephemeral=True)
    try:
        old, new = bot.db.remove_money(usuario.id, amount)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(f"Removido **{brl(amount)}** de {usuario.mention}. Saldo: **{brl(new)}**.", ephemeral=True)
    await send_admin_log("DINHEIRO REMOVIDO", usuario, f"Administrador: {interaction.user} ({interaction.user.id})\nValor: {brl(amount)}\nSaldo: {brl(old)} → {brl(new)}")


@bot.tree.command(name="excluir_conta", description="Exclui uma conta. Administradores.")
@app_commands.default_permissions(administrator=True)
async def excluir_conta(interaction: discord.Interaction, usuario: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Apenas administradores podem usar este comando.", ephemeral=True)
    account = bot.db.get_account(usuario.id)
    try:
        bot.db.delete_account(usuario.id)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)

    if account and account["private_channel_id"] and interaction.guild:
        channel = interaction.guild.get_channel(int(account["private_channel_id"]))
        if channel:
            try:
                await channel.delete(reason="Conta excluída por administrador")
            except discord.HTTPException:
                log.warning("Não foi possível excluir o canal privado da conta de %s.", usuario.id)

    await interaction.response.send_message(f"Conta de {usuario.mention} excluída. O histórico de auditoria foi preservado.", ephemeral=True)
    await send_admin_log("CONTA EXCLUÍDA", usuario, f"Administrador: {interaction.user} ({interaction.user.id})")


@bot.tree.command(name="apostar_corrida_de_cavalos", description="Aposta em uma corrida virtual de cavalos.")
@app_commands.choices(cavalo=[app_commands.Choice(name=f"Cavalo {x} — 25%", value=x) for x in "ABCD"])
async def apostar_corrida_de_cavalos(interaction: discord.Interaction, cavalo: app_commands.Choice[str], valor: str):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta primeiro.", ephemeral=True)
    try:
        amount = parse_money(valor)
    except Exception:
        return await interaction.response.send_message("Valor inválido.", ephemeral=True)
    if bot.db.has_active_bet(interaction.user.id, "horse"):
        return await interaction.response.send_message("Você já possui uma aposta de corrida ativa.", ephemeral=True)
    ends = datetime.now(timezone.utc) + timedelta(minutes=2)
    try:
        bot.db.create_bet(interaction.user.id, "horse", cavalo.value, amount, ends, "—", "—", interaction.channel.id if interaction.channel else None)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await interaction.response.send_message(f"Aposta registrada em **Cavalo {cavalo.value}** por **{brl(amount)}**. Resultado em aproximadamente 2 minutos.", ephemeral=True)
    await send_admin_log("APOSTA — CORRIDA", interaction.user, f"Cavalo: {cavalo.value}\nValor: {brl(amount)}")


@bot.tree.command(name="apostar_partida_de_futebol", description="Aposta em uma partida virtual de futebol.")
@app_commands.choices(resultado=[
    app_commands.Choice(name="Vitória do mandante — 35%", value="home"),
    app_commands.Choice(name="Vitória do visitante — 35%", value="away"),
    app_commands.Choice(name="Empate — 30%", value="draw"),
])
async def apostar_partida_de_futebol(interaction: discord.Interaction, resultado: app_commands.Choice[str], valor: str):
    if not bot.db.has_account(interaction.user.id):
        return await interaction.response.send_message("Abra sua conta primeiro.", ephemeral=True)
    if bot.db.has_active_bet(interaction.user.id, "football"):
        return await interaction.response.send_message("Você já possui uma aposta de futebol ativa.", ephemeral=True)
    try:
        amount = parse_money(valor)
    except Exception:
        return await interaction.response.send_message("Valor inválido.", ephemeral=True)

    pairs = [
        ("Time A", "Time B"),
        ("Time C", "Time D"),
        ("Time A", "Time C"),
        ("Time B", "Time D"),
        ("Time A", "Time D"),
        ("Time B", "Time C"),
    ]
    recent = bot.db.recent_match()
    random.shuffle(pairs)
    home, away = pairs[0]
    if recent and recent[0] == home and recent[1] == away and len(pairs) > 1:
        home, away = pairs[1]

    ends = datetime.now(timezone.utc) + timedelta(minutes=2)
    try:
        bot.db.create_bet(
            interaction.user.id,
            "football",
            resultado.value,
            amount,
            ends,
            home,
            away,
            interaction.channel.id if interaction.channel else None
        )
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)

    pick = {"home": "Mandante", "away": "Visitante", "draw": "Empate"}[resultado.value]
    await interaction.response.send_message(
        f"Partida: **{home} vs {away}**\n"
        f"Aposta: **{pick}**\n"
        f"Valor: **{brl(amount)}**\n"
        f"Resultado em aproximadamente 2 minutos.\n\n"
        f"O resultado será publicado neste mesmo canal.",
        ephemeral=True
    )
    await send_admin_log("APOSTA — FUTEBOL", interaction.user, f"Partida: {home} vs {away}\nResultado: {resultado.value}\nValor: {brl(amount)}\nCanal: {interaction.channel.id if interaction.channel else 'desconhecido'}")


async def send_admin_log(title, user, details):
    try:
        admin = bot.get_user(ADMIN_LOG_ID) or await bot.fetch_user(ADMIN_LOG_ID)
        embed = discord.Embed(title=f"📋 {title}", description=details, color=discord.Color.dark_gold())
        embed.add_field(name="Usuário", value=f"{user} ({user.id})", inline=False)
        embed.timestamp = datetime.now(timezone.utc)
        await admin.send(embed=embed)
    except Exception:
        log.exception("Falha ao enviar log administrativo.")


@bot.event
async def on_command_error(interaction, error):
    log.exception("Erro de comando: %s", error)


if not TOKEN:
    raise RuntimeError("A variável de ambiente DISCORD_TOKEN não foi definida.")

bot.run(TOKEN)
