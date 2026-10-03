#!/usr/bin/env python3
"""Tárcafigyelő – adatlekérő (Alchemy + DexScreener → SQLite).

Publikus EVM címek egyenlegeit és mozgásait tölti le, és egy helyi SQLite
adatbázisba menti. Csak olvas: privát kulcsot vagy seed phrase-t soha nem
kér és nem is tud kezelni.

Használat:
    python3 fetch.py --probe           # melyik láncon mi érhető el a kulcsoddal
    python3 fetch.py --only-balances   # csak az aktuális egyenlegek
    python3 fetch.py                   # teljes frissítés (egyenleg, mozgások, árak)
    python3 fetch.py --report          # kiírás az adatbázisból, API-hívás nélkül

Csak a Python szabványos könyvtárát használja, telepíteni nem kell semmit.
"""

import argparse
import configparser
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.6"
HERE = os.path.dirname(os.path.abspath(__file__))

# Becsült költség compute unitban (CU). A pontos fogyasztást az Alchemy
# dashboard mutatja; ez csak arra való, hogy egy hiba ne égesse el a keretet.
CU = {"rpc": 20, "transfers": 120, "portfolio": 400, "prices": 40}

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
LABEL_BAD = re.compile(r"[=\[\]#;\r\n]")
MAX_WALLETS = 5
PORTFOLIO_NETWORKS = 5      # az Alchemy ennyi hálózatot enged címenként egy kérésben
PRICE_WINDOW_DAYS = 300     # napi felbontású ártörténetet legfeljebb ekkora ablakban kérünk
DEX_BATCH = 30              # a DexScreener ennyi címet enged egy kérésben
SWAP_GAS = 180000           # egy átlagos DEX-csere gázigénye a díjbecsléshez
# A szerződésen belüli (internal) natív mozgásokat az Alchemy csak ezeken a láncokon adja.
INTERNAL_OK = {"eth-mainnet", "polygon-mainnet", "base-mainnet"}

# Alchemy hálózatnév → DexScreener láncnév (ott kérünk árat, ahol az Alchemy nem ad).
DEX_CHAIN = {
    "eth-mainnet": "ethereum", "monad-mainnet": "monad", "base-mainnet": "base",
    "arb-mainnet": "arbitrum", "opt-mainnet": "optimism", "polygon-mainnet": "polygon",
    "bnb-mainnet": "bsc", "linea-mainnet": "linea", "avax-mainnet": "avalanche",
    "gnosis-mainnet": "gnosischain", "zksync-mainnet": "zksync", "scroll-mainnet": "scroll",
    "blast-mainnet": "blast", "mantle-mainnet": "mantle", "unichain-mainnet": "unichain",
    "berachain-mainnet": "berachain", "sonic-mainnet": "sonic",
    "robinhood-mainnet": "robinhood",
}

# "Pénz" jellegű tokenek: ha ilyen érkezik, az átutalás, nem ajándék.
STABLES = {"USDC", "USDT", "DAI", "USDC.E", "USDBC", "USDT0", "BUSD", "FDUSD", "USDE",
           "PYUSD", "USDS"}
WRAPPED = {"WETH", "WMON", "WBNB", "WPOL", "WMATIC", "WAVAX", "WXDAI"}

