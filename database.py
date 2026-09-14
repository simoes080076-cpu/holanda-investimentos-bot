import sqlite3
import threading
from decimal import Decimal
from datetime import datetime, timezone


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def initialize(self):
        with self.lock:
            c = self.conn.cursor()
            c.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                id_informed TEXT NOT NULL,
                income TEXT NOT NULL,
                cpf_cnpj TEXT NOT NULL,
                balance TEXT NOT NULL DEFAULT '0.00',
                created_at TEXT NOT NULL,
                private_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS holdings (
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity TEXT NOT NULL DEFAULT '0',
                PRIMARY KEY(user_id, category, symbol),
                FOREIGN KEY(user_id) REFERENCES accounts(user_id)
            );

            CREATE TABLE IF NOT EXISTS prices (
                symbol TEXT PRIMARY KEY,
                price TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cdbs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ATIVO',
                paid_at TEXT,
                FOREIGN KEY(user_id) REFERENCES accounts(user_id)
            );

            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                selection TEXT NOT NULL,
                amount TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                home TEXT,
                away TEXT,
                channel_id INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDENTE',
                result TEXT,
                payout TEXT NOT NULL DEFAULT '0.00',
                resolved_at TEXT,
                UNIQUE(id)
            );

            CREATE TABLE IF NOT EXISTS pix (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                sender_old TEXT NOT NULL,
                sender_new TEXT NOT NULL,
                receiver_old TEXT NOT NULL,
                receiver_new TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                user_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)

            # Migração automática para bancos criados pela versão anterior.
            account_columns = {row[1] for row in c.execute("PRAGMA table_info(accounts)").fetchall()}
            if "private_channel_id" not in account_columns:
                c.execute("ALTER TABLE accounts ADD COLUMN private_channel_id INTEGER")

            bet_columns = {row[1] for row in c.execute("PRAGMA table_info(bets)").fetchall()}
            if "channel_id" not in bet_columns:
                c.execute("ALTER TABLE bets ADD COLUMN channel_id INTEGER")

            self.conn.commit()

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    def close(self):
        self.conn.close()

    def has_account(self, user_id):
        return self.conn.execute("SELECT 1 FROM accounts WHERE user_id=?", (user_id,)).fetchone() is not None

    def get_account(self, user_id):
        return self.conn.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchone()

    def create_account(self, user_id, name, ident, income, cpf):
        with self.lock:
            self.conn.execute(
                "INSERT INTO accounts(user_id,full_name,id_informed,income,cpf_cnpj,balance,created_at) VALUES (?, ?, ?, ?, ?, '0.00', ?)",
                (user_id, name, ident, str(income), cpf, self.now())
            )
            self.conn.commit()

    def get_private_channel_id(self, user_id):
        row = self.conn.execute("SELECT private_channel_id FROM accounts WHERE user_id=?", (user_id,)).fetchone()
        return row["private_channel_id"] if row else None

    def set_private_channel_id(self, user_id, channel_id):
        with self.lock:
            self.conn.execute("UPDATE accounts SET private_channel_id=? WHERE user_id=?", (int(channel_id), user_id))
            self.conn.commit()

    def get_balance(self, user_id):
        row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
        return Decimal(row["balance"]) if row else Decimal("0.00")

    def holdings(self, user_id, category):
        rows = self.conn.execute("SELECT symbol, quantity FROM holdings WHERE user_id=? AND category=?", (user_id, category)).fetchall()
        return {r["symbol"]: Decimal(r["quantity"]) for r in rows}

    def get_price(self, symbol):
        row = self.conn.execute("SELECT price FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return Decimal(row["price"]) if row else Decimal("0.00")

    def set_price(self, symbol, price):
        self.conn.execute("INSERT INTO prices(symbol,price,updated_at) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,updated_at=excluded.updated_at", (symbol, str(price), self.now()))
        self.conn.commit()

    def add_price_history(self, symbol, price):
        self.conn.execute("INSERT INTO price_history(symbol,price,created_at) VALUES(?,?,?)", (symbol, str(price), self.now()))
        self.conn.commit()

    def buy_asset(self, user_id, category, symbol, amount, units):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                if not row:
                    raise ValueError("Conta inexistente.")
                old = Decimal(row["balance"])
                if old < amount:
                    raise ValueError("Saldo insuficiente.")
                h = self.conn.execute("SELECT quantity FROM holdings WHERE user_id=? AND category=? AND symbol=?", (user_id, category, symbol)).fetchone()
                current = Decimal(h["quantity"]) if h else Decimal("0")
                new = old - amount
                new_qty = current + units
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.execute("INSERT INTO holdings VALUES(?,?,?,?) ON CONFLICT(user_id,category,symbol) DO UPDATE SET quantity=excluded.quantity", (user_id, category, symbol, str(new_qty)))
                self.conn.execute("INSERT INTO audit_logs(action,user_id,details,created_at) VALUES(?,?,?,?)", ("COMPRA", user_id, f"{category}:{symbol}; valor={amount}; quantidade={units}", self.now()))
                self.conn.commit()
                return old, new
            except Exception:
                self.conn.rollback()
                raise

    def sell_asset(self, user_id, category, symbol, units, price):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                h = self.conn.execute("SELECT quantity FROM holdings WHERE user_id=? AND category=? AND symbol=?", (user_id, category, symbol)).fetchone()
                if not row or not h:
                    raise ValueError("Você não possui esse ativo.")
                old_qty = Decimal(h["quantity"])
                if old_qty < units:
                    raise ValueError("Quantidade insuficiente.")
                old = Decimal(row["balance"])
                received = units * price
                new = old + received
                new_qty = old_qty - units
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.execute("UPDATE holdings SET quantity=? WHERE user_id=? AND category=? AND symbol=?", (str(new_qty), user_id, category, symbol))
                self.conn.execute("INSERT INTO audit_logs(action,user_id,details,created_at) VALUES(?,?,?,?)", ("VENDA", user_id, f"{category}:{symbol}; quantidade={units}; recebido={received}", self.now()))
                self.conn.commit()
                return old, new, received
            except Exception:
                self.conn.rollback()
                raise

    def create_cdb(self, user_id, category, amount, rate, start, end):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                if not row:
                    raise ValueError("Conta inexistente.")
                old = Decimal(row["balance"])
                if old < amount:
                    raise ValueError("Saldo insuficiente.")
                new = old - amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                cur = self.conn.execute("INSERT INTO cdbs(user_id,category,amount,rate,starts_at,ends_at) VALUES(?,?,?,?,?,?)", (user_id, category, str(amount), str(rate), start.isoformat(), end.isoformat()))
                self.conn.commit()
                return old, new, cur.lastrowid
            except Exception:
                self.conn.rollback()
                raise

    def active_cdbs(self, user_id):
        return self.conn.execute("SELECT * FROM cdbs WHERE user_id=? AND status='ATIVO' ORDER BY ends_at", (user_id,)).fetchall()

    def mature_cdbs(self):
        now = self.now()
        return self.conn.execute("SELECT * FROM cdbs WHERE status='ATIVO' AND ends_at<=?", (now,)).fetchall()

    def mature_cdb(self, cdb_id):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                c = self.conn.execute("SELECT * FROM cdbs WHERE id=? AND status='ATIVO'", (cdb_id,)).fetchone()
                if not c:
                    self.conn.rollback()
                    return False
                amount = Decimal(c["amount"])
                rate = Decimal(c["rate"])
                payout = amount * (Decimal("1") + rate)
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (c["user_id"],)).fetchone()
                if not row:
                    self.conn.execute("UPDATE cdbs SET status='CONTA_EXCLUIDA',paid_at=? WHERE id=?", (self.now(), cdb_id))
                    self.conn.commit()
                    return False
                new = Decimal(row["balance"]) + payout
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), c["user_id"]))
                self.conn.execute("UPDATE cdbs SET status='CONCLUIDO',paid_at=? WHERE id=?", (self.now(), cdb_id))
                self.conn.execute("INSERT INTO audit_logs(action,user_id,details,created_at) VALUES(?,?,?,?)", ("CDB VENCIDO", c["user_id"], f"CDB #{cdb_id}; pago={payout}", self.now()))
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def cancel_cdb(self, user_id, cdb_id):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                c = self.conn.execute("SELECT * FROM cdbs WHERE id=? AND user_id=? AND status='ATIVO'", (cdb_id, user_id)).fetchone()
                if not c:
                    raise ValueError("CDB não encontrado ou não está ativo.")
                amount = Decimal(c["amount"])
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                old = Decimal(row["balance"])
                new = old + amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.execute("UPDATE cdbs SET status='CANCELADO' WHERE id=?", (cdb_id,))
                self.conn.commit()
                return old, new, amount
            except Exception:
                self.conn.rollback()
                raise

    def transfer_pix(self, sender, receiver, amount):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                s = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (sender,)).fetchone()
                r = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (receiver,)).fetchone()
                if not s:
                    raise ValueError("O remetente não possui conta.")
                if not r:
                    raise ValueError("O destinatário não possui conta.")
                so = Decimal(s["balance"])
                ro = Decimal(r["balance"])
                if so < amount:
                    raise ValueError("Saldo insuficiente.")
                sn = so - amount
                rn = ro + amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(sn), sender))
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(rn), receiver))
                self.conn.execute("INSERT INTO pix(sender_id,receiver_id,amount,sender_old,sender_new,receiver_old,receiver_new,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (sender, receiver, str(amount), str(so), str(sn), str(ro), str(rn), "CONCLUIDO", self.now()))
                self.conn.commit()
                return {"sender_old": so, "sender_new": sn, "receiver_old": ro, "receiver_new": rn}
            except Exception:
                self.conn.rollback()
                raise

    def add_money(self, user_id, amount):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                if not row:
                    raise ValueError("Conta inexistente.")
                old = Decimal(row["balance"])
                new = old + amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.commit()
                return old, new
            except Exception:
                self.conn.rollback()
                raise

    def remove_money(self, user_id, amount):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                if not row:
                    raise ValueError("Conta inexistente.")
                old = Decimal(row["balance"])
                if old < amount:
                    raise ValueError("Saldo insuficiente.")
                new = old - amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.commit()
                return old, new
            except Exception:
                self.conn.rollback()
                raise

    def delete_account(self, user_id):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if not self.has_account(user_id):
                    raise ValueError("Conta inexistente.")
                self.conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
                self.conn.execute("UPDATE cdbs SET status='CONTA_EXCLUIDA' WHERE user_id=? AND status='ATIVO'", (user_id,))
                self.conn.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def has_active_bet(self, user_id, bet_type):
        return self.conn.execute("SELECT 1 FROM bets WHERE user_id=? AND type=? AND status='PENDENTE'", (user_id, bet_type)).fetchone() is not None

    def create_bet(self, user_id, bet_type, selection, amount, ends_at, home, away, channel_id=None):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                if not row:
                    raise ValueError("Conta inexistente.")
                if Decimal(row["balance"]) < amount:
                    raise ValueError("Saldo insuficiente.")
                old = Decimal(row["balance"])
                new = old - amount
                self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), user_id))
                self.conn.execute("INSERT INTO bets(user_id,type,selection,amount,ends_at,home,away,channel_id) VALUES(?,?,?,?,?,?,?,?)", (user_id, bet_type, selection, str(amount), ends_at.isoformat(), home, away, channel_id))
                self.conn.execute("INSERT INTO audit_logs(action,user_id,details,created_at) VALUES(?,?,?,?)", ("APOSTA", user_id, f"tipo={bet_type}; selecao={selection}; valor={amount}; canal={channel_id}", self.now()))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def pending_bets(self):
        return self.conn.execute("SELECT * FROM bets WHERE status='PENDENTE'").fetchall()

    def resolve_bet(self, bet_id, result, payout, won):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                b = self.conn.execute("SELECT * FROM bets WHERE id=? AND status='PENDENTE'", (bet_id,)).fetchone()
                if not b:
                    self.conn.rollback()
                    return False
                if payout > 0:
                    row = self.conn.execute("SELECT balance FROM accounts WHERE user_id=?", (b["user_id"],)).fetchone()
                    if row:
                        new = Decimal(row["balance"]) + payout
                        self.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (str(new), b["user_id"]))
                status = "GANHOU" if won else "PERDEU"
                self.conn.execute("UPDATE bets SET status=?,result=?,payout=?,resolved_at=? WHERE id=?", (status, result, str(payout), self.now(), bet_id))
                self.conn.execute("INSERT INTO audit_logs(action,user_id,details,created_at) VALUES(?,?,?,?)", ("APOSTA RESOLVIDA", b["user_id"], f"aposta=#{bet_id}; resultado={result}; payout={payout}; status={status}", self.now()))
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def get_setting(self, key):
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key, value):
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def recent_match(self):
        r = self.conn.execute("SELECT home,away FROM bets WHERE type='football' ORDER BY id DESC LIMIT 1").fetchone()
        return (r["home"], r["away"]) if r else None
