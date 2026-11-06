#!/usr/bin/env python3
"""
lst_nav_scanner.py : READ-ONLY scanner for Solana LST arbitrage gaps.

What it checks, every cycle, for every SPL stake pool LST above a TVL floor:
  A) DISCOUNT  : buy LST with SOL on DEX (Jupiter quote) -> WithdrawSol at NAV
  B) PREMIUM   : DepositSol at NAV -> sell LST for SOL on DEX (Jupiter quote)
and logs the net result after pool fees, flash-loan fee and tip.

No keys, no transactions, nothing is ever sent on-chain.

Usage:
  pip install requests
  export RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"   # public RPC usually blocks getProgramAccounts
  python lst_nav_scanner.py --once              # single cycle, sanity check
  python lst_nav_scanner.py --hours 6           # run for 6 hours, log to CSV

Env overrides:
  JUP_BASE (default lite-api), JUP_API_KEY, JUP_DELAY (sec between quotes, default 1.1)
  FLASH_FEE_BPS (default 0, SET THIS once we confirm the lender's real fee)
  TIP_LAMPORTS (default 100000 = 0.0001 SOL)
  SLOW_TX_LAMPORTS (default 50000, tx fees for the slow unstake route)
  SLOW_ALERT_BPS (default 50), ALERT_COOLDOWN_MIN (default 30)
  TG_BOT_TOKEN, TG_CHAT_ID (Telegram alerts; test with --test-alert)

Directions logged: discount (instant, flash-loanable), premium (mint and sell),
slow (buy, unstake over ~1 epoch, needs own capital).
"""
import os, sys, time, csv, base64, argparse
from datetime import datetime, timezone
import requests

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_BASE = os.getenv("JUP_BASE", "https://lite-api.jup.ag/swap/v1")
JUP_KEY = os.getenv("JUP_API_KEY", "")
JUP_DELAY = float(os.getenv("JUP_DELAY", "1.1"))
FLASH_FEE_BPS = float(os.getenv("FLASH_FEE_BPS", "0"))
TIP_LAMPORTS = int(os.getenv("TIP_LAMPORTS", "100000"))
SLOW_TX_LAMPORTS = int(os.getenv("SLOW_TX_LAMPORTS", "50000"))  # ~4 normal txs for the slow route
SLOW_ALERT_BPS = float(os.getenv("SLOW_ALERT_BPS", "50"))       # flag + alert slow hits at/above this
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN_MIN", "30")) * 60  # no repeat alert per pool within this
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
LAST_ALERT = {}
BASE_FEE = 5000

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
STAKE_RENT = 2_282_880  # rent-exempt minimum the reserve stake account must keep

STAKE_POOL_PROGRAMS = {
    "spl": "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy",
    "sanctum_multi": "SP12tWFxD9oJsVWNavTTBZvMbA6gkAmxtVgxdqvyvhY",
    "sanctum_single": "SPMBzsVUuoHA4Jm6KunbsotaahvVikZs1JyTW6iJvbn",
}

KNOWN = {
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": "jupSOL",
    "he1iusmfkpAdwvxLNGV8Y1iSbj4rUy6yMhEA3fotn9A": "hSOL",
    "BonK1YhkXEGLZzwtcvRTip3gAL9nCeQD7ppZBLXhtTs": "bonkSOL",
}

# ---------------------------------------------------------------- helpers
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + s

def sym(mint):
    return KNOWN.get(mint, mint[:6] + "..")

def rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=90)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]

def get_multiple(addrs):
    out = []
    for i in range(0, len(addrs), 100):
        res = rpc("getMultipleAccounts", [addrs[i:i + 100], {"encoding": "base64"}])
        out.extend(res["value"])
    return out