# Link a token nevében vagy jelében: ez szinte mindig csali.
LINK = re.compile(
    r"(https?://|www\.|t\.me/|\.(com|io|org|net|xyz|app|fi|finance|site|top|cc|gg|ly|live)\b)",
    re.IGNORECASE,
)
# Csalogató szavak: önmagukban nem perdöntők, csak ár nélküli tokennél számítanak.
LURE = re.compile(r"\b(claims?|rewards?|voucher|visit|airdrop|bonus|gift)\b|\$\s?\d",
                  re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    label   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS balances (
    address TEXT, chain TEXT, token_address TEXT,
    symbol TEXT, name TEXT, decimals INTEGER,
    amount REAL, usd_price REAL, usd_value REAL,
    native INTEGER,
    source TEXT,             -- 'portfolio', 'rpc' (csak natív egyenleg) vagy 'nft'
    possible_spam INTEGER DEFAULT 0,
    spam_reason TEXT,
    updated_at TEXT,
    PRIMARY KEY (address, chain, token_address)
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT, chain TEXT, tx_hash TEXT, log_key TEXT,
    block_number INTEGER, block_time TEXT,
    token_address TEXT, symbol TEXT,
    direction TEXT,          -- 'in' vagy 'out'
    kind TEXT,               -- 'erc20', 'native' vagy 'call' (általam indított, érték nélküli hívás)
    category TEXT,           -- az Alchemy kategóriája: external, internal, erc20
    amount REAL,
    counterparty TEXT,
    acq TEXT,                -- bejövőnél: 'bought', 'free', 'transfer' vagy 'internal'
    initiated INTEGER,       -- bejövőnél: a tranzakciót én indítottam-e (pl. claim)
    usd_price_then REAL,     -- darabár az érkezéskor
    price_status TEXT,       -- 'pending', 'ok' vagy 'none'
    UNIQUE (address, chain, log_key, direction)
);
CREATE INDEX IF NOT EXISTS idx_transfers_token
    ON transfers (address, chain, token_address, block_number);
CREATE INDEX IF NOT EXISTS idx_transfers_tx
    ON transfers (address, chain, tx_hash);
CREATE TABLE IF NOT EXISTS sync_state (
    address TEXT, chain TEXT,
    last_block INTEGER DEFAULT 0,
    transfers_ok INTEGER,    -- 1: a mozgások lekérhetők, 0: ezen a láncon nem
    note TEXT,
    checked_at TEXT,
    PRIMARY KEY (address, chain)
);
CREATE TABLE IF NOT EXISTS usage (
    month TEXT PRIMARY KEY,
    cu    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS day_prices (
    chain TEXT, token TEXT, day TEXT,
    price REAL,              -- NULL: megnéztük, nincs adat
    PRIMARY KEY (chain, token, day)
);
CREATE TABLE IF NOT EXISTS chain_info (
    chain TEXT PRIMARY KEY,
    gas_price_wei TEXT,
    native_usd REAL,
    fee_usd REAL,            -- egy csere becsült díja dollárban
    updated_at TEXT
);
-- Kézi jelölések a felületről. Ezeket a frissítés soha nem írja felül.
CREATE TABLE IF NOT EXISTS token_flags (
    address TEXT, chain TEXT, token_address TEXT,
    spam INTEGER,            -- 1: scam, 0: nem scam, NULL: a program döntsön
    watch INTEGER,           -- 1: figyelő bekapcsolva
    PRIMARY KEY (address, chain, token_address)
);
CREATE TABLE IF NOT EXISTS lot_flags (
    transfer_id INTEGER PRIMARY KEY,
    acq TEXT                 -- 'free' vagy 'bought': az érkezés kézi besorolása
);
"""

# Későbbi változatokban hozzáadott oszlopok (a meglévő adatbázis megmarad).
NEW_COLUMNS = [
    ("balances", "price_source", "TEXT"),     # 'alchemy' vagy 'dex'
    ("balances", "liquidity_usd", "REAL"),    # a DEX-pár likviditása, ha onnan jön az ár
    ("balances", "pool", "TEXT"),             # a DEX-pár címe
    ("transfers", "price_basis", "TEXT"),     # 'paid' (amit fizettem) vagy 'market' (napi ár)
]


class BudgetExceeded(Exception):
    """A havi CU-keret elfogyott; a futás később folytatható."""


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__("HTTP %s: %s" % (status, str(body)[:200]))

    @property
    def fatal(self):
        """Hibás kulcs: nincs értelme tovább próbálkozni."""
        return self.status == 401

    @property
    def not_enabled(self):
        """403: a kulcs jó, de ez a hálózat nincs bekapcsolva az Alchemy appban."""
        return self.status == 403


# --------------------------------------------------------------------------
# Segédfüggvények
# --------------------------------------------------------------------------

def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value):
    """Számmá alakít; ha nem megy, None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def hex_int(value):
    """'0x1a' vagy '26' → 26; ha nem megy, None."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def short(address):
    return address[:6] + "…" + address[-4:]


def fmt_num(value, decimals=2):
    text = "{:,.{d}f}".format(value, d=decimals)
    return text.replace(",", " ").replace(".", ",")


def fmt_usd(value):
    if value is None:
        return "n.a."
    if 0 < abs(value) < 0.01:
        return "< 0,01 $"
    return fmt_num(value) + " $"


def fmt_change(value):
    if value is None:
        return "n.a."
    if abs(value) < 0.005:
        return "0,00 $"
    return ("+" if value > 0 else "-") + fmt_num(abs(value)) + " $"


def fmt_qty(value):
    if value >= 1000:
        return fmt_num(value, 2)
    if value >= 1:
        return fmt_num(value, 4)
    return ("%.8f" % value).rstrip("0").rstrip(".").replace(".", ",") or "0"


def add_days(day, days):
    """'2024-01-31' + 1 → '2024-02-01'."""
    date = dt.datetime.strptime(day, "%Y-%m-%d") + dt.timedelta(days=days)
    return date.strftime("%Y-%m-%d")


def is_money(token, symbol):
    """A lánc saját coinja, stabilcoin vagy wrapped coin: ennek érkezése átutalás, nem ajándék."""
    return token == "native" or (symbol or "").upper() in STABLES | WRAPPED


# --------------------------------------------------------------------------
# Beállítások
# --------------------------------------------------------------------------

def load_env(path):
    """Egyszerű KULCS=érték olvasó a .env fájlhoz."""
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def ensure_config(path):
    """Első indításkor a config.example.ini másolatából hozza létre a config.ini-t."""
    example = os.path.join(os.path.dirname(os.path.abspath(path)), "config.example.ini")
    if not os.path.exists(path) and os.path.exists(example):
        shutil.copyfile(example, path)


def load_config(path, require_wallets=True):
    if not os.path.exists(path):
        raise SystemExit("Nem találom a beállításfájlt: %s" % path)
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str  # a címkék kis- és nagybetűi maradjanak meg
    parser.read(path, encoding="utf-8")

    wallets = []
    for label, address in (parser.items("wallets") if parser.has_section("wallets") else []):
        address = address.strip()
        if not ADDRESS_RE.match(address):
            raise SystemExit("Hibás cím a config.ini-ben (%s): %s" % (label, address))
        wallets.append((label, address.lower()))
    if not wallets and require_wallets:
        raise SystemExit("Még nincs figyelt cím. Add meg a felületen (python app.py, Címek gomb) "
                         "vagy a config.ini [wallets] részében.")
    if len(wallets) > MAX_WALLETS:
        raise SystemExit("Legfeljebb %d cím adható meg." % MAX_WALLETS)

    chains = []
    for slug, value in parser.items("chains"):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 2 or not all(parts):
            raise SystemExit("Hibás sor a [chains] részben: %s = %s "
                             "(elvárt: hálózat = Név, JEL)" % (slug, value))
        chains.append((slug, parts[0], parts[1]))
    if not chains:
        raise SystemExit("A config.ini [chains] részében nincs lánc megadva.")

    wrapped = {}
    if parser.has_section("wrapped_native"):
        wrapped = {k: v.strip().lower() for k, v in parser.items("wrapped_native")}

    return {
        "wallets": wallets,
        "chains": chains,
        "wrapped": wrapped,
        "monthly_cu": parser.getint("budget", "monthly_cu", fallback=20000000),
        "min_liquidity": parser.getfloat("prices", "min_liquidity_usd", fallback=500.0),
    }


def check_wallets(wallets):
    """[(név, cím), …] ellenőrzése. Hibánál ValueError, a felületen megjeleníthető szöveggel."""
    if not wallets:
        raise ValueError("Legalább egy címnek maradnia kell.")
    if len(wallets) > MAX_WALLETS:
        raise ValueError("Legfeljebb %d cím adható meg." % MAX_WALLETS)
    cleaned, labels, addresses = [], set(), set()
    for label, address in wallets:
        label, address = (label or "").strip(), (address or "").strip()
        if PRIVATE_KEY_RE.match(address) or len(address.split()) >= 12:
            raise ValueError("Ez privát kulcsnak vagy seed phrase-nek tűnik, nem címnek. "
                             "Ilyet soha ne írj be sehova; ide csak a publikus cím kell.")
        if not ADDRESS_RE.match(address):
            raise ValueError("Ez nem érvényes cím. A cím 0x-szel kezdődik, és utána 40 "
                             "karakter (szám vagy a–f betű) áll.")
        if not label or len(label) > 30 or LABEL_BAD.search(label):
            raise ValueError("A név 1–30 karakter legyen, és ne legyen benne = [ ] # ; jel.")
        if label.lower() in labels:
            raise ValueError("Ez a név már foglalt: %s" % label)
        if address.lower() in addresses:
            raise ValueError("Ez a cím már szerepel a listán.")
        labels.add(label.lower())
        addresses.add(address.lower())
        cleaned.append((label, address))
    return cleaned


def save_wallets(path, wallets):
    """A config.ini [wallets] részét cseréli le; a fájl többi része (megjegyzésekkel) marad."""
    wallets = check_wallets(wallets)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    body = ["# név = publikus cím   (legfeljebb %d sor; a felületen is szerkeszthető)" % MAX_WALLETS]
    body += ["%s = %s" % (label, address) for label, address in wallets]
    body.append("")
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == "[wallets]"), None)
    if start is None:
        lines = ["[wallets]"] + body + lines
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
                   len(lines))
        lines = lines[:start + 1] + body + lines[end:]
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, path)
    return wallets


# --------------------------------------------------------------------------
# Alchemy kliens
# --------------------------------------------------------------------------

class Alchemy:
    def __init__(self, api_key, db, monthly_cu, pause=0.1):
        self.api_key = api_key
        self.db = db
        self.monthly_cu = monthly_cu
        self.pause = pause

    def safe(self, text):
        """Az API kulcs az URL része, ezért semmilyen kiírásba nem kerülhet bele."""
        return str(text).replace(self.api_key, "***") if self.api_key else str(text)

    # A próbák ezt a metódust cserélik le, hogy ne kelljen hálózat.
    def _http(self, method, url, body=None):
        headers = {"Accept": "application/json", "User-Agent": "tarcafigyelo/" + VERSION}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                text = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as error:
            return error.code, self.safe(error.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as error:
            return 599, self.safe(error.reason)
        try:
            return status, (json.loads(text) if text else None)
        except ValueError:
            return status, self.safe(text)

    def used_this_month(self):
        month = utc_now().strftime("%Y-%m")
        row = self.db.execute("SELECT cu FROM usage WHERE month=?", (month,)).fetchone()
        return row[0] if row else 0

    def _add_usage(self, cost):
        month = utc_now().strftime("%Y-%m")
        self.db.execute(
            "INSERT INTO usage (month, cu) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET cu = cu + excluded.cu",
            (month, cost),
        )
        self.db.commit()

    def _call(self, kind, method, url, body=None):
        cost = CU[kind]
        if self.used_this_month() + cost > self.monthly_cu:
            raise BudgetExceeded()
        status, data = 599, None
        for attempt in range(4):
            status, data = self._http(method, url, body)
            if status == 429 or status >= 500:
                time.sleep(1.5 * (2 ** attempt))
                continue
            break
        if status < 500:
            self._add_usage(cost)
        if status >= 400:
            raise ApiError(status, data)
        if self.pause:
            time.sleep(self.pause)
        return data

    def rpc(self, network, method, params, kind="rpc"):
        url = "https://%s.g.alchemy.com/v2/%s" % (network, self.api_key)
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = self._call(kind, "POST", url, body)
        if not isinstance(data, dict):
            raise ApiError(400, data)
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise ApiError(400, message)
        return data.get("result")

    def tokens_by_address(self, body):
        url = "https://api.g.alchemy.com/data/v1/%s/assets/tokens/by-address" % self.api_key
        return self._call("portfolio", "POST", url, body)

    def price_by_symbol(self, symbol):
        url = "https://api.g.alchemy.com/prices/v1/%s/tokens/by-symbol?%s" % (
            self.api_key, urllib.parse.urlencode({"symbols": symbol}))
        return self._call("prices", "GET", url)

    def price_history(self, body):
        url = "https://api.g.alchemy.com/prices/v1/%s/tokens/historical" % self.api_key
        return self._call("prices", "POST", url, body)


def usd_from_prices(prices):
    for price in prices or []:
        if isinstance(price, dict) and (price.get("currency") or "usd").lower() == "usd":
            return num(price.get("value"))
    return None


# --------------------------------------------------------------------------
# DexScreener: mostani ár ott, ahol az Alchemy nem ad (kulcs nem kell hozzá)
# --------------------------------------------------------------------------

def dex_fetch(chain_id, addresses):
    """A DexScreener párjai a megadott tokenekhez. A próbák ezt a függvényt cserélik le."""
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (chain_id, ",".join(addresses))
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "tarcafigyelo/" + VERSION})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def dex_prices(chain, addresses, min_liquidity):
    """{cím: (ár, likviditás, pár címe)} a legnagyobb likviditású párból.

    A túl kis likviditású párokat kihagyjuk: scam tokenhez bárki tud hamis árat csinálni.
    """
    chain_id = DEX_CHAIN.get(chain)
    wanted = {a.lower() for a in addresses}
    found = {}
    if not chain_id or not wanted:
        return found
    for group in chunks(sorted(wanted), DEX_BATCH):
        try:
            pairs = dex_fetch(chain_id, group)
        except (urllib.error.URLError, ValueError, OSError):
            continue
        for pair in pairs if isinstance(pairs, list) else []:
            if not isinstance(pair, dict):
                continue
            price = num(pair.get("priceUsd"))
            liquidity = num((pair.get("liquidity") or {}).get("usd")) or 0.0
            if price is None or liquidity < min_liquidity:
                continue
            base = ((pair.get("baseToken") or {}).get("address") or "").lower()
            quote = ((pair.get("quoteToken") or {}).get("address") or "").lower()
            candidates = [(base, price)]
            native_ratio = num(pair.get("priceNative"))
            if native_ratio:
                candidates.append((quote, price / native_ratio))  # a pár másik oldalának ára
            for address, value in candidates:
                if address in wanted and (address not in found or liquidity > found[address][1]):
                    found[address] = (value, liquidity, pair.get("pairAddress"))
        time.sleep(0.25)
    return found


# --------------------------------------------------------------------------
# Egyenlegek
# --------------------------------------------------------------------------

def fetch_portfolio(api, address, networks):
    """Egy cím tokenjei több hálózaton. Visszaad: ({hálózat: [tételek]}, [sikertelen hálózatok])."""

    def one(group):
        tokens, page = [], None
        while True:
            body = {
                "addresses": [{"address": address, "networks": group}],
                "withMetadata": True, "withPrices": True,
                "includeNativeTokens": True, "includeErc20Tokens": True,
            }
            if page:
                body["pageKey"] = page
            data = api.tokens_by_address(body)
            inner = (data or {}).get("data") or {}
            tokens.extend(inner.get("tokens") or [])
            page = inner.get("pageKey")
            if not page:
                return tokens

    result = {network: [] for network in networks}
    failed = []
    for group in chunks(networks, PORTFOLIO_NETWORKS):
        try:
            tokens = one(group)
        except ApiError as error:
            if error.fatal:
                raise
            # Ha a csoportban van egy nem támogatott hálózat, egyenként próbáljuk újra.
            tokens = []
            for network in group:
                if len(group) == 1:
                    failed.append(network)
                    break
                try:
                    tokens.extend(one([network]))
                except ApiError as inner_error:
                    if inner_error.fatal:
                        raise
                    failed.append(network)
        for token in tokens:
            network = token.get("network")
            if network in result:
                result[network].append(token)
    for network in failed:
        result.pop(network, None)
    return result, failed


def token_meta(api, network, token_address):
    try:
        return api.rpc(network, "alchemy_getTokenMetadata", [token_address]) or {}
    except ApiError as error:
        if error.fatal:
            raise
        return {}


def sync_balances(api, db, address, chains, config):
    """Egy cím aktuális egyenlegei az összes megadott láncon. Visszaad: {hálózat: tokenek száma}."""
    slugs = [slug for slug, _, _ in chains]
    symbols = {slug: symbol for slug, _, symbol in chains}
    wrapped = config["wrapped"]
    portfolio, _failed = fetch_portfolio(api, address, slugs)
    now = iso(utc_now())
    counts = {}

    for slug in slugs:
        rows, native_seen = [], False
        for entry in portfolio.get(slug, []):
            if entry.get("error"):
                continue
            native = not entry.get("tokenAddress")
            native_seen = native_seen or native
            raw = hex_int(entry.get("tokenBalance"))
            if not raw:
                continue
            meta = entry.get("tokenMetadata") or {}
            row = {
                "token_address": "native" if native else entry["tokenAddress"].lower(),
                "symbol": meta.get("symbol") or (symbols[slug] if native else None),
                "name": meta.get("name"), "native": int(native), "source": "portfolio",
                "usd_price": usd_from_prices(entry.get("tokenPrices")),
                "price_source": None, "liquidity_usd": None, "pool": None,
            }
            decimals = meta.get("decimals")
            if decimals is None and native:
                decimals = 18
            if decimals is None:
                extra = token_meta(api, slug, row["token_address"])
                decimals = extra.get("decimals")
                row["symbol"] = row["symbol"] or extra.get("symbol")
                row["name"] = row["name"] or extra.get("name")
            if decimals is None:
                # Nincs tizedesjegye: ez nem ERC-20 token, hanem NFT-kollekció (darabszám).
                row.update(source="nft", decimals=0, amount=float(raw), usd_price=None)
            else:
                row.update(decimals=int(decimals), amount=raw / (10 ** int(decimals)))
            if row["usd_price"] is not None:
                row["price_source"] = "alchemy"
            rows.append(row)

        if not native_seen:
            # A Portfolio API ezen a láncon nem adott natív egyenleget: közvetlenül a
            # csomóponttól kérdezzük meg, hogy a coin ne tűnjön el.
            try:
                raw = hex_int(api.rpc(slug, "eth_getBalance", [address, "latest"]))
            except ApiError as error:
                if error.fatal:
                    raise
                raw = None
            if raw:
                rows.append({
                    "token_address": "native", "symbol": symbols[slug], "name": None,
                    "native": 1, "source": "rpc", "decimals": 18, "amount": raw / 10 ** 18,
                    "usd_price": None, "price_source": None, "liquidity_usd": None, "pool": None,
                })

        # Ahol az Alchemy nem adott árat, ott a DexScreenertől kérünk.
        lookup = {}
        for row in rows:
            if row["usd_price"] is None and row["source"] != "nft":
                target = wrapped.get(slug) if row["native"] else row["token_address"]
                if target:
                    lookup.setdefault(target, []).append(row)
        for target, (price, liquidity, pool) in dex_prices(
                slug, list(lookup), config["min_liquidity"]).items():
            for row in lookup[target]:
                row.update(usd_price=price, price_source="dex", liquidity_usd=liquidity, pool=pool)

        # A lánc saját coinjához végső esetben a jel alapján kérünk árat az Alchemytől.
        for row in rows:
            if row["native"] and row["usd_price"] is None:
                try:
                    data = api.price_by_symbol(symbols[slug])
                    price = usd_from_prices(((data or {}).get("data") or [{}])[0].get("prices"))
                except ApiError as error:
                    if error.fatal:
                        raise
                    price = None
                if price is not None:
                    row.update(usd_price=price, price_source="alchemy")

        db.execute("DELETE FROM balances WHERE address=? AND chain=?", (address, slug))
        for row in rows:
            value = None if row["usd_price"] is None else row["amount"] * row["usd_price"]
            db.execute(
                "INSERT OR REPLACE INTO balances "
                "(address, chain, token_address, symbol, name, decimals, amount, usd_price, "
                " usd_value, native, source, possible_spam, spam_reason, updated_at, "
                " price_source, liquidity_usd, pool) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?)",
                (address, slug, row["token_address"], row["symbol"], row["name"],
                 row["decimals"], row["amount"], row["usd_price"], value, row["native"],
                 row["source"], now, row["price_source"], row["liquidity_usd"], row["pool"]),
            )
        counts[slug] = len([r for r in rows if r["source"] != "nft"])
    db.commit()
    return counts


def fill_symbols(db, address):
    """Ahol az egyenleghez nem jött jel, ott a mozgásokban látott jelet használjuk."""
    db.execute(
        "UPDATE balances SET symbol = ("
        "  SELECT t.symbol FROM transfers t WHERE t.address = balances.address "
        "  AND t.chain = balances.chain AND t.token_address = balances.token_address "
        "  AND t.symbol IS NOT NULL AND t.symbol != '' LIMIT 1) "
        "WHERE address=? AND (symbol IS NULL OR symbol = '')",
        (address,),
    )
    db.commit()


# --------------------------------------------------------------------------
# Mozgások
# --------------------------------------------------------------------------

def store_transfer(db, address, chain, item, side):
    """Egy Alchemy-transfert ment. side: 'to' (bejövő) vagy 'from' (kimenő). 1, ha új sor lett."""
    frm = (item.get("from") or "").lower()
    to = (item.get("to") or "").lower()
    if frm == to:
        return 0
    category = item.get("category")
    raw = item.get("rawContract") or {}
    amount = num(item.get("value"))

    if category == "erc20":
        token = (raw.get("address") or "").lower()
        if not token:
            return 0
        if amount is None:
            value, decimals = hex_int(raw.get("value")), hex_int(raw.get("decimal"))
            if value is None:
                return 0
            amount = value / (10 ** (decimals if decimals is not None else 18))
        if not amount:
            return 0  # nulla értékű ("address poisoning") átutalásokat nem tárolunk
        kind = "erc20"
    elif category in ("external", "internal"):
        token = "native"
        amount = amount or 0.0
        if amount:
            kind = "native"
        elif side == "from" and category == "external":
            kind = "call"  # érték nélküli hívás: ebből tudjuk, hogy a tranzakciót én indítottam
        else:
            return 0
    else:
        return 0  # NFT-mozgásokat ez a változat nem kezel

    direction = "in" if side == "to" else "out"
    key = item.get("uniqueId") or "%s:%s:%s" % (item.get("hash"), category, token)
    cursor = db.execute(
        "INSERT OR IGNORE INTO transfers "
        "(address, chain, tx_hash, log_key, block_number, block_time, token_address, symbol, "
        " direction, kind, category, amount, counterparty, price_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            address, chain, item.get("hash"), key, hex_int(item.get("blockNum")) or 0,
            (item.get("metadata") or {}).get("blockTimestamp"),
            token, item.get("asset"), direction, kind, category, amount,
            frm if direction == "in" else to,
            "pending" if direction == "in" else None,
        ),
    )
    return cursor.rowcount


def classify(db, address, chain, other_wallets):
    """A bejövő mozgások besorolása. Minden futásnál újraszámoljuk, olcsó.

    internal: a feladó egy másik saját cím
    bought:   ugyanabban a tranzakcióban ki is ment valami (vétel, csere)
    transfer: coin vagy stabilcoin érkezett úgy, hogy semmi nem ment ki (átutalás)
    free:     más token érkezett úgy, hogy semmi nem ment ki (airdrop, claim, ajándék)
    """
    per_tx = {}
    for tx_hash, paid, mine in db.execute(
        "SELECT tx_hash, "
        " SUM(CASE WHEN direction='out' AND amount > 0 THEN 1 ELSE 0 END), "
        " SUM(CASE WHEN direction='out' AND category='external' THEN 1 ELSE 0 END) "
        "FROM transfers WHERE address=? AND chain=? GROUP BY tx_hash",
        (address, chain),
    ):
        per_tx[tx_hash] = (paid or 0, mine or 0)

    rows = db.execute(
        "SELECT id, tx_hash, counterparty, token_address, symbol FROM transfers "
        "WHERE address=? AND chain=? AND direction='in'",
        (address, chain),
    ).fetchall()
    for row_id, tx_hash, counterparty, token, symbol in rows:
        paid, mine = per_tx.get(tx_hash, (0, 0))
        if counterparty in other_wallets:
            acq = "internal"
        elif paid:
            acq = "bought"
        elif is_money(token, symbol):
            acq = "transfer"
        else:
            acq = "free"
        db.execute("UPDATE transfers SET acq=?, initiated=? WHERE id=?",
                   (acq, 1 if mine else 0, row_id))
    db.commit()


def fill_block_times(api, db, address, chain):
    """Ahol a mozgáshoz nem jött időpont, ott a blokk időbélyegét kérdezzük le."""
    blocks = [row[0] for row in db.execute(
        "SELECT DISTINCT block_number FROM transfers WHERE address=? AND chain=? "
        "AND (block_time IS NULL OR block_time = '') AND block_number > 0 LIMIT 200",
        (address, chain))]
    for block in blocks:
        try:
            data = api.rpc(chain, "eth_getBlockByNumber", [hex(block), False]) or {}
        except ApiError as error:
            if error.fatal:
                raise
            continue
        stamp = hex_int(data.get("timestamp"))
        if stamp is None:
            continue
        text = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        db.execute(
            "UPDATE transfers SET block_time=?, price_status = CASE "
            "  WHEN direction='in' AND price_status='none' THEN 'pending' ELSE price_status END "
            "WHERE address=? AND chain=? AND block_number=?",
            (text, address, chain, block))
    db.commit()


def sync_transfers(api, db, address, chain, other_wallets):
    """A cím mozgásai az utoljára látott blokktól a lánc mostani végéig. Új sorok száma."""
    row = db.execute(
        "SELECT last_block FROM sync_state WHERE address=? AND chain=?", (address, chain)
    ).fetchone()
    start = (row[0] + 1) if row and row[0] else 0

    def remember(ok, note, last_block=None):
        db.execute(
            "INSERT INTO sync_state (address, chain, last_block, transfers_ok, note, checked_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(address, chain) DO UPDATE SET "
            "last_block = COALESCE(excluded.last_block, last_block), "
            "transfers_ok = excluded.transfers_ok, note = excluded.note, "
            "checked_at = excluded.checked_at",
            (address, chain, last_block, ok, note, iso(utc_now())),
        )
        db.commit()

    try:
        head = hex_int(api.rpc(chain, "eth_blockNumber", []))
    except ApiError as error:
        if error.fatal:
            raise
        remember(0, api.safe(error))
        return None

    added = 0
    if head is not None and start <= head:
        try:
            for side in ("to", "from"):
                categories = ["external", "erc20"]
                if chain in INTERNAL_OK:
                    categories.append("internal")
                page = None
                while True:
                    params = {
                        "fromBlock": hex(start), "toBlock": hex(head),
                        side + "Address": address,
                        "category": categories,
                        "withMetadata": True,
                        # kimenő oldalon a nulla értékű hívások is kellenek (ki indította)
                        "excludeZeroValue": side == "to",
                        "maxCount": "0x3e8", "order": "asc",
                    }
                    if page:
                        params["pageKey"] = page
                    result = api.rpc(chain, "alchemy_getAssetTransfers", [params],
                                     kind="transfers") or {}
                    for item in result.get("transfers") or []:
                        added += store_transfer(db, address, chain, item, side)
                    db.commit()
                    page = result.get("pageKey")
                    if not page:
                        break
        except ApiError as error:
            if error.fatal:
                raise
            remember(0, api.safe(error))
            return None
        remember(1, None, head)

    fill_block_times(api, db, address, chain)
    classify(db, address, chain, other_wallets)
    return added


# --------------------------------------------------------------------------
# Scam-gyanú saját szabályokkal (az Alchemy tokenekre nem ad ilyen jelzést)
# --------------------------------------------------------------------------

def spam_reasons(name, symbol, price, incoming):
    """incoming: a token bejövő mozgásai [(acq, initiated), …]."""
    if any(acq == "bought" for acq, _ in incoming):
        return []  # amiért fizettem, azt nem tekintjük scamnek
    reasons = []
    text = "%s %s" % (name or "", symbol or "")
    if LINK.search(text):
        reasons.append("link a névben")
    elif price is None and LURE.search(text):
        reasons.append("csalogató név, nincs ára")
    if price is None and incoming and all(
            acq in ("free", "transfer") and not init for acq, init in incoming):
        reasons.append("nincs ára, kéretlenül érkezett")
    return reasons


def update_spam(db, address):
    tokens = db.execute(
        "SELECT chain, token_address, name, symbol, usd_price FROM balances "
        "WHERE address=? AND native=0 AND source != 'nft'", (address,)
    ).fetchall()
    for chain, token, name, symbol, price in tokens:
        incoming = db.execute(
            "SELECT acq, initiated FROM transfers "
            "WHERE address=? AND chain=? AND token_address=? AND direction='in'",
            (address, chain, token),
        ).fetchall()
        reasons = spam_reasons(name, symbol, price, incoming)
        flag = db.execute(
            "SELECT spam FROM token_flags WHERE address=? AND chain=? AND token_address=?",
            (address, chain, token)).fetchone()
        if flag and flag[0] is not None:
            reasons = ["kézzel megjelölve"] if flag[0] else []   # a kézi jelölés az erősebb
        db.execute(
            "UPDATE balances SET possible_spam=?, spam_reason=? "
            "WHERE address=? AND chain=? AND token_address=?",
            (1 if reasons else 0, "; ".join(reasons) or None, address, chain, token),
        )
    db.commit()


# --------------------------------------------------------------------------
# Érkezéskori árak
# --------------------------------------------------------------------------

def day_windows(days, span):
    """Rendezett napokat legfeljebb `span` nap széles ablakokra bont."""
    window = []
    for day in days:
        if window and day > add_days(window[0], span):
            yield window
            window = []
        window.append(day)
    if window:
        yield window


class PriceBook:
    """Napi árfolyamok az Alchemytől, a day_prices táblában megjegyezve."""

    def __init__(self, api, db, chains, wrapped):
        self.api = api
        self.db = db
        self.symbols = {slug: symbol for slug, _, symbol in chains}
        self.wrapped = wrapped
        self.no_address_history = set()   # láncok, ahol cím alapján nincs ártörténet
        self.symbol_now = {}              # jel → mostani ár a jel alapján
        self.symbol_trusted = {}          # lánc → használható-e a jel alapú ár
        self.notes = []

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def symbol_ok(self, chain):
        """A jel (pl. MON) több tokent is jelenthet: csak akkor használjuk, ha a jel alapján
        kapott mostani ár egyezik a lánc coinjának ismert árával."""
        if chain in self.symbol_trusted:
            return self.symbol_trusted[chain]
        symbol = self.symbols[chain]
        if symbol not in self.symbol_now:
            try:
                data = self.api.price_by_symbol(symbol)
                self.symbol_now[symbol] = usd_from_prices(
                    ((data or {}).get("data") or [{}])[0].get("prices"))
            except ApiError as error:
                if error.fatal:
                    raise
                self.symbol_now[symbol] = None
        price = self.symbol_now[symbol]
        ok = price is not None
        if not ok:
            self.note("%s: a jel alapján nincs ár az Alchemynél" % symbol)
        else:
            reference = self.db.execute(
                "SELECT usd_price FROM balances WHERE chain=? AND native=1 "
                "AND usd_price IS NOT NULL LIMIT 1", (chain,)).fetchone()
            if reference and reference[0] > 0 and not 0.7 <= price / reference[0] <= 1.43:
                ok = False
                self.note("%s: a jel alapján kapott ár (%s) nem egyezik a coin árával (%s), "
                          "ezért nem használom" % (symbol, fmt_usd(price), fmt_usd(reference[0])))
        self.symbol_trusted[chain] = ok
        return ok

    def targets(self, chain, token):
        if token == "native":
            if self.wrapped.get(chain) and chain not in self.no_address_history:
                yield {"network": chain, "address": self.wrapped[chain]}
            if self.symbol_ok(chain):
                yield {"symbol": self.symbols[chain]}
        elif chain not in self.no_address_history:
            yield {"network": chain, "address": token}

    def ensure(self, chain, token, days):
        """Gondoskodik róla, hogy a megadott napokra legyen bejegyzés (ár vagy 'nincs adat')."""
        have = {row[0] for row in self.db.execute(
            "SELECT day FROM day_prices WHERE chain=? AND token=?", (chain, token))}
        missing = sorted(set(days) - have)
        for window in day_windows(missing, PRICE_WINDOW_DAYS):
            prices = {}
            for target in self.targets(chain, token):
                body = dict(target)
                body.update({
                    "startTime": add_days(window[0], -1) + "T00:00:00Z",
                    "endTime": add_days(window[-1], 2) + "T00:00:00Z",
                    "interval": "1d",
                })
                try:
                    data = self.api.price_history(body)
                except ApiError as error:
                    if error.fatal:
                        raise
                    text = self.api.safe(error)
                    if "address" in target and "nsupported network" in text:
                        self.no_address_history.add(chain)
                    self.note(text)
                    continue
                for point in (data or {}).get("data") or []:
                    value = num(point.get("value"))
                    stamp = str(point.get("timestamp") or "")[:10]
                    if value is not None and stamp:
                        prices[stamp] = value
                if prices:
                    break
            for day in window:
                price = prices.get(day)
                if price is None:
                    price = prices.get(add_days(day, -1), prices.get(add_days(day, 1)))
                self.db.execute("INSERT OR REPLACE INTO day_prices VALUES (?,?,?,?)",
                                (chain, token, day, price))
            self.db.commit()

    def get(self, chain, token, day):
        row = self.db.execute(
            "SELECT price FROM day_prices WHERE chain=? AND token=? AND day=?",
            (chain, token, day)).fetchone()
        return row[0] if row else None


def tx_trade(db, address, chain, tx_hash):
    """Egy tranzakció nettó mérlege: (bejött {token: mennyiség}, kiment {…}, {token: jel})."""
    net, symbols = {}, {}
    for direction, token, amount, symbol in db.execute(
        "SELECT direction, token_address, amount, symbol FROM transfers "
        "WHERE address=? AND chain=? AND tx_hash=? AND kind != 'call'",
        (address, chain, tx_hash),
    ):
        net[token] = net.get(token, 0.0) + (amount if direction == "in" else -amount)
        symbols[token] = symbol
    came = {t: v for t, v in net.items() if v > 1e-12}
    went = {t: -v for t, v in net.items() if v < -1e-12}
    return came, went, symbols


def sync_prices(api, db, chains, wrapped):
    """Érkezéskori darabár a még ár nélküli érkezésekhez (a meglévő, nem scam tokeneknél).

    Vételnél abból számolunk, amit a tranzakcióban ténylegesen fizettem; ahol ez nem megy,
    ott az érkezés napjának piaci árából. Visszaad: (megvan, nincs, megjegyzések).
    """
    book = PriceBook(api, db, chains, wrapped)
    lots = db.execute(
        "SELECT t.id, t.address, t.chain, t.tx_hash, t.token_address, t.acq, "
        "       substr(t.block_time, 1, 10) "
        "FROM transfers t JOIN balances b "
        "  ON b.address = t.address AND b.chain = t.chain AND b.token_address = t.token_address "
        "WHERE t.direction='in' AND t.price_status='pending' "
        "  AND b.possible_spam = 0 AND b.source != 'nft'"
    ).fetchall()
    lots = [lot for lot in lots if lot[2] in book.symbols]

    # 1. Vételek: mit fizettem érte?
    trades = {}
    paid_needs = {}
    for row_id, address, chain, tx_hash, token, acq, day in lots:
        if acq != "bought" or not day:
            continue
        came, went, symbols = tx_trade(db, address, chain, tx_hash)
        if list(came) != [token] or not went:
            continue  # több féle token jött, vagy nem egyértelmű: marad a piaci ár
        trades[row_id] = (came[token], went, symbols)
        for out_token in went:
            if (symbols.get(out_token) or "").upper() not in STABLES:
                paid_needs.setdefault((chain, out_token), set()).add(day)
    for (chain, token), days in sorted(paid_needs.items()):
        book.ensure(chain, token, days)

    resolved = {}
    for row_id, address, chain, tx_hash, token, acq, day in lots:
        if row_id not in trades:
            continue
        amount_in, went, symbols = trades[row_id]
        cost = 0.0
        for out_token, amount in went.items():
            price = book.get(chain, out_token, day)
            if price is None and (symbols.get(out_token) or "").upper() in STABLES:
                price = 1.0
            if price is None:
                cost = None
                break
            cost += amount * price
        if cost is not None and amount_in > 0:
            resolved[row_id] = (cost / amount_in, "paid")

    # 2. Minden más: az érkezés napjának piaci ára.
    market_needs = {}
    for row_id, address, chain, tx_hash, token, acq, day in lots:
        if row_id not in resolved and day:
            market_needs.setdefault((chain, token), set()).add(day)
    for (chain, token), days in sorted(market_needs.items()):
        book.ensure(chain, token, days)

    found = missing = 0
    recent = add_days(utc_now().strftime("%Y-%m-%d"), -2)
    for row_id, address, chain, tx_hash, token, acq, day in lots:
        price, basis = resolved.get(row_id, (None, None))
        if price is None and day:
            price, basis = book.get(chain, token, day), "market"
        if price is None and day and day >= recent:
            # Az elmúlt napokra még nincs napi árfolyam: a mostani árral számolunk.
            now = db.execute(
                "SELECT usd_price FROM balances WHERE address=? AND chain=? AND token_address=?",
                (address, chain, token)).fetchone()
            price, basis = (now[0] if now else None), "market"
        if price is None:
            basis = None
            missing += 1
            book.note("nincs érkezéskori ár: %s, %s, %s" % (
                chain, "coin" if token == "native" else short(token),
                day or "az időpont ismeretlen"))
        else:
            found += 1
        db.execute(
            "UPDATE transfers SET usd_price_then=?, price_status=?, price_basis=? WHERE id=?",
            (price, "ok" if price is not None else "none", basis, row_id))
    db.commit()
    return found, missing, book.notes


def sync_fees(api, db, chains, config):
    """Egy csere becsült díja láncónként: gázár × átlagos gázigény × a coin ára."""
    for slug, _name, symbol in chains:
        try:
            gas_price = hex_int(api.rpc(slug, "eth_gasPrice", []))
        except ApiError as error:
            if error.fatal:
                raise
            continue
        if gas_price is None:
            continue
        row = db.execute(
            "SELECT usd_price FROM balances WHERE chain=? AND native=1 "
            "AND usd_price IS NOT NULL LIMIT 1", (slug,)).fetchone()
        native_usd = row[0] if row else None
        wrapped = config["wrapped"].get(slug)
        if native_usd is None and wrapped:
            found = dex_prices(slug, [wrapped], config["min_liquidity"])
            native_usd = found[wrapped][0] if wrapped in found else None
        if native_usd is None:
            try:
                data = api.price_by_symbol(symbol)
                native_usd = usd_from_prices(((data or {}).get("data") or [{}])[0].get("prices"))
            except ApiError as error:
                if error.fatal:
                    raise
        fee = None if native_usd is None else gas_price * SWAP_GAS / 1e18 * native_usd
        db.execute("INSERT OR REPLACE INTO chain_info VALUES (?,?,?,?,?)",
                   (slug, str(gas_price), native_usd, fee, iso(utc_now())))
    db.commit()


# --------------------------------------------------------------------------
# Futtatás
# --------------------------------------------------------------------------

def select_chains(config, args):
    chains = config["chains"]
    if args.chains:
        wanted = {c.strip() for c in args.chains.split(",") if c.strip()}
        chains = [c for c in chains if c[0] in wanted]
        if not chains:
            raise SystemExit("A megadott láncok egyike sem szerepel a config.ini-ben.")
    return chains


def print_network_hint(names):
    print("\nEzek a láncok nincsenek bekapcsolva az Alchemy appodban: %s" % ", ".join(names))
    print("Az Alchemy dashboardon nyisd meg az appot, a Networks fülön kapcsold be őket, "
          "majd futtasd újra.")


def run_probe(api, config, args):
    """Kipróbálja, hogy a kulccsal melyik láncon mi érhető el. Keveset fogyaszt."""
    address = config["wallets"][0][1]
    disabled = []
    print("%-12s %-10s %-10s %-22s %s" % ("Lánc", "Csomópont", "Mozgások", "Egyenleg (Portfolio)",
                                          "Megjegyzés"))
    for slug, name, _symbol in select_chains(config, args):
        node = moves = portfolio = "–"
        notes = []
        try:
            api.rpc(slug, "eth_blockNumber", [])
            node = "ok"
        except ApiError as error:
            if error.fatal:
                raise
            if error.not_enabled:
                node = "tiltva"
                disabled.append(name)
            else:
                node = "nem megy"
                notes.append(api.safe(error)[:70])
        if node == "ok":
            try:
                api.rpc(slug, "alchemy_getAssetTransfers", [{
                    "fromBlock": "0x0", "toBlock": "latest", "toAddress": address,
                    "category": ["external", "erc20"], "maxCount": "0x1",
                }], kind="transfers")
                moves = "ok"
            except ApiError as error:
                if error.fatal:
                    raise
                moves = "nem megy"
                notes.append(api.safe(error)[:70])
        try:
            found, failed = fetch_portfolio(api, address, [slug])
            if failed:
                portfolio = "nem megy"
            else:
                held = [t for t in found.get(slug, []) if hex_int(t.get("tokenBalance"))]
                portfolio = "ok (%d token)" % len(held)
        except ApiError as error:
            if error.fatal:
                raise
            portfolio = "nem megy"
        print("%-12s %-10s %-10s %-22s %s" % (name[:12], node, moves, portfolio, "; ".join(notes)))
    if disabled:
        print_network_hint(disabled)
    return 0


def run_sync(api, db, config, args):
    wallets = config["wallets"]
    chains = select_chains(config, args)
    names = {slug: name for slug, name, _ in chains}

    db.execute("DELETE FROM wallets")
    db.executemany("INSERT INTO wallets VALUES (?, ?)", [(a, l) for l, a in wallets])
    db.commit()
    all_addresses = {address for _, address in wallets}
    disabled = []

    try:
        print("Egyenlegek…")
        for label, address in wallets:
            counts = sync_balances(api, db, address, chains, config)
            held = ", ".join("%s: %d" % (names[s], n) for s, n in counts.items() if n)
            print("  %-14s %s" % (label[:14], held or "nincs token"))

        if not args.only_balances:
            print("Mozgások…")
            for label, address in wallets:
                for slug, name, _symbol in chains:
                    added = sync_transfers(api, db, address, slug, all_addresses - {address})
                    if added is None:
                        note = db.execute(
                            "SELECT note FROM sync_state WHERE address=? AND chain=?",
                            (address, slug)).fetchone()
                        if note and str(note[0]).startswith("HTTP 403"):
                            if name not in disabled:
                                disabled.append(name)
                            print("  %-14s %-12s nincs bekapcsolva" % (label[:14], name[:12]))
                        else:
                            print("  %-14s %-12s nem érhető el" % (label[:14], name[:12]))
                    elif added:
                        print("  %-14s %-12s %d új mozgás" % (label[:14], name[:12], added))
        for _, address in wallets:
            fill_symbols(db, address)
            update_spam(db, address)

        if not args.only_balances:
            print("Érkezéskori árak…")
            found, missing, notes = sync_prices(api, db, chains, config["wrapped"])
            print("  %d érkezéshez van ár, %d-hez nincs" % (found, missing))
            for note in notes[:8]:
                print("  megjegyzés: %s" % note)
            sync_fees(api, db, chains, config)
    except BudgetExceeded:
        print("\nA beállított havi keret (%d CU) elfogyott. Emeld meg a config.ini-ben, "
              "vagy futtasd újra a következő hónapban." % config["monthly_cu"])
    except ApiError as error:
        if not error.fatal:
            raise
        print("\nAz Alchemy nem fogadta el az API kulcsot (%s). Ellenőrizd a .env fájlt."
              % error.status)
        return 2
    if disabled:
        print_network_hint(disabled)
    print("Becsült felhasználás ebben a hónapban: %s / %s CU"
          % (fmt_num(api.used_this_month(), 0), fmt_num(config["monthly_cu"], 0)))
    return 0


# --------------------------------------------------------------------------
# Kiértékelés és kiírás
# --------------------------------------------------------------------------

def consume(lots, need):
    for lot in lots:
        if need <= 0:
            break
        take = min(lot["amount"], need)
        lot["amount"] -= take
        need -= take


def live(lots):
    return [lot for lot in lots if lot["amount"] > lot["original"] * 1e-9]


def remaining_lots(db, address, chain, token):
    """A még meglévő érkezések, FIFO elven: a kimenő mozgások a legrégebbi tételt fogyasztják.

    Visszaad: (tételek, az összes valaha beérkezett mennyiség).
    """
    rows = db.execute(
        "SELECT t.direction, t.amount, t.block_time, t.acq, t.initiated, t.usd_price_then, "
        "       t.price_status, t.price_basis, t.id, t.tx_hash, t.counterparty, f.acq "
        "FROM transfers t LEFT JOIN lot_flags f ON f.transfer_id = t.id "
        "WHERE t.address=? AND t.chain=? AND t.token_address=? "
        "ORDER BY t.block_number, t.id",
        (address, chain, token),
    ).fetchall()
    lots = []
    for (direction, amount, block_time, acq, initiated, price, status, basis, row_id, tx_hash,
         counterparty, manual) in rows:
        if direction == "in":
            lots.append({"amount": amount, "original": amount, "time": block_time,
                         "acq": manual or acq, "acq_manual": bool(manual),
                         "initiated": bool(initiated), "price": price, "status": status,
                         "basis": basis, "id": row_id, "tx_hash": tx_hash,
                         "counterparty": counterparty})
        else:
            consume(lots, amount)
    return live(lots), sum(lot["original"] for lot in lots)


def evaluate(db, address):
    """Egy cím tokenjei kiértékelve: mostani érték, érkezéskori érték, jelölések."""
    tokens = []
    fees = dict(db.execute("SELECT chain, fee_usd FROM chain_info").fetchall())
    flags = {(c, t): (spam_flag, watch) for c, t, spam_flag, watch in db.execute(
        "SELECT chain, token_address, spam, watch FROM token_flags WHERE address=?", (address,))}
    rows = db.execute(
        "SELECT chain, token_address, symbol, name, amount, usd_price, usd_value, native, "
        "       source, possible_spam, spam_reason, price_source, liquidity_usd "
        "FROM balances WHERE address=?",
        (address,),
    ).fetchall()
    for (chain, token, symbol, name, amount, price, value, native, source, spam, reason,
         price_source, liquidity) in rows:
        spam_flag, watch = flags.get((chain, token), (None, None))
        fee = fees.get(chain)
        lots, total_in = remaining_lots(db, address, chain, token)
        held = sum(lot["amount"] for lot in lots)
        had_history = total_in > 0
        # Volt mozgása, de szinte mind elment: eladás után maradt töredék.
        leftover = had_history and not native and amount <= total_in * 1e-6
        unknown = 0.0
        mismatch = False
        if lots and native:
            # A tranzakciós díjak nem látszanak a mozgások között: a többletet a legrégebbi
            # tételekből vonjuk le. Ha kevesebb jött össze, mint az egyenleg, annak a résznek
            # ismeretlen az eredete (például szerződésen belüli átutalás volt).
            if held > amount:
                consume(lots, held - amount)
                lots = live(lots)
            elif amount > held:
                unknown = amount - held
        elif lots:
            mismatch = abs(held - amount) > max(amount, held) * 0.01

        known = bool(lots) and all(lot["price"] is not None for lot in lots)
        partly_unknown = unknown > amount * 0.02
        then_value = None
        if known and not mismatch and not partly_unknown:
            then_value = sum(lot["amount"] * lot["price"] for lot in lots)
        free = [lot for lot in lots if lot["acq"] == "free"]
        tokens.append({
            "chain": chain, "token": token,
            "symbol": symbol or (short(token) if token != "native" else "?"),
            "name": name or "",
            "amount": amount, "price": price, "value": value, "native": bool(native),
            "nft": source == "nft",
            "spam": bool(spam), "spam_reason": reason or "",
            "price_source": price_source, "liquidity": liquidity,
            "lots": lots, "then_value": then_value,
            "first_time": lots[0]["time"] if lots else None,
            "all_free": bool(lots) and len(free) == len(lots),
            "part_free": bool(free) and len(free) < len(lots),
            "pending": any(lot["status"] == "pending" for lot in lots),
            "mismatch": mismatch,
            "partly_unknown": partly_unknown,
            "no_history": not had_history,
            "leftover": leftover,
            "total_in": total_in,
            "spam_manual": spam_flag is not None,
            "watch": bool(watch),
            "fee": fee,
            # az eladás díja több, mint amennyit a token ér
            "dust": value is not None and fee is not None and value <= fee,
        })
    tokens.sort(key=lambda t: (t["value"] is None, -(t["value"] or 0.0), t["symbol"].lower()))
    return tokens


def print_report(db, config):
    names = {slug: name for slug, name, _ in config["chains"]}

    def chain_name(token):
        return names.get(token["chain"], token["chain"])

    wallets = db.execute("SELECT address, label FROM wallets ORDER BY rowid").fetchall()
    if not wallets:
        print("Az adatbázis még üres. Futtasd előbb: python3 fetch.py --only-balances")
        return
    for address, label in wallets:
        tokens = evaluate(db, address)
        nfts = [t for t in tokens if t["nft"]]
        spam = [t for t in tokens if t["spam"] and not t["nft"]]
        rest = [t for t in tokens if not t["spam"] and not t["nft"]]
        crumbs = [t for t in rest if t["leftover"] and (t["value"] or 0) < 0.01]
        normal = [t for t in rest if t not in crumbs]

        print("\n== %s  %s ==" % (label, short(address)))
        if not normal:
            print("  (nincs token)")
        else:
            print("  %-12s %-10s %16s %14s %14s %14s  %s" % (
                "Token", "Lánc", "Mennyiség", "Most", "Érkezéskor", "Változás", "Megjegyzés"))
        total_now = total_then = total_then_now = 0.0
        for t in normal:
            notes = []
            if t["all_free"]:
                notes.append("ingyenes")
            elif t["part_free"]:
                notes.append("részben ingyenes")
            if t["pending"]:
                notes.append("ár lekérés alatt")
            if t["no_history"]:
                notes.append("nincs történet")
            elif t["leftover"]:
                notes.append("eladás utáni maradék")
            elif t["mismatch"]:
                notes.append("eltér az egyenlegtől")
            elif t["partly_unknown"]:
                notes.append("részben ismeretlen eredetű")
            if t["dust"]:
                notes.append("díj alatt")
            change = None
            if t["value"] is not None:
                total_now += t["value"]
            if t["value"] is not None and t["then_value"] is not None:
                change = t["value"] - t["then_value"]
                total_then += t["then_value"]
                total_then_now += t["value"]
            print("  %-12s %-10s %16s %14s %14s %14s  %s" % (
                t["symbol"][:12], chain_name(t)[:10], fmt_qty(t["amount"]),
                fmt_usd(t["value"]), fmt_usd(t["then_value"]), fmt_change(change),
                ", ".join(notes)))
        if normal:
            print("  Összesen most: %s   |   ismert érkezéskori érték: %s   |   változás: %s" % (
                fmt_usd(total_now), fmt_usd(total_then),
                fmt_change(total_then_now - total_then)))
        if crumbs:
            print("  Eladás utáni töredékek (%d): %s" % (len(crumbs), ", ".join(
                "%s (%s)" % (t["symbol"], chain_name(t)) for t in crumbs)))
        if nfts:
            print("  NFT-kollekciók (%d): %s" % (len(nfts), ", ".join(
                "%s ×%d" % (t["symbol"], round(t["amount"])) for t in nfts)))
        if spam:
            print("  Scam-gyanús (%d):" % len(spam))
            for t in spam[:25]:
                print("    %-14s %-10s %s" % (t["symbol"][:14], chain_name(t)[:10],
                                              t["spam_reason"]))
            if len(spam) > 25:
                print("    … és még %d" % (len(spam) - 25))
        blocked = db.execute(
            "SELECT chain FROM sync_state WHERE address=? AND transfers_ok=0", (address,)
        ).fetchall()
        if blocked:
            print("  Mozgások nem érhetők el ezeken a láncokon: %s"
                  % ", ".join(names.get(c[0], c[0]) for c in blocked))


# --------------------------------------------------------------------------
# Belépési pont
# --------------------------------------------------------------------------

def get_api_key():
    """Az Alchemy kulcs a környezetből vagy a .env fájlból; ha nincs, None."""
    env = load_env(os.path.join(HERE, ".env"))
    api_key = os.environ.get("ALCHEMY_API_KEY") or env.get("ALCHEMY_API_KEY")
    if not api_key:
        return None
    # Ha valaki a teljes endpoint URL-t másolta be, abból is csak a kulcs kell.
    return api_key.strip().rstrip("/").split("/")[-1] or None


def open_db(path):
    db = sqlite3.connect(path, timeout=30)
    if path != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")   # a felület olvashat, miközben a frissítés ír
    db.executescript(SCHEMA)
    upgraded = False
    for table, column, decl in NEW_COLUMNS:
        present = {row[1] for row in db.execute("PRAGMA table_info(%s)" % table)}
        if column not in present:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
            upgraded = upgraded or column == "price_basis"
    if upgraded:
        # Az árszámítás módja változott: a korábban beárazott érkezéseket újraszámoljuk.
        db.execute("UPDATE transfers SET price_status='pending', usd_price_then=NULL "
                   "WHERE direction='in'")
    db.commit()
    return db


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Tárcafigyelő – adatlekérő (Alchemy → SQLite)")
    parser.add_argument("--probe", action="store_true",
                        help="kipróbálja, melyik láncon mi érhető el a kulccsal")
    parser.add_argument("--only-balances", action="store_true",
                        help="csak az aktuális egyenlegek")
    parser.add_argument("--report", action="store_true",
                        help="csak kiírás az adatbázisból, API-hívás nélkül")
    parser.add_argument("--retry-prices", action="store_true",
                        help="a korábban ár nélkül maradt érkezésekhez újra kér árat")
    parser.add_argument("--chains", help="csak ezek a láncok, vesszővel (pl. eth-mainnet,monad-mainnet)")
    parser.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    parser.add_argument("--db", default=os.path.join(HERE, "tarca.sqlite3"))
    args = parser.parse_args(argv)

    ensure_config(args.config)
    config = load_config(args.config)
    db = open_db(args.db)
    if args.report:
        print_report(db, config)
        return 0

    api_key = get_api_key()
    if not api_key:
        print("Hiányzik az ALCHEMY_API_KEY. Másold a .env.example fájlt .env néven, "
              "és írd bele a kulcsot.")
        return 2

    api = Alchemy(api_key, db, config["monthly_cu"])
    if args.probe:
        try:
            return run_probe(api, config, args)
        except ApiError as error:
            print("Az Alchemy nem fogadta el az API kulcsot (%s). Ellenőrizd a .env fájlt."
                  % error.status)
            return 2
    if args.retry_prices:
        db.execute("DELETE FROM day_prices WHERE price IS NULL")
        db.execute("UPDATE transfers SET price_status='pending' "
                   "WHERE direction='in' AND price_status='none'")
        db.commit()
    code = run_sync(api, db, config, args)
    print_report(db, config)
    return code


if __name__ == "__main__":
    sys.exit(main())
