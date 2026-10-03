"""Próbák az adatlekérőhöz – hálózat és API kulcs nélkül, szimulált válaszokkal.

Futtatás:  python3 -m unittest -v test_fetch
"""

import argparse
import contextlib
import io
import unittest
import urllib.parse

import fetch

KEY = "titkos-teszt-kulcs"
A = "0x" + "a" * 40          # figyelt cím
B = "0x" + "b" * 40          # második saját cím
STRANGER = "0x" + "d" * 40
ROUTER = "0x" + "e" * 40
TK1 = "0x" + "1" * 40        # rendes token: részben ingyen, részben vétel
CLM = "0x" + "2" * 40        # általam indított claim, van ára
AIR = "0x" + "3" * 40        # kéretlenül jött, nincs ára
DEXT = "0x" + "4" * 40       # vettem; ára csak a DexScreeneren van, jele csak a mozgásokban
LOWLIQ = "0x" + "5" * 40     # kéretlen; a DEX-párja túl kicsi, ezért nincs ára
NFT1 = "0x" + "6" * 40       # NFT-kollekció (nincs tizedesjegye)
USDC = "0x" + "8" * 40       # stabilcoin, idegentől érkezett: átutalás, nem ajándék
CR = "0x" + "c" * 40         # vettem, majd szinte mindet eladtam: töredék
LURE = "0x" + "9" * 40       # link a nevében
CHOG = "0x" + "f" * 40       # Monad token, MON-ért vettem
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
HEAD = 1000

E, M = "eth-mainnet", "monad-mainnet"


def tok(address, raw, symbol, name, decimals, price, network=E):
    return {
        "network": network, "address": A, "tokenAddress": address,
        "tokenBalance": hex(raw),
        "tokenMetadata": {"symbol": symbol, "name": name, "decimals": decimals, "logo": None},
        "tokenPrices": [] if price is None else [
            {"currency": "usd", "value": str(price), "lastUpdatedAt": "2026-10-03T00:00:00Z"}],
    }


PORTFOLIO = {
    E: [
        tok(None, 45 * 10 ** 16, "ETH", "Ethereum", 18, 4000),
        tok(TK1, 750 * 10 ** 18, "TK1", "Token One", 18, 0.02),
        tok(CLM, 50 * 10 ** 6, "CLM", "Claimed Token", 6, 1.0),
        tok(AIR, 100 * 10 ** 18, "AIR", "Quiet Token", 18, None),
        tok(LURE, 1000 * 10 ** 18, "USDC-R", "Visit usdc-reward.io to claim", 18, None),
        tok(DEXT, 1000 * 10 ** 18, None, None, 18, None),
        tok(LOWLIQ, 500 * 10 ** 18, "LOW", "Low Liquidity", 18, None),
        tok(NFT1, 5, None, None, None, None),
        tok(USDC, 9500000, "USDC", "USD Coin", 6, 1.0),
        tok(CR, 5 * 10 ** 11, "CR", "Crumb", 18, None),
        tok("0x" + "7" * 40, 0, "OLD", "Sold Out", 18, 1.0),   # nulla egyenleg: nem tároljuk
    ],
    M: [
        tok(None, 2767 * 10 ** 16, "MON", "Monad", 18, None, M),
        tok(CHOG, 10000 * 10 ** 18, "CHOG", "Chog", 18, None, M),
    ],
}


def tr(block, day, tx, frm, to, value, category="erc20", token=None, asset="TK1"):
    return {
        "blockNum": hex(block), "uniqueId": "%s:%s:%s" % (tx, category, token or "n"),
        "hash": tx, "from": frm, "to": to, "value": value, "asset": asset, "category": category,
        "rawContract": {"value": None, "address": token, "decimal": "0x12"},
        "metadata": {"blockTimestamp": day + "T12:00:00.000Z"},
    }