# ---------------------------------------------------------------- stake pool parsing (borsh)
class Reader:
    def __init__(self, b): self.b, self.o = b, 0
    def u8(self): v = self.b[self.o]; self.o += 1; return v
    def u64(self): v = int.from_bytes(self.b[self.o:self.o + 8], "little"); self.o += 8; return v
    def pk(self): v = b58encode(self.b[self.o:self.o + 32]); self.o += 32; return v
    def skip(self, n): self.o += n
    def fee(self):
        d = self.u64(); n = self.u64()          # Fee { denominator, numerator }
        return n / d if d else 0.0
    def opt_pk(self): return self.pk() if self.u8() else None
    def future_fee(self): return self.fee() if self.u8() else None

def parse_pool(raw: bytes):
    r = Reader(raw)
    if r.u8() != 1:                              # AccountType::StakePool
        return None
    r.skip(32 * 3)                               # manager, staker, stake_deposit_authority
    r.skip(1)                                    # stake_withdraw_bump_seed
    r.pk()                                       # validator_list
    reserve = r.pk()
    mint = r.pk()
    r.skip(32 * 2)                               # manager_fee_account, token_program_id
    total = r.u64(); supply = r.u64(); last_epoch = r.u64()
    r.skip(48)                                   # lockup
    r.fee(); r.future_fee()                      # epoch_fee, next_epoch_fee
    r.opt_pk(); r.opt_pk()                       # preferred deposit / withdraw validator
    r.fee()                                      # stake_deposit_fee
    stake_wd_fee = r.fee()                       # stake_withdrawal_fee (slow route)
    r.future_fee()                               # next_stake_withdrawal_fee
    r.u8()                                       # stake_referral_fee
    sol_dep_auth = r.opt_pk()
    sol_dep_fee = r.fee()
    r.u8()                                       # sol_referral_fee
    sol_wd_auth = r.opt_pk()
    sol_wd_fee = r.fee()
    if supply == 0 or sol_dep_fee > 0.5 or sol_wd_fee > 0.5 or stake_wd_fee > 0.5:
        return None                              # empty pool or layout mismatch
    return dict(reserve=reserve, mint=mint, total=total, supply=supply, last_epoch=last_epoch,
                dep_auth=sol_dep_auth, dep_fee=sol_dep_fee, wd_auth=sol_wd_auth, wd_fee=sol_wd_fee,
                stake_wd_fee=stake_wd_fee)

def discover_pools():
    pools = {}
    for name, prog in STAKE_POOL_PROGRAMS.items():
        try:
            res = rpc("getProgramAccounts", [prog, {"encoding": "base64",
                      "filters": [{"memcmp": {"offset": 0, "bytes": "2"}}]}])  # first byte == 1
        except Exception as e:
            print(f"[WARN] discovery failed for {name}: {e}")
            continue
        n = 0
        for acc in res:
            try:
                p = parse_pool(base64.b64decode(acc["account"]["data"][0]))
            except Exception:
                p = None
            if p:
                pools[acc["pubkey"]] = p; n += 1
        print(f"[discover] {name}: {n} pools")
    return pools

# ---------------------------------------------------------------- jupiter
DROPPED = {"n": 0}

def jup_quote(in_mint, out_mint, amount, retries=3):
    params = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(int(amount)),
              "slippageBps": "10", "restrictIntermediateTokens": "true"}
    headers = {"x-api-key": JUP_KEY} if JUP_KEY else {}
    for attempt in range(retries):
        time.sleep(JUP_DELAY)
        try:
            r = requests.get(f"{JUP_BASE}/quote", params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"[WARN] Jupiter rate limit, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait); continue
            if r.status_code != 200:
                return None, ""          # no route, not a rate limit
            j = r.json()
            route = "+".join(s["swapInfo"].get("label", "?") for s in j.get("routePlan", []))
            return int(j["outAmount"]), route
        except Exception:
            continue
    DROPPED["n"] += 1
    return None, ""

# ---------------------------------------------------------------- telegram
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
                          timeout=15)
        if r.status_code != 200:
            print(f"[WARN] Telegram send failed: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")
        return False

