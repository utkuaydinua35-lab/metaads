#!/usr/bin/env python3
"""
metaads — Meta (Facebook/Instagram) reklam yönetimi CLI
Token: META_TOKEN ortam değişkeni veya ~/.metaads.env dosyası
Bağımlılık yok, sadece Python 3.8+
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_VERSION = os.environ.get("META_API_VERSION", "v23.0")
BASE = f"https://graph.facebook.com/{API_VERSION}"
CONFIG_FILE = Path.home() / ".metaads.json"
ENV_FILE = Path.home() / ".metaads.env"
ZERO_DECIMAL = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}
PURCHASE_TYPES = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
ACCOUNT_STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "GRACE_PERIOD", 100: "PENDING_CLOSURE", 101: "CLOSED",
}
LIVE_STATUSES = [
    "ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
    "WITH_ISSUES", "PENDING_REVIEW", "DISAPPROVED",
]
DATE_PRESETS = [
    "today", "yesterday", "last_3d", "last_7d", "last_14d", "last_30d",
    "last_90d", "this_month", "last_month", "maximum",
]


# ---------------------------------------------------------------- yardımcılar

def die(msg):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def load_env_file():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except ValueError:
            return {}
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def token():
    t = os.environ.get("META_TOKEN")
    if not t:
        die("META_TOKEN yok. ~/.metaads.env içine META_TOKEN=... yaz ya da export et.")
    return t


def encode(params):
    return urllib.parse.urlencode(
        {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
    )


def api(method, path, params=None):
    params = dict(params or {})
    data = None
    if path.startswith("http"):  # paging.next linki, token zaten içinde
        url = path
    else:
        params["access_token"] = token()
        url = f"{BASE}/{path.lstrip('/')}"
        if method == "GET":
            url += "?" + encode(params)
        else:
            data = encode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)["error"]
        except (ValueError, KeyError):
            die(f"HTTP {e.code}: {body[:500]}")
        msg = err.get("error_user_msg") or err.get("message")
        die(f"API hatası [{err.get('code')}/{err.get('error_subcode', '-')}]: {msg}")
    except urllib.error.URLError as e:
        die(f"Bağlantı hatası: {e.reason}")


def get_all(path, params=None, limit=1000):
    params = dict(params or {})
    params.setdefault("limit", 100)
    res = api("GET", path, params)
    rows = list(res.get("data", []))
    while res.get("paging", {}).get("next") and len(rows) < limit:
        res = api("GET", res["paging"]["next"])
        rows.extend(res.get("data", []))
    return rows


def account_id(args):
    acc = args.account or os.environ.get("META_AD_ACCOUNT") or load_config().get("account")
    if not acc:
        die("Reklam hesabı seçili değil. Önce: metaads accounts  →  metaads use act_XXXX")
    acc = str(acc)
    return acc if acc.startswith("act_") else f"act_{acc}"


_currency_cache = {}


def currency(acc):
    if acc not in _currency_cache:
        _currency_cache[acc] = api("GET", acc, {"fields": "currency"}).get("currency", "USD")
    return _currency_cache[acc]


def offset(cur):
    return 1 if cur in ZERO_DECIMAL else 100


def has_value(v):
    return v not in (None, "", "0", 0)


def fmt_money(minor, cur):
    if not has_value(minor):
        return "-"
    return f"{int(minor) / offset(cur):,.2f} {cur}"


def confirm(args, msg):
    print(msg)
    if args.yes:
        return
    if input("Onaylıyor musun? [e/H] ").strip().lower() not in ("e", "evet", "y", "yes"):
        die("İptal edildi.")


def cut(s, n=45):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def table(rows, cols):
    if not rows:
        print("(sonuç yok)")
        return
    rows = [{c: cut(r.get(c, "")) for c in cols} for r in rows]
    w = {c: max(len(c), *(len(r[c]) for r in rows)) for c in cols}
    print("  ".join(c.upper().ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(r[c].ljust(w[c]) for c in cols))


def status_filter(args):
    return {} if args.all else {"effective_status": LIVE_STATUSES}


def pick(actions):
    actions = actions or []
    for t in PURCHASE_TYPES:
        for a in actions:
            if a.get("action_type") == t:
                return float(a.get("value", 0))
    return 0.0


# ---------------------------------------------------------------- komutlar

def cmd_accounts(args):
    rows = get_all("me/adaccounts", {"fields": "name,account_id,currency,account_status,amount_spent"})
    for r in rows:
        r["id"] = f"act_{r.get('account_id')}"
        r["status"] = ACCOUNT_STATUS.get(r.get("account_status"), r.get("account_status"))
        r["spent_total"] = fmt_money(r.get("amount_spent"), r.get("currency", "USD"))
    table(rows, ["id", "name", "currency", "status", "spent_total"])
    cur = load_config().get("account")
    if cur:
        print(f"\nVarsayılan: {cur}")


def cmd_use(args):
    acc = args.account_id if args.account_id.startswith("act_") else f"act_{args.account_id}"
    info = api("GET", acc, {"fields": "name,currency"})
    cfg = load_config()
    cfg["account"] = acc
    save_config(cfg)
    print(f"✓ Varsayılan hesap: {acc} — {info.get('name')} ({info.get('currency')})")


def cmd_campaigns(args):
    acc = account_id(args)
    cur = currency(acc)
    p = {"fields": "id,name,effective_status,objective,daily_budget,lifetime_budget"}
    p.update(status_filter(args))
    rows = get_all(f"{acc}/campaigns", p)
    for r in rows:
        r["status"] = r.get("effective_status")
        r["daily"] = fmt_money(r.get("daily_budget"), cur)
        r["lifetime"] = fmt_money(r.get("lifetime_budget"), cur)
    table(rows, ["id", "name", "status", "objective", "daily", "lifetime"])


def cmd_adsets(args):
    acc = account_id(args)
    cur = currency(acc)
    parent = args.campaign or acc
    p = {"fields": "id,name,effective_status,campaign_id,optimization_goal,daily_budget,lifetime_budget"}
    p.update(status_filter(args))
    rows = get_all(f"{parent}/adsets", p)
    for r in rows:
        r["status"] = r.get("effective_status")
        r["goal"] = r.get("optimization_goal")
        r["daily"] = fmt_money(r.get("daily_budget"), cur)
        r["lifetime"] = fmt_money(r.get("lifetime_budget"), cur)
    table(rows, ["id", "name", "status", "campaign_id", "goal", "daily", "lifetime"])


def cmd_ads(args):
    acc = account_id(args)
    parent = args.adset or args.campaign or acc
    p = {"fields": "id,name,effective_status,adset_id,campaign_id"}
    p.update(status_filter(args))
    rows = get_all(f"{parent}/ads", p)
    for r in rows:
        r["status"] = r.get("effective_status")
    table(rows, ["id", "name", "status", "adset_id", "campaign_id"])


def cmd_insights(args):
    acc = account_id(args)
    cur = currency(acc)
    level = args.level
    p = {
        "level": level,
        "fields": f"{level}_id,{level}_name,spend,impressions,clicks,ctr,cpc,actions,action_values",
        "limit": 200,
    }
    if args.since:
        p["time_range"] = {"since": args.since, "until": args.until or args.since}
        period = f"{args.since} → {args.until or args.since}"
    else:
        p["date_preset"] = args.date
        period = args.date
    rows = get_all(f"{args.id or acc}/insights", p, limit=5000)

    data = []
    for r in rows:
        data.append({
            "id": r.get(f"{level}_id"),
            "name": r.get(f"{level}_name"),
            "spend": float(r.get("spend", 0) or 0),
            "impr": int(r.get("impressions", 0) or 0),
            "clicks": int(r.get("clicks", 0) or 0),
            "ctr": float(r.get("ctr", 0) or 0),
            "cpc": float(r.get("cpc", 0) or 0),
            "purch": pick(r.get("actions")),
            "rev": pick(r.get("action_values")),
        })
    data.sort(key=lambda x: x["spend"], reverse=True)

    out, ts, tp, tr = [], 0.0, 0.0, 0.0
    for d in data:
        ts, tp, tr = ts + d["spend"], tp + d["purch"], tr + d["rev"]
        out.append({
            "id": d["id"],
            "name": d["name"],
            "spend": f"{d['spend']:,.2f}",
            "impr": f"{d['impr']:,}",
            "clicks": f"{d['clicks']:,}",
            "ctr%": f"{d['ctr']:.2f}",
            "cpc": f"{d['cpc']:.2f}",
            "purch": f"{int(d['purch'])}",
            "cpa": f"{d['spend'] / d['purch']:.2f}" if d["purch"] else "-",
            "roas": f"{d['rev'] / d['spend']:.2f}" if d["spend"] else "-",
        })
    print(f"Dönem: {period} | Seviye: {level} | Para birimi: {cur}\n")
    table(out, ["id", "name", "spend", "impr", "clicks", "ctr%", "cpc", "purch", "cpa", "roas"])
    roas = f"{tr / ts:.2f}" if ts else "-"
    cpa = f"{ts / tp:,.2f}" if tp else "-"
    print(f"\nToplam: harcama {ts:,.2f} {cur} | satın alma {int(tp)} | CPA {cpa} | gelir {tr:,.2f} | ROAS {roas}")


def cmd_status(args):
    status = "PAUSED" if args.cmd == "pause" else "ACTIVE"
    names = {oid: api("GET", oid, {"fields": "name"}).get("name", "?") for oid in args.ids}
    lines = "\n".join(f"  {oid}  {n}" for oid, n in names.items())
    confirm(args, f"{status} yapılacak:\n{lines}")
    for oid in args.ids:
        res = api("POST", oid, {"status": status})
        print(f"✓ {oid} → {status}" if res.get("success") else f"? {oid}: {res}")


def cmd_budget(args):
    info = api("GET", args.id, {"fields": "name,daily_budget,lifetime_budget,account_id"})
    cur = currency(f"act_{info['account_id']}")
    daily, life = info.get("daily_budget"), info.get("lifetime_budget")
    has_d, has_l = has_value(daily), has_value(life)
    if not (has_d or has_l):
        die("Bu nesnede bütçe yok. CBO açıksa bütçe kampanyada, kapalıysa reklam setinde.")

    if args.pct is not None:
        field = "daily_budget" if has_d else "lifetime_budget"
        old = int(info[field])
        new = int(round(old * (1 + args.pct / 100)))
    else:
        field = "daily_budget" if args.daily is not None else "lifetime_budget"
        if field == "daily_budget" and not has_d:
            die("Bu nesne ömür boyu bütçe kullanıyor, --lifetime kullan.")
        if field == "lifetime_budget" and not has_l:
            die("Bu nesne günlük bütçe kullanıyor, --daily kullan.")
        old = int(info[field])
        amount = args.daily if args.daily is not None else args.lifetime
        new = int(round(amount * offset(cur)))

    change = (new - old) / old * 100 if old else 0
    confirm(args, f"{info.get('name')}\n  {field}: {fmt_money(old, cur)} → {fmt_money(new, cur)} ({change:+.0f}%)")
    res = api("POST", args.id, {field: new})
    print("✓ Bütçe güncellendi" if res.get("success") else res)


def cmd_rename(args):
    old = api("GET", args.id, {"fields": "name"}).get("name")
    confirm(args, f"İsim: {old} → {args.name}")
    res = api("POST", args.id, {"name": args.name})
    print("✓ İsim güncellendi" if res.get("success") else res)


def cmd_copy(args):
    name = api("GET", args.id, {"fields": "name"}).get("name")
    p = {"status_option": "PAUSED"}
    if args.deep:
        p["deep_copy"] = True
    confirm(args, f"Kopyalanacak (duraklatılmış olarak): {name}{' + alt öğeler' if args.deep else ''}")
    res = api("POST", f"{args.id}/copies", p)
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_raw(args):
    params = {}
    for kv in args.params:
        if "=" not in kv:
            die(f"Parametre anahtar=değer olmalı: {kv}")
        k, v = kv.split("=", 1)
        params[k] = v
    method = args.method.upper()
    if method != "GET":
        confirm(args, f"{method} {args.path} {params}")
    res = api(method, args.path, params)
    print(json.dumps(res, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- CLI

def main():
    load_env_file()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-a", "--account", help="Reklam hesabı (act_XXX), varsayılanı ezer")
    common.add_argument("-y", "--yes", action="store_true", help="Onay sormadan uygula")

    ap = argparse.ArgumentParser(prog="metaads", description="Meta reklam yönetimi CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        sp = sub.add_parser(name, parents=[common], help=help_text)
        sp.set_defaults(fn=fn)
        return sp

    add("accounts", cmd_accounts, "Reklam hesaplarını listele")

    p = add("use", cmd_use, "Varsayılan reklam hesabını ayarla")
    p.add_argument("account_id")

    p = add("campaigns", cmd_campaigns, "Kampanyaları listele")
    p.add_argument("--all", action="store_true", help="Arşivlenmiş/silinmiş dahil")

    p = add("adsets", cmd_adsets, "Reklam setlerini listele")
    p.add_argument("--campaign", help="Sadece bu kampanyanın setleri")
    p.add_argument("--all", action="store_true")

    p = add("ads", cmd_ads, "Reklamları listele")
    p.add_argument("--adset", help="Sadece bu setin reklamları")
    p.add_argument("--campaign", help="Sadece bu kampanyanın reklamları")
    p.add_argument("--all", action="store_true")

    p = add("insights", cmd_insights, "Performans raporu")
    p.add_argument("--level", choices=["account", "campaign", "adset", "ad"], default="campaign")
    p.add_argument("--date", choices=DATE_PRESETS, default="last_7d")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--id", help="Sadece bu kampanya/set/reklam altında")

    for name, h in (("pause", "Duraklat"), ("activate", "Aktifleştir")):
        p = add(name, cmd_status, f"{h} (kampanya/set/reklam ID)")
        p.add_argument("ids", nargs="+")

    p = add("budget", cmd_budget, "Bütçe değiştir (kampanya veya set)")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", type=float, help="Yeni günlük bütçe (örn. 250)")
    g.add_argument("--lifetime", type=float, help="Yeni ömür boyu bütçe")
    g.add_argument("--pct", type=float, help="Yüzde değişim (örn. 20 veya -15)")

    p = add("rename", cmd_rename, "İsim değiştir")
    p.add_argument("id")
    p.add_argument("name")

    p = add("copy", cmd_copy, "Kopyala (duraklatılmış olarak)")
    p.add_argument("id")
    p.add_argument("--deep", action="store_true", help="Alt setler/reklamlarla birlikte")

    p = add("raw", cmd_raw, "Ham Graph API çağrısı")
    p.add_argument("method", choices=["GET", "POST", "DELETE", "get", "post", "delete"])
    p.add_argument("path", help="örn. act_123/campaigns")
    p.add_argument("params", nargs="*", help="anahtar=değer")

    args = ap.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        die("İptal.")


if __name__ == "__main__":
    main()