TRANSFERS = {
    E: ([   # bejövő
        tr(50, "2023-12-01", "0xt0", B, A, 0.602, "external", asset="ETH"),
        tr(100, "2024-01-01", "0xt1", STRANGER, A, 1000, token=TK1),
        tr(200, "2024-02-01", "0xt2", ROUTER, A, 250, token=TK1),
        tr(500, "2024-05-01", "0xt5", STRANGER, A, 1000, token=LURE, asset="USDC-R"),
        tr(600, "2024-06-01", "0xt6", STRANGER, A, 100, token=AIR, asset="AIR"),
        tr(650, "2024-06-15", "0xt65", STRANGER, A, 500, token=LOWLIQ, asset="LOW"),
        tr(700, "2024-07-01", "0xt7", STRANGER, A, 50, token=CLM, asset="CLM"),
        tr(900, "2024-09-01", "0xt9", ROUTER, A, 1000, token=DEXT, asset="DEXT"),
        tr(910, "2024-09-10", "0xt91", STRANGER, A, 9.5, token=USDC, asset="USDC"),
        tr(920, "2024-09-20", "0xt92", ROUTER, A, 100, token=CR, asset="CR"),
        tr(930, "2024-09-25", "0xt93", ROUTER, A, 0.0009, "internal", asset="ETH"),
    ], [    # kimenő
        tr(200, "2024-02-01", "0xt2", A, ROUTER, 0.1, "external", asset="ETH"),
        tr(300, "2024-03-01", "0xt3", A, TK1, 0, "external", asset="ETH"),
        tr(300, "2024-03-01", "0xt3", A, STRANGER, 500, token=TK1),
        tr(700, "2024-07-01", "0xt7", A, CLM, 0, "external", asset="ETH"),
        tr(800, "2024-08-01", "0xt8", A, STRANGER, 0, token=TK1),              # nulla értékű csali
        tr(900, "2024-09-01", "0xt9", A, ROUTER, 0.05, "external", asset="ETH"),
        tr(920, "2024-09-20", "0xt92", A, ROUTER, 0.001, "external", asset="ETH"),
        tr(930, "2024-09-25", "0xt93", A, ROUTER, 0, "external", asset="ETH"),
        tr(930, "2024-09-25", "0xt93", A, ROUTER, 99.9999995, token=CR, asset="CR"),
    ]),
    M: ([
        tr(10, "2026-01-10", "0xm1", STRANGER, A, 30, "external", asset="MON"),
        tr(20, "2026-02-01", "0xm2", ROUTER, A, 10000, token=CHOG, asset="CHOG"),
    ], [
        tr(20, "2026-02-01", "0xm2", A, ROUTER, 2, "external", asset="MON"),
    ]),
}

HISTORY = {
    ("symbol", "ETH"): {"2023-12-01": 3000, "2024-02-01": 2000, "2024-09-01": 2500,
                        "2024-09-20": 2550, "2024-09-25": 2600},
    ("symbol", "MON"): {"2026-01-10": 0.03, "2026-02-01": 0.025},
    (E, TK1): {"2024-01-01": 0.03},
    (E, CLM): {"2024-07-01": 0.5},
    (E, USDC): {"2024-09-10": 1.0},
}
SYMBOL_NOW = {"ETH": 4000, "MON": 0.032, "ODD": 0.5}


def pair(base, price, liquidity, quote="0x" + "0" * 40, native="1"):
    return {"chainId": "x", "pairAddress": "0xpool" + base[-4:],
            "baseToken": {"address": base, "symbol": "B"},
            "quoteToken": {"address": quote, "symbol": "Q"},
            "priceUsd": str(price), "priceNative": native, "liquidity": {"usd": liquidity}}


DEX = {
    "ethereum": [pair(DEXT, 2.0, 10000), pair(LOWLIQ, 9.0, 100)],
    "monad": [pair(WMON, 0.03225, 1476784), pair(CHOG, 0.0001, 5000)],
}


