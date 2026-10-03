#!/usr/bin/env python3
"""Tárcafigyelő – helyi webes felület.

Indítás:
    python3 app.py                 # csak ezen a gépen: http://127.0.0.1:8765
    python3 app.py --host 0.0.0.0  # az otthoni hálózatról (például telefonról) is elérhető

A felület a fetch.py adatbázisából dolgozik. Csak olvas a láncról: privát kulcsot
vagy seed phrase-t nem kér és nem kezel.
"""

import argparse
import contextlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fetch

WEB = os.path.join(fetch.HERE, "web")
REFRESH = {"running": False, "log": "", "code": None, "started_at": None, "finished_at": None}
REFRESH_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Az állapot összeállítása a felületnek
# --------------------------------------------------------------------------

def token_json(label, address, token, names):
    change = percent = None
    if token["value"] is not None and token["then_value"] is not None:
        change = token["value"] - token["then_value"]
        if token["then_value"] > 0:
            percent = change / token["then_value"] * 100
    lots = []
    for lot in token["lots"]:
        lots.append({
            "id": lot["id"], "time": lot["time"],
            "amount": lot["amount"], "original": lot["original"],
            "acq": lot["acq"], "acq_manual": lot["acq_manual"], "initiated": lot["initiated"],
            "price": lot["price"], "basis": lot["basis"], "tx_hash": lot["tx_hash"],
            "then_value": None if lot["price"] is None else lot["amount"] * lot["price"],
            "now_value": None if token["price"] is None else lot["amount"] * token["price"],
        })
    times = sorted(lot["time"] for lot in token["lots"] if lot["time"])
    return {
        "id": "%s|%s|%s" % (address, token["chain"], token["token"]),
        "wallet": address, "wallet_label": label,
        "chain": token["chain"], "chain_name": names.get(token["chain"], token["chain"]),
        "token": token["token"], "symbol": token["symbol"], "name": token["name"],
        "amount": token["amount"], "price": token["price"], "value": token["value"],
        "then_value": token["then_value"], "change": change, "change_pct": percent,
        "native": token["native"], "nft": token["nft"],
        "spam": token["spam"], "spam_reason": token["spam_reason"],
        "spam_manual": token["spam_manual"],
        "all_free": token["all_free"], "part_free": token["part_free"],
        "leftover": token["leftover"], "no_history": token["no_history"],
        "mismatch": token["mismatch"], "partly_unknown": token["partly_unknown"],
        "pending": token["pending"],
        "price_source": token["price_source"], "liquidity": token["liquidity"],
        "fee": token["fee"], "dust": token["dust"], "watch": token["watch"],
        "first_time": times[0] if times else None,
        "last_time": times[-1] if times else None,
        "lots": lots,
    }


def build_state(db, config):
    names = {slug: name for slug, name, _ in config["chains"]}
    fees = dict(db.execute("SELECT chain, fee_usd FROM chain_info").fetchall())
    tokens = []
    for label, address in config["wallets"]:
        for token in fetch.evaluate(db, address):
            tokens.append(token_json(label, address, token, names))
    last_sync = db.execute("SELECT MAX(updated_at) FROM balances").fetchone()[0]
    month = fetch.utc_now().strftime("%Y-%m")
    used = db.execute("SELECT cu FROM usage WHERE month=?", (month,)).fetchone()
    return {
        "version": fetch.VERSION,
        "last_sync": last_sync,
        "wallets": [{"address": a, "label": l} for l, a in config["wallets"]],
        "max_wallets": fetch.MAX_WALLETS,
        "chains": [{"slug": slug, "name": name, "symbol": symbol, "fee": fees.get(slug)}
                   for slug, name, symbol in config["chains"]],
        "tokens": tokens,
        "usage": {"used": used[0] if used else 0, "limit": config["monthly_cu"]},
        "has_key": fetch.get_api_key() is not None,
        "refresh": dict(REFRESH),
    }


# --------------------------------------------------------------------------
# Frissítés a háttérben
# --------------------------------------------------------------------------

class LogWriter:
    """A frissítés kiírásait a REFRESH naplóba gyűjti, hogy a felület mutatni tudja."""

    def write(self, text):
        REFRESH["log"] = (REFRESH["log"] + text)[-6000:]
        return len(text)

    def flush(self):
        pass


def refresh_job(config_path, db_path):
    code = 1
    with contextlib.redirect_stdout(LogWriter()):
        try:
            config = fetch.load_config(config_path)
            db = fetch.open_db(db_path)
            api_key = fetch.get_api_key()
            if not api_key:
                print("Hiányzik az ALCHEMY_API_KEY a .env fájlból.")
                code = 2
            else:
                api = fetch.Alchemy(api_key, db, config["monthly_cu"])
                args = argparse.Namespace(chains=None, only_balances=False)
                code = fetch.run_sync(api, db, config, args)
            db.close()
        except (Exception, SystemExit) as error:  # a szerver ne álljon le egy hibától
            print("Hiba a frissítés közben: %s" % error)
    REFRESH.update(running=False, code=code, finished_at=fetch.iso(fetch.utc_now()))