def alert_slow(rows, epoch):
    best = {}
    for r in rows:   # best size per pool among qualifying slow hits
        if r["direction"] == "slow" and r["bps"] >= SLOW_ALERT_BPS and not r["stale"]:
            m = r["p"]["mint"]
            if m not in best or r["net"] > best[m]["net"]:
                best[m] = r
    now = time.time()
    for m, r in best.items():
        if now - LAST_ALERT.get(m, 0) < ALERT_COOLDOWN:
            continue
        msg = (f"$$ SLOW LST discount: {sym(m)}\n"
               f"{r['bps']:+.1f} bps on {r['size']} SOL = {r['net'] / LAMPORTS:+.4f} SOL\n"
               f"NAV {r['nav']:.6f} | stake withdraw fee {r['fee'] * 100:.2f}% | epoch {epoch}\n"
               f"Route: {r['route']}\n"
               f"Mint: {m}\n"
               f"Chart: https://gmgn.ai/sol/token/{m}\n"
               f"Buy: https://jup.ag/swap/SOL-{m}\n"
               f"Check the price on Jupiter before buying, this quote may be minutes old.")
        print("[ALERT] " + msg.replace("\n", " | "))
        tg_send(msg)
        LAST_ALERT[m] = now

# ---------------------------------------------------------------- scan
FIELDS = ["ts", "epoch", "symbol", "mint", "direction", "size_sol", "route", "nav",
          "pool_fee_pct", "proceeds_sol", "net_sol", "net_bps", "reserve_sol", "capped", "stale"]