class FakeAlchemy(fetch.Alchemy):
    """A valódi kliens, de a hálózati hívás helyén a fenti mintaválaszokkal."""

    def __init__(self, db, monthly_cu=20000000, history_fails=False, symbol_now=None):
        super().__init__(KEY, db, monthly_cu, pause=0)
        self.calls = []
        self.history_fails = history_fails
        self.symbol_now = symbol_now or SYMBOL_NOW

    def _http(self, method, url, body=None):
        parsed = urllib.parse.urlparse(url)
        host, path = parsed.netloc, parsed.path.replace("/" + KEY, "")
        if host.endswith(".g.alchemy.com") and host != "api.g.alchemy.com":
            network = host.split(".")[0]
            self.calls.append((network, body["method"]))
            if network == "base-mainnet":   # nincs bekapcsolva az appban
                return 403, '{"error": {"message": "BASE_MAINNET is not enabled for this app"}}'
            return 200, self.rpc_reply(network, body["method"], body["params"])
        self.calls.append((path, body))
        if path == "/data/v1/assets/tokens/by-address":
            return self.portfolio(body)
        if path == "/prices/v1/tokens/by-symbol":
            symbol = dict(urllib.parse.parse_qsl(parsed.query))["symbols"]
            price = self.symbol_now.get(symbol)
            prices = [] if price is None else [{"currency": "usd", "value": str(price)}]
            return 200, {"data": [{"symbol": symbol, "prices": prices}]}
        if path == "/prices/v1/tokens/historical":
            if self.history_fails:
                return 400, '{"error": {"message": "bad request %s"}}' % KEY
            if body.get("network") == M:
                return 400, '{"error":{"message":"Unsupported network: monad-mainnet"}}'
            key = ("symbol", body["symbol"]) if "symbol" in body else (body["network"],
                                                                       body["address"])
            days = HISTORY.get(key)
            if days is None:
                return 404, '{"error": {"message": "Token not found"}}'
            data = [{"value": str(v), "timestamp": d + "T00:00:00Z"} for d, v in days.items()
                    if body["startTime"][:10] <= d <= body["endTime"][:10]]
            return 200, {"currency": "usd", "data": data}
        return 404, "ismeretlen végpont"

    def portfolio(self, body):
        entry = body["addresses"][0]
        networks = entry["networks"]
        if "base-mainnet" in networks:
            return 403, '{"error": {"message": "BASE_MAINNET is not enabled for this app"}}'
        if "odd-mainnet" in networks:
            return 400, '{"error": {"message": "Unsupported network: odd-mainnet"}}'
        tokens = []
        if entry["address"] == A:
            for network in networks:
                tokens.extend(PORTFOLIO.get(network, []))
        return 200, {"data": {"tokens": tokens, "pageKey": None}}

    def rpc_reply(self, network, method, params):
        if method == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": hex(HEAD)}
        if method == "eth_gasPrice":
            gwei = {E: 20, M: 100}.get(network)
            if gwei is None:
                return {"jsonrpc": "2.0", "id": 1, "error": {"message": "nincs gázár"}}
            return {"jsonrpc": "2.0", "id": 1, "result": hex(gwei * 10 ** 9)}
        if method == "eth_getBlockByNumber":
            # 2024-09-10 12:00:00 UTC
            return {"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(1725969600)}}
        if method == "eth_getBalance":
            amount = 1250 * 10 ** 18 if (network == "odd-mainnet" and params[0] == A) else 0
            return {"jsonrpc": "2.0", "id": 1, "result": hex(amount)}
        if method == "alchemy_getTokenMetadata":
            meta = {"decimals": None, "symbol": "BLOOP", "name": "BLOOPS LAB", "logo": None}
            return {"jsonrpc": "2.0", "id": 1, "result": meta if params[0] == NFT1 else {}}
        if method == "alchemy_getAssetTransfers":
            if network not in TRANSFERS and network != "linea-mainnet":
                return {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": -32600, "message": "Unsupported method on this network"}}
            incoming, outgoing = TRANSFERS.get(network, ([], []))
            p = params[0]
            start = int(p["fromBlock"], 16)
            if p.get("toAddress") == A:
                items = [t for t in incoming if int(t["blockNum"], 16) >= start]
                if not p.get("pageKey") and len(items) > 3:      # lapozás kipróbálása
                    return {"result": {"transfers": items[:3], "pageKey": "tovább"}}
                if p.get("pageKey"):
                    items = items[3:]
            elif p.get("fromAddress") == A:
                items = [t for t in outgoing if int(t["blockNum"], 16) >= start]
                if p.get("excludeZeroValue"):
                    items = [t for t in items if t["value"]]
            else:
                items = []
            return {"jsonrpc": "2.0", "id": 1, "result": {"transfers": items}}
        return {"jsonrpc": "2.0", "id": 1, "error": {"message": "ismeretlen metódus"}}


ETH_ONLY = [(E, "Ethereum", "ETH")]
ETH_MONAD = [(E, "Ethereum", "ETH"), (M, "Monad", "MON")]


def make_config(chains=None):
    return {
        "wallets": [("Fő", A), ("Második", B)],
        "chains": chains or ETH_ONLY,
        "wrapped": {M: WMON},
        "monthly_cu": 20000000,
        "min_liquidity": 500.0,
    }


def make_args(**kw):
    base = dict(chains=None, only_balances=False, probe=False, retry_prices=False)
    base.update(kw)
    return argparse.Namespace(**base)


def run_quiet(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = function(*args)
    return code, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.db = fetch.open_db(":memory:")
        self.dex_calls = []
        self._dex_fetch = fetch.dex_fetch

        def fake_dex(chain_id, addresses):
            self.dex_calls.append((chain_id, list(addresses)))
            return [p for p in DEX.get(chain_id, [])
                    if p["baseToken"]["address"] in addresses
                    or p["quoteToken"]["address"] in addresses]

        fetch.dex_fetch = fake_dex
        self._sleep = fetch.time.sleep
        fetch.time.sleep = lambda seconds: None

    def tearDown(self):
        fetch.dex_fetch = self._dex_fetch
        fetch.time.sleep = self._sleep
        self.db.close()

    def sync(self, config=None, **kw):
        api = FakeAlchemy(self.db, **kw)
        code, out = run_quiet(fetch.run_sync, api, self.db, config or make_config(), make_args())
        return api, code, out

    def token(self, symbol, address=A):
        return next(t for t in fetch.evaluate(self.db, address) if t["symbol"] == symbol)


class FetchTests(Base):
    def test_bought_lot_uses_what_was_paid(self):
        _, code, _ = self.sync()
        self.assertEqual(code, 0)
        tk1 = self.token("TK1")
        # FIFO: az 1000-es ingyenes tételből 500 elment, a 250-es vétel megvan
        self.assertEqual([round(l["amount"]) for l in tk1["lots"]], [500, 250])
        self.assertEqual([l["acq"] for l in tk1["lots"]], ["free", "bought"])
        self.assertEqual([l["basis"] for l in tk1["lots"]], ["market", "paid"])
        # ingyenes rész: napi piaci ár; vétel: 0,1 ETH × 2000 $ = 200 $
        self.assertAlmostEqual(tk1["then_value"], 500 * 0.03 + 200)
        self.assertTrue(tk1["part_free"])
        self.assertFalse(tk1["mismatch"])
        self.assertFalse(tk1["spam"])

    def test_dex_price_and_symbol_from_transfers(self):
        self.sync()
        dext = self.token("DEXT")                      # a jel a mozgásokból jött
        self.assertEqual(dext["price_source"], "dex")
        self.assertAlmostEqual(dext["value"], 2000)
        self.assertAlmostEqual(dext["then_value"], 0.05 * 2500)   # amit fizettem érte
        self.assertFalse(dext["spam"])
        low = self.token("LOW")                        # túl kicsi a DEX-pár: nincs ára
        self.assertIsNone(low["value"])
        self.assertIn("kéretlenül", low["spam_reason"])

    def test_dex_prices_quote_side_and_liquidity_floor(self):
        wanted = "0x" + "ab" * 20
        other = "0x" + "cd" * 20
        fetch.dex_fetch = lambda chain_id, addresses: [
            pair(other, 2.0, 9000, quote=wanted, native="4.0"),
            pair(wanted, 99.0, 10),
        ]
        found = fetch.dex_prices(E, [wanted], 500.0)
        self.assertAlmostEqual(found[wanted][0], 0.5)   # 2,0 $ / 4,0 a pár másik oldaláról
        self.assertEqual(fetch.dex_prices("ismeretlen-lánc", [wanted], 500.0), {})

    def test_native_coin_from_own_wallet_with_fees_reconciled(self):
        self.sync()
        eth = self.token("ETH")
        self.assertEqual(eth["token"], "native")
        self.assertEqual([l["acq"] for l in eth["lots"]], ["internal", "bought"])
        self.assertAlmostEqual(sum(l["amount"] for l in eth["lots"]), 0.45)
        # az eladott CR-nek nincs ártörténete, ezért a kapott ETH napi piaci áron számol
        self.assertAlmostEqual(eth["then_value"], 0.4491 * 3000 + 0.0009 * 2600)
        self.assertFalse(eth["all_free"])
        self.assertFalse(eth["partly_unknown"])

    def test_money_arriving_is_a_transfer_not_a_gift(self):
        self.sync()
        usdc = self.token("USDC")
        self.assertEqual(usdc["lots"][0]["acq"], "transfer")
        self.assertFalse(usdc["all_free"])
        self.assertFalse(usdc["spam"])
        self.assertAlmostEqual(usdc["then_value"], 9.5)

    def test_claim_is_free_and_initiated(self):
        self.sync()
        clm = self.token("CLM")
        self.assertAlmostEqual(clm["amount"], 50)          # 6 tizedesjegyű token
        self.assertTrue(clm["all_free"])
        self.assertTrue(clm["lots"][0]["initiated"])
        self.assertFalse(clm["spam"])
        self.assertAlmostEqual(clm["then_value"], 25)

    def test_nft_and_leftover_are_separated(self):
        self.sync()
        nft = self.token("BLOOP")
        self.assertTrue(nft["nft"])
        self.assertEqual(nft["amount"], 5)
        self.assertTrue(self.token("CR")["leftover"])
        _, text = run_quiet(fetch.print_report, self.db, make_config())
        self.assertIn("NFT-kollekciók (1): BLOOP ×5", text)
        self.assertIn("Eladás utáni töredékek (1): CR (Ethereum)", text)
        self.assertIn("Scam-gyanús (3):", text)
        table = text.split("Összesen most")[0]
        for hidden in ("BLOOP", "CR ", "AIR", "LOW", "USDC-R"):
            self.assertNotIn(hidden, table)
        self.assertNotIn("+<", text)

    def test_spam_rules(self):
        self.sync()
        self.assertIn("kéretlenül", self.token("AIR")["spam_reason"])
        self.assertIn("link a névben", self.token("USDC-R")["spam_reason"])
        # scamre nem kérünk érkezéskori árat
        self.assertEqual(self.token("AIR")["lots"][0]["status"], "pending")
        # amiért fizettem, az akkor sem scam, ha gyanús a neve és nincs ára
        self.assertEqual(fetch.spam_reasons("Claim Bonus", "CB", None, [("bought", 1)]), [])
        self.assertEqual(fetch.spam_reasons("Arbitrum", "ARB", 0.4, [("free", 0)]), [])
        # csalogató szó csak ár nélkül számít
        self.assertEqual(fetch.spam_reasons("Staking Rewards", "SR", 1.2, [("free", 1)]), [])
        self.assertEqual(fetch.spam_reasons("Bonus Reward", "BR", None, [("free", 1)]),
                         ["csalogató név, nincs ára"])
        # hamis "USDC": átutalásnak látszik, de ár nélkül és kéretlenül ez is scam
        self.assertEqual(fetch.spam_reasons("USD Coin", "USDC", None, [("transfer", 0)]),
                         ["nincs ára, kéretlenül érkezett"])

    def test_zero_value_transfers_are_ignored_but_calls_are_kept(self):
        self.sync()
        self.assertFalse(self.db.execute(
            "SELECT 1 FROM transfers WHERE tx_hash='0xt8'").fetchone())
        calls = self.db.execute(
            "SELECT tx_hash FROM transfers WHERE kind='call' ORDER BY block_number").fetchall()
        self.assertEqual([c[0] for c in calls], ["0xt3", "0xt7", "0xt93"])

    def test_monad_prices_come_from_dex_and_symbol_history(self):
        api, code, out = self.sync(make_config(ETH_MONAD))
        self.assertEqual(code, 0)
        mon = self.token("MON")
        self.assertEqual(mon["price_source"], "dex")
        self.assertAlmostEqual(mon["value"], 27.67 * 0.03225)
        self.assertEqual(mon["lots"][0]["acq"], "transfer")
        self.assertFalse(mon["all_free"])
        self.assertAlmostEqual(mon["then_value"], 27.67 * 0.03)     # 30 jött, 2 ment, díjak
        chog = self.token("CHOG")
        self.assertAlmostEqual(chog["value"], 1.0)
        self.assertAlmostEqual(chog["then_value"], 2 * 0.025)        # 2 MON-t fizettem érte
        self.assertFalse(chog["spam"])
        # cím alapján a Monadon nincs ártörténet: egyszer próbáljuk, utána nem
        failing = [c for c in api.calls if c[0] == "/prices/v1/tokens/historical"
                   and c[1].get("network") == M]
        self.assertEqual(len(failing), 1)
        self.assertIn("Unsupported network", out)

    def test_ambiguous_symbol_price_is_rejected(self):
        wrong = dict(SYMBOL_NOW, MON=5.0)       # ugyanilyen jelű másik token ára
        _, _, out = self.sync(make_config(ETH_MONAD), symbol_now=wrong)
        self.assertIsNone(self.token("MON")["then_value"])
        self.assertIsNone(self.token("CHOG")["then_value"])
        self.assertIn("nem egyezik a coin árával", out)

    def test_rerun_adds_nothing(self):
        self.sync(make_config(ETH_MONAD))
        before = self.db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
        api2, _, _ = self.sync(make_config(ETH_MONAD))
        after = self.db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
        self.assertEqual(before, after)
        # a lánc vége nem változott: a mozgásokat nem kéri újra, ártörténetet sem
        self.assertFalse([c for c in api2.calls if c[1] == "alchemy_getAssetTransfers"])
        self.assertFalse([c for c in api2.calls if c[0] == "/prices/v1/tokens/historical"])
        self.assertAlmostEqual(self.token("TK1")["then_value"], 215)
        self.assertAlmostEqual(self.token("CHOG")["then_value"], 0.05)

    def test_old_database_is_upgraded_and_reclassified(self):
        self.sync()
        # egy korábbi változat adatbázisa: minden bejövő "free", piaci áron beárazva
        self.db.execute("UPDATE transfers SET acq='free', price_basis=NULL WHERE direction='in'")
        self.db.commit()
        self.sync()
        self.assertEqual(self.token("USDC")["lots"][0]["acq"], "transfer")
        self.assertEqual(self.token("ETH")["lots"][0]["acq"], "internal")

    def test_fee_estimate_and_dust(self):
        self.sync(make_config(ETH_MONAD))
        fees = dict(self.db.execute("SELECT chain, fee_usd FROM chain_info").fetchall())
        self.assertAlmostEqual(fees[E], 20e9 * fetch.SWAP_GAS / 1e18 * 4000)        # 14,40 $
        self.assertAlmostEqual(fees[M], 100e9 * fetch.SWAP_GAS / 1e18 * 0.03225)
        self.assertTrue(self.token("USDC")["dust"])      # 9,50 $-t ér, a díj 14,40 $
        self.assertFalse(self.token("DEXT")["dust"])
        self.assertFalse(self.token("CHOG")["dust"])     # a Monadon a díj töredék cent
        self.assertFalse(self.token("CR")["dust"])       # ár nélkül nem tudjuk megmondani

    def test_manual_flags_survive_refresh(self):
        self.sync()
        air = self.token("AIR")
        self.assertTrue(air["spam"])
        # kézzel: az AIR nem scam, a TK1 scam, a CLM érkezése vásárlás volt
        self.db.execute("INSERT INTO token_flags (address, chain, token_address, spam) "
                        "VALUES (?,?,?,0), (?,?,?,1)", (A, E, AIR, A, E, TK1))
        clm_lot = self.token("CLM")["lots"][0]["id"]
        self.db.execute("INSERT INTO lot_flags VALUES (?, 'bought')", (clm_lot,))
        self.db.commit()
        self.sync()                                      # a frissítés nem írja felül
        air, tk1, clm = self.token("AIR"), self.token("TK1"), self.token("CLM")
        self.assertFalse(air["spam"])
        self.assertTrue(air["spam_manual"])
        self.assertTrue(tk1["spam"])
        self.assertEqual(tk1["spam_reason"], "kézzel megjelölve")
        self.assertFalse(clm["all_free"])
        self.assertTrue(clm["lots"][0]["acq_manual"])

    def test_missing_timestamp_is_filled_from_block(self):
        original = TRANSFERS[E][0][8]["metadata"]["blockTimestamp"]
        TRANSFERS[E][0][8]["metadata"]["blockTimestamp"] = None      # az USDC érkezése
        try:
            self.sync()
        finally:
            TRANSFERS[E][0][8]["metadata"]["blockTimestamp"] = original
        usdc = self.token("USDC")
        self.assertEqual(usdc["lots"][0]["time"][:10], "2024-09-10")
        self.assertAlmostEqual(usdc["then_value"], 9.5)

    def test_unsupported_chain_falls_back_to_native_balance(self):
        chains = ETH_ONLY + [("odd-mainnet", "Odd", "ODD")]
        _, code, out = self.sync(make_config(chains))
        self.assertEqual(code, 0)
        odd = self.token("ODD")
        self.assertAlmostEqual(odd["amount"], 1250)
        self.assertAlmostEqual(odd["value"], 1250 * 0.5)     # ár a jel alapján
        self.assertTrue(odd["no_history"])
        self.assertIn("nem érhető el", out)
        self.assertAlmostEqual(self.token("TK1")["then_value"], 215)
        _, text = run_quiet(fetch.print_report, self.db, make_config(chains))
        self.assertIn("Mozgások nem érhetők el ezeken a láncokon: Odd", text)

    def test_disabled_network_is_not_treated_as_bad_key(self):
        chains = [(E, "Ethereum", "ETH"), ("base-mainnet", "Base", "ETH"), (M, "Monad", "MON")]
        config = make_config(chains)
        code, probe = run_quiet(fetch.run_probe, FakeAlchemy(self.db), config, make_args())
        self.assertEqual(code, 0)
        base_line = next(l for l in probe.splitlines() if l.startswith("Base"))
        self.assertIn("tiltva", base_line)
        self.assertIn("ok (10 token)", next(l for l in probe.splitlines()
                                            if l.startswith("Ethereum")))
        self.assertIn("nincsenek bekapcsolva az Alchemy appodban: Base", probe)
        self.assertNotIn(KEY, probe)

        _, code, out = self.sync(config)
        self.assertEqual(code, 0)
        self.assertIn("nincs bekapcsolva", out)
        self.assertIn("Networks fülön", out)
        self.assertAlmostEqual(self.token("TK1")["then_value"], 215)

    def test_bad_key_stops(self):
        api = FakeAlchemy(self.db)
        api._http = lambda method, url, body=None: (401, "Must be authenticated!")
        code, out = run_quiet(fetch.run_sync, api, self.db, make_config(), make_args())
        self.assertEqual(code, 2)
        self.assertIn("nem fogadta el az API kulcsot", out)

    def test_only_balances(self):
        api = FakeAlchemy(self.db)
        run_quiet(fetch.run_sync, api, self.db, make_config(), make_args(only_balances=True))
        self.assertFalse([c for c in api.calls if c[1] == "alchemy_getAssetTransfers"])
        self.assertTrue(self.token("TK1")["no_history"])
        self.assertTrue(self.token("USDC-R")["spam"])      # a név alapján így is kiszűrjük

    def test_price_failure_is_reported_without_leaking_key_and_can_be_retried(self):
        _, _, out = self.sync(history_fails=True)
        self.assertIn("megjegyzés: HTTP 400", out)
        self.assertNotIn(KEY, out)
        self.assertIsNone(self.token("TK1")["then_value"])

        # ugyanaz, amit a --retry-prices kapcsoló csinál
        self.db.execute("DELETE FROM day_prices WHERE price IS NULL")
        self.db.execute("UPDATE transfers SET price_status='pending' "
                        "WHERE direction='in' AND price_status='none'")
        self.sync()
        self.assertAlmostEqual(self.token("TK1")["then_value"], 215)

    def test_budget_stops_cleanly(self):
        config = make_config()
        config["monthly_cu"] = 900
        api = FakeAlchemy(self.db, monthly_cu=900)
        code, out = run_quiet(fetch.run_sync, api, self.db, config, make_args())
        self.assertEqual(code, 0)
        self.assertIn("elfogyott", out)
        self.assertLessEqual(api.used_this_month(), 900)

    def test_incomplete_history_is_flagged(self):
        self.sync()
        self.db.execute("DELETE FROM transfers WHERE tx_hash='0xt3'")   # hiányzik egy kimenő
        tk1 = self.token("TK1")
        self.assertTrue(tk1["mismatch"])
        self.assertIsNone(tk1["then_value"])

    def test_native_with_unknown_origin(self):
        self.sync()
        self.db.execute("UPDATE balances SET amount=2.0 WHERE token_address='native'")
        eth = self.token("ETH")
        self.assertTrue(eth["partly_unknown"])
        self.assertIsNone(eth["then_value"])


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        example = fetch.os.path.join(fetch.HERE, "config.example.ini")
        config = fetch.load_config(example, require_wallets=False)
        self.assertEqual(config["wallets"], [])        # a minta nem tartalmaz személyes címet
        with self.assertRaises(SystemExit):
            fetch.load_config(example)
        self.assertIn(("robinhood-mainnet", "Robinhood", "ETH"), config["chains"])
        self.assertIn((M, "Monad", "MON"), config["chains"])
        self.assertEqual(config["wrapped"][M], WMON)
        self.assertEqual(config["min_liquidity"], 500.0)
        for slug, _, _ in config["chains"]:
            self.assertIn(slug, fetch.DEX_CHAIN)

    def test_wallets_can_be_saved_without_touching_the_rest(self):
        import shutil
        import tempfile
        folder = tempfile.mkdtemp()
        path = fetch.os.path.join(folder, "config.ini")
        shutil.copy(fetch.os.path.join(fetch.HERE, "config.example.ini"),
                    fetch.os.path.join(folder, "config.example.ini"))
        fetch.ensure_config(path)                      # első indítás: a mintából jön létre
        before = fetch.load_config(path, require_wallets=False)
        second = "0x" + "Ab" * 20
        fetch.save_wallets(path, [("Fő tárca", "0x" + "1" * 40), ("Régi MetaMask", second)])
        after = fetch.load_config(path)
        self.assertEqual(after["wallets"][1], ("Régi MetaMask", second.lower()))
        self.assertEqual(after["chains"], before["chains"])
        self.assertEqual(after["wrapped"], before["wrapped"])
        self.assertIn("# Alchemy hálózatnév", open(path, encoding="utf-8").read())
        shutil.rmtree(folder)

    def test_wallet_validation(self):
        good = [("Fő", "0x" + "1" * 40)]
        for label, address, part in [
            ("Új", "0x12", "nem érvényes cím"),
            ("a=b", "0x" + "2" * 40, "név"),
            ("Kulcs", "0x" + "3" * 64, "privát kulcsnak"),
            ("Szavak", " ".join(["szó"] * 12), "seed phrase"),
            ("fő", "0x" + "4" * 40, "már foglalt"),
            ("Másik", "0x" + "1" * 40, "már szerepel"),
        ]:
            with self.assertRaises(ValueError) as caught:
                fetch.check_wallets(good + [(label, address)])
            self.assertIn(part, str(caught.exception))
        with self.assertRaises(ValueError):
            fetch.check_wallets([])
        with self.assertRaises(ValueError):
            fetch.check_wallets([("c%d" % i, "0x%040x" % i) for i in range(6)])

    def test_day_windows_and_formatting(self):
        days = ["2024-01-01", "2024-06-01", "2025-03-01", "2025-03-02"]
        self.assertEqual(list(fetch.day_windows(days, 300)),
                         [["2024-01-01", "2024-06-01"], ["2025-03-01", "2025-03-02"]])
        self.assertEqual(fetch.fmt_change(0.004), "0,00 $")
        self.assertEqual(fetch.fmt_change(-1.5), "-1,50 $")
        self.assertEqual(fetch.fmt_change(None), "n.a.")


if __name__ == "__main__":
    unittest.main()