def start_refresh(config_path, db_path):
    with REFRESH_LOCK:
        if REFRESH["running"]:
            return False
        REFRESH.update(running=True, log="", code=None,
                       started_at=fetch.iso(fetch.utc_now()), finished_at=None)
    threading.Thread(target=refresh_job, args=(config_path, db_path), daemon=True).start()
    return True


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    config_path = None
    db_path = None

    def log_message(self, *args):  # ne szemetelje tele a terminált
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            try:
                config = fetch.load_config(self.config_path, require_wallets=False)
            except SystemExit as error:
                return self.send_json({"error": str(error)}, 500)
            db = fetch.open_db(self.db_path)
            try:
                self.send_json(build_state(db, config))
            finally:
                db.close()
        elif path == "/api/refresh":
            self.send_json(dict(REFRESH))
        else:
            self.send_json({"error": "nincs ilyen oldal"}, 404)

    def do_POST(self):
        # Más weboldal ne tudjon a háttérben kéréseket küldeni ennek a szervernek.
        if self.headers.get("X-Tarca") != "1":
            return self.send_json({"error": "hiányzó fejléc"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return self.send_json({"error": "hibás kérés"}, 400)
        path = self.path.split("?")[0]

        if path == "/api/refresh":
            started = start_refresh(self.config_path, self.db_path)
            return self.send_json({"started": started, "refresh": dict(REFRESH)})

        try:
            config = fetch.load_config(self.config_path, require_wallets=False)
        except SystemExit as error:
            return self.send_json({"error": str(error)}, 500)

        if path == "/api/wallets":
            # A címek a config.ini-ben élnek; a felület azt írja át.
            current = list(config["wallets"])
            action = data.get("action")
            address = str(data.get("address") or "").strip()
            label = str(data.get("label") or "").strip()
            if action == "add":
                current.append((label, address))
            elif action == "remove":
                current = [(l, a) for l, a in current if a != address.lower()]
            elif action == "rename":
                current = [(label if a == address.lower() else l, a) for l, a in current]
            else:
                return self.send_json({"error": "ismeretlen művelet"}, 400)
            try:
                fetch.save_wallets(self.config_path, current)
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            config = fetch.load_config(self.config_path, require_wallets=False)
            db = fetch.open_db(self.db_path)
            try:
                return self.send_json(build_state(db, config))
            finally:
                db.close()

        wallets = {address for _, address in config["wallets"]}
        db = fetch.open_db(self.db_path)
        try:
            if path in ("/api/token-flag", "/api/watch"):
                address = str(data.get("address") or "").lower()
                chain, token = str(data.get("chain") or ""), str(data.get("token") or "")
                if address not in wallets or not chain or not token:
                    return self.send_json({"error": "ismeretlen token"}, 400)
                if path == "/api/watch":
                    column, value = "watch", 1 if data.get("on") else 0
                else:
                    spam = data.get("spam")
                    column, value = "spam", None if spam is None else (1 if spam else 0)
                db.execute(
                    "INSERT INTO token_flags (address, chain, token_address, %s) "
                    "VALUES (?,?,?,?) ON CONFLICT(address, chain, token_address) "
                    "DO UPDATE SET %s = excluded.%s" % (column, column, column),
                    (address, chain, token, value))
                db.commit()
                if column == "spam":
                    fetch.update_spam(db, address)
            elif path == "/api/lot-flag":
                acq = data.get("acq")
                row = db.execute("SELECT address FROM transfers WHERE id=? AND direction='in'",
                                 (data.get("id"),)).fetchone()
                if not row or row[0] not in wallets or acq not in (None, "free", "bought"):
                    return self.send_json({"error": "ismeretlen tétel"}, 400)
                if acq is None:
                    db.execute("DELETE FROM lot_flags WHERE transfer_id=?", (data["id"],))
                else:
                    db.execute("INSERT OR REPLACE INTO lot_flags VALUES (?, ?)", (data["id"], acq))
                db.commit()
            else:
                return self.send_json({"error": "nincs ilyen művelet"}, 404)
            self.send_json(build_state(db, config))
        finally:
            db.close()


def make_server(host, port, config_path, db_path):
    handler = type("TarcaHandler", (Handler,), {"config_path": config_path, "db_path": db_path})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Tárcafigyelő – helyi webes felület")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0, ha az otthoni hálózatról (telefonról) is el akarod érni")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default=os.path.join(fetch.HERE, "config.ini"))
    parser.add_argument("--db", default=os.path.join(fetch.HERE, "tarca.sqlite3"))
    args = parser.parse_args(argv)

    fetch.ensure_config(args.config)
    # hibás beállításnál itt álljon meg, érthető üzenettel; cím nélkül is elindul
    fetch.load_config(args.config, require_wallets=False)
    fetch.open_db(args.db).close()
    server = make_server(args.host, args.port, args.config, args.db)
    print("A felület itt érhető el: http://127.0.0.1:%d" % args.port)
    if args.host != "127.0.0.1":
        print("A hálózat többi gépéről: http://<ennek a gépnek az IP-címe>:%d" % args.port)
        print("Figyelem: így a hálózaton bárki megnyithatja, jelszó nincs rajta.")
    print("Leállítás: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLeállt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