def scan_cycle(pools, sizes, min_tvl, writer):
    epoch = rpc("getEpochInfo", [])["epoch"]
    keys = list(pools.keys())
    accs = get_multiple(keys)
    live = {}
    for k, a in zip(keys, accs):
        if not a: continue
        p = parse_pool(base64.b64decode(a["data"][0]))
        if p and p["total"] / LAMPORTS >= min_tvl:
            live[k] = p
    reserves = get_multiple([p["reserve"] for p in live.values()])
    for p, ra in zip(live.values(), reserves):
        p["reserve_avail"] = max(0, (ra["lamports"] if ra else 0) - STAKE_RENT)

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for p in live.values():
        nav = p["total"] / p["supply"]
        stale = p["last_epoch"] < epoch
        for size in sizes:
            x = int(size * LAMPORTS)
            costs = x * FLASH_FEE_BPS / 1e4 + TIP_LAMPORTS + BASE_FEE
            # one DEX quote (SOL -> LST) feeds both A and C, no extra Jupiter calls
            q, route = jup_quote(SOL_MINT, p["mint"], x)
            if q:
                # A) discount: buy on DEX, instant WithdrawSol at NAV (flash-loanable, reserve-capped)
                if p["wd_auth"] is None:
                    proceeds = q * (1 - p["wd_fee"]) * nav
                    net = proceeds - x - costs
                    rows.append(dict(direction="discount", size=size, route=route, nav=nav,
                                     fee=p["wd_fee"], proceeds=proceeds, net=net,
                                     capped=proceeds > p["reserve_avail"], p=p, stale=stale))
                # C) slow: buy on DEX, WithdrawStake, deactivate, wait ~1 epoch, withdraw SOL
                #    own capital, no flash fee, no tip race, not limited by reserve
                proceeds = q * (1 - p["stake_wd_fee"]) * nav
                net = proceeds - x - SLOW_TX_LAMPORTS
                rows.append(dict(direction="slow", size=size, route=route, nav=nav,
                                 fee=p["stake_wd_fee"], proceeds=proceeds, net=net,
                                 capped=False, p=p, stale=stale))
            # B) premium: deposit at NAV, sell on DEX
            if p["dep_auth"] is None:
                tokens = x / nav * (1 - p["dep_fee"])
                q, route = jup_quote(p["mint"], SOL_MINT, tokens)
                if q:
                    net = q - x - costs
                    rows.append(dict(direction="premium", size=size, route=route, nav=nav,
                                     fee=p["dep_fee"], proceeds=q, net=net,
                                     capped=False, p=p, stale=stale))

    for r in rows:
        r["bps"] = r["net"] / (r["size"] * LAMPORTS) * 1e4
        writer.writerow({
            "ts": ts, "epoch": epoch, "symbol": sym(r["p"]["mint"]), "mint": r["p"]["mint"],
            "direction": r["direction"], "size_sol": r["size"], "route": r["route"],
            "nav": f"{r['nav']:.9f}", "pool_fee_pct": f"{r['fee'] * 100:.3f}",
            "proceeds_sol": f"{r['proceeds'] / LAMPORTS:.6f}", "net_sol": f"{r['net'] / LAMPORTS:.6f}",
            "net_bps": f"{r['bps']:.2f}", "reserve_sol": f"{r['p']['reserve_avail'] / LAMPORTS:.2f}",
            "capped": r["capped"], "stale": r["stale"]})

    rows.sort(key=lambda r: r["bps"], reverse=True)
    alert_slow(rows, epoch)
    print(f"\n[{ts}] epoch {epoch} | {len(live)} pools >= {min_tvl} SOL TVL | {len(rows)} quotes"
          f" | dropped after retries: {DROPPED['n']}")
    DROPPED["n"] = 0
    for r in rows[:5]:
        if r["direction"] == "slow":
            flag = "$$ SLOW" if r["bps"] >= SLOW_ALERT_BPS and not r["stale"] else "       "
        else:
            flag = "!!! GAP" if r["net"] > 0 and not r["capped"] and not r["stale"] else "       "
        print(f"{flag} {sym(r['p']['mint']):9} {r['direction']:8} {r['size']:>6} SOL "
              f"net {r['net'] / LAMPORTS:+.6f} SOL ({r['bps']:+.1f} bps) "
              f"fee {r['fee'] * 100:.2f}% res {r['p']['reserve_avail'] / LAMPORTS:.1f}"
              f"{' CAPPED' if r['capped'] else ''}{' STALE' if r['stale'] else ''} [{r['route']}]")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="5,25", help="trade sizes in SOL, comma separated")
    ap.add_argument("--min-tvl", type=float, default=1000, help="skip pools below this SOL TVL")
    ap.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    ap.add_argument("--hours", type=float, default=0, help="stop after N hours (0 = forever)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--out", default="lst_gaps.csv")
    ap.add_argument("--test-alert", action="store_true", help="send a Telegram test message and exit")
    a = ap.parse_args()
    sizes = [float(s) for s in a.sizes.split(",")]
    if a.test_alert:
        ok = tg_send("lst_nav_scanner: Telegram alerts are working.")
        sys.exit("Test alert sent." if ok else "Test alert FAILED: check TG_BOT_TOKEN and TG_CHAT_ID.")
    if not (TG_TOKEN and TG_CHAT):
        print("[NOTE] TG_BOT_TOKEN / TG_CHAT_ID not set, alerts print to log only.")

    if FLASH_FEE_BPS == 0:
        print("[NOTE] FLASH_FEE_BPS=0, net figures exclude the flash-loan fee.")
    pools = discover_pools()
    if not pools:
        sys.exit("No pools found. Use an RPC that allows getProgramAccounts (set RPC_URL).")

    jito = next((p for p in pools.values() if p["mint"] in KNOWN and KNOWN[p["mint"]] == "JitoSOL"), None)
    if jito:
        print(f"[sanity] JitoSOL NAV = {jito['total'] / jito['supply']:.6f} SOL, "
              f"withdraw fee {jito['wd_fee'] * 100:.3f}% (NAV should be > 1.0)")

    new = not os.path.exists(a.out)
    end = time.time() + a.hours * 3600 if a.hours else None
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new: w.writeheader()
        while True:
            t0 = time.time()
            try:
                scan_cycle(pools, sizes, a.min_tvl, w)
                f.flush()
            except Exception as e:
                print(f"[ERROR] cycle failed: {e}")
            if a.once or (end and time.time() > end):
                break
            time.sleep(max(0, a.interval - (time.time() - t0)))

if __name__ == "__main__":
    main()
