#!/usr/bin/env python3
"""
metaads_export v3 — Detaylı analiz için tüm reklam verisini tek JSON dosyasına döker.
metaads.py ile aynı klasörde durmalı (token/hesap ayarlarını ondan kullanır).

  python3 metaads_export.py                  # tüm aktif hesaplar, son 30 gün
  python3 metaads_export.py -a act_XXXX      # tek hesap
  python3 metaads_export.py --date last_90d

v3: sadece yayındaki reklamların kreatifleri toplu çekilir, sadece son 30 günde
olay almış pikseller sorgulanır, performans sorguları paralel çalışır.
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metaads as m  # noqa: E402

PERF = (
    "spend,impressions,reach,frequency,clicks,inline_link_clicks,ctr,inline_link_click_ctr,"
    "cpm,cpc,actions,action_values,cost_per_action_type"
)
AD_INSIGHT_FIELDS = (
    "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name," + PERF + ","
    "quality_ranking,engagement_rate_ranking,conversion_rate_ranking,"
    "video_play_actions,video_p25_watched_actions,video_p50_watched_actions,video_p100_watched_actions"
)
DAILY_FIELDS = (
    "campaign_id,campaign_name,adset_id,adset_name,spend,impressions,reach,frequency,"
    "inline_link_clicks,cpm,actions,action_values"
)
BREAKDOWN_FIELDS = "spend,impressions,reach,inline_link_clicks,ctr,cpm,actions,action_values"
AUDIENCE_FIELDS = (
    "id,name,subtype,description,approximate_count_lower_bound,approximate_count_upper_bound,"
    "delivery_status,operation_status,retention_days,rule,lookalike_spec,data_source,"
    "time_created,time_updated"
)
PIXEL_FIELDS = (
    "id,name,last_fired_time,is_unavailable,creation_time,"
    "enable_automatic_matching,automatic_matching_fields,first_party_cookie_status"
)
CREATIVE_FIELD_SETS = [
    "id,name,title,body,object_type,call_to_action_type,thumbnail_url,video_id,"
    "product_set_id,link_url,object_story_spec",
    "id,name,title,body,object_type,call_to_action_type,thumbnail_url,video_id,link_url",
    "id,name,object_type",
]


WORKERS = 6
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def safe(label, fn):
    """Bir bölüm hata verirse tüm export'u öldürme, hatayı kaydet."""
    t = time.time()
    try:
        res = fn()
        n = len(res) if isinstance(res, list) else 1
        log(f"  · {label}: {n}  ({time.time() - t:.1f}s)")
        return res
    except SystemExit:
        log(f"  · {label}: HATA (atlandı)")
        return {"error": f"{label} çekilemedi"}


def first_ok(label, attempts):
    """Sırayla dener; ilk başarılı sonucu döner (ağır alanlar hata verirse hafife düşer)."""
    t = time.time()
    for i, fn in enumerate(attempts):
        try:
            res = fn()
            n = len(res) if isinstance(res, list) else 1
            log(f"  · {label}: {n}" + (f" (sade mod {i})" if i else "") + f"  ({time.time() - t:.1f}s)")
            return res
        except SystemExit:
            continue
    log(f"  · {label}: HATA (atlandı)")
    return {"error": f"{label} çekilemedi"}


def run_parallel(jobs):
    """jobs: {anahtar: (etiket, fn)} → {anahtar: sonuç}, WORKERS kadar eşzamanlı."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {k: ex.submit(safe, label, fn) for k, (label, fn) in jobs.items()}
        return {k: f.result() for k, f in futs.items()}


def fetch_by_ids(ids, fields, chunk=50):
    """Belirli ID'leri ?ids= ile toplu çeker (sayfa sayfa gezmekten çok daha hızlı)."""
    out = []
    ids = list(dict.fromkeys(i for i in ids if i))
    for i in range(0, len(ids), chunk):
        res = m.api("GET", "", {"ids": ",".join(ids[i:i + chunk]), "fields": fields})
        out.extend(res.values())
    return out


def creatives_for(ads):
    ids = [a.get("creative", {}).get("id") for a in ads] if isinstance(ads, list) else []
    if not ids:
        return []
    return first_ok("kreatifler (yayındaki reklamlar)", [
        (lambda f=f: fetch_by_ids(ids, f, chunk=25 if "object_story_spec" in f else 50))
        for f in CREATIVE_FIELD_SETS])


def period(args):
    if args.since:
        return {"time_range": {"since": args.since, "until": args.until or args.since}}
    return {"date_preset": args.date}


def insights(acc, p, **params):
    return lambda: m.get_all(f"{acc}/insights", {**params, **p}, limit=10000)


def recently_fired(px, days=30):
    lf = px.get("last_fired_time")
    if not lf:
        return False
    try:
        ts = datetime.strptime(lf[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return True
    return time.time() - ts < days * 86400


def pixel_section(acc):
    pixels = safe("pikseller", lambda: m.get_all(f"{acc}/adspixels", {"fields": PIXEL_FIELDS}))
    if not isinstance(pixels, list):
        return pixels
    now = int(time.time())
    week = {"start_time": now - 7 * 86400, "end_time": now}
    jobs = {}
    for px in pixels:
        if not recently_fired(px):
            px["stats_7d"] = "son 30 günde olay yok, sorgulanmadı"
            continue
        for agg in ("event", "event_source", "match_keys", "device_os", "event_detection_method"):
            jobs[(px["id"], agg)] = (f"piksel {px['id']} · {agg}", lambda p=px["id"], a=agg: m.get_all(
                f"{p}/stats", {"aggregation": a, **week}, limit=2000))
    res = run_parallel(jobs)
    for px in pixels:
        if isinstance(px.get("stats_7d"), str):
            continue
        px["stats_7d"] = {agg: res[(px["id"], agg)] for (pid, agg) in res if pid == px["id"]}
    dead = sum(1 for px in pixels if isinstance(px.get("stats_7d"), str))
    if dead:
        log(f"  · {dead} piksel son 30 günde olay almamış, atlandı")
    return pixels


def export_account(acc, args):
    p = period(args)
    print(f"\n▸ {acc}")
    out = {"id": acc}
    out["account"] = safe("hesap bilgisi", lambda: m.api("GET", acc, {
        "fields": "name,currency,timezone_name,account_status,disable_reason,amount_spent,"
                  "spend_cap,balance,created_time,funding_source_details,is_prepay_account"}))
    out["campaigns"] = safe("kampanyalar", lambda: m.get_all(f"{acc}/campaigns", {
        "fields": "id,name,objective,effective_status,buying_type,bid_strategy,daily_budget,"
                  "lifetime_budget,special_ad_categories,smart_promotion_type,start_time,created_time",
        "effective_status": m.LIVE_STATUSES}))
    out["adsets"] = safe("reklam setleri", lambda: m.get_all(f"{acc}/adsets", {
        "fields": "id,name,campaign_id,effective_status,daily_budget,lifetime_budget,"
                  "optimization_goal,billing_event,bid_strategy,bid_amount,attribution_spec,"
                  "targeting,promoted_object,destination_type,learning_stage_info,"
                  "start_time,created_time",
        "effective_status": m.LIVE_STATUSES}))
    out["ads"] = safe("reklamlar", lambda: m.get_all(f"{acc}/ads", {
        "fields": "id,name,adset_id,campaign_id,effective_status,created_time,"
                  "issues_info,ad_review_feedback,creative{id}",
        "effective_status": m.LIVE_STATUSES, "limit": 100}))
    out["creatives"] = creatives_for(out["ads"])
    out["audiences"] = first_ok("özel kitleler", [
        lambda: m.get_all(f"{acc}/customaudiences", {"fields": AUDIENCE_FIELDS, "limit": 50}),
        lambda: m.get_all(f"{acc}/customaudiences", {
            "fields": "id,name,subtype,delivery_status,operation_status,retention_days,"
                      "lookalike_spec,time_updated", "limit": 50}),
    ])
    out["pixels"] = pixel_section(acc)

    jobs = {
        "saved_audiences": ("kayıtlı kitleler", lambda: m.get_all(
            f"{acc}/saved_audiences", {"fields": "id,name,targeting,time_updated"})),
        "totals": ("30 günlük toplam (gerçek erişim/frekans)",
                   insights(acc, p, level="account", fields=PERF)),
        "insights_campaigns": ("kampanya performansı", insights(
            acc, p, level="campaign", fields="campaign_id,campaign_name," + PERF)),
        "insights_adsets": ("set performansı", insights(
            acc, p, level="adset", fields="campaign_id,campaign_name,adset_id,adset_name," + PERF)),
        "insights_ads": ("reklam performansı", insights(acc, p, level="ad", fields=AD_INSIGHT_FIELDS)),
        "insights_adsets_daily": ("günlük set performansı", insights(
            acc, p, level="adset", fields=DAILY_FIELDS, time_increment=1)),
        "insights_account_daily": ("günlük hesap toplamı", insights(
            acc, p, level="account", fields=BREAKDOWN_FIELDS + ",frequency", time_increment=1)),
        "campaigns_90d": ("90 günlük kampanya geçmişi", lambda: m.get_all(
            f"{acc}/insights", {"level": "campaign", "date_preset": "last_90d",
                                "fields": "campaign_id,campaign_name," + BREAKDOWN_FIELDS + ",frequency"},
            limit=5000)),
        "account_weekly_90d": ("90 günlük haftalık trend", lambda: m.get_all(
            f"{acc}/insights", {"level": "account", "date_preset": "last_90d", "time_increment": 7,
                                "fields": BREAKDOWN_FIELDS + ",frequency"})),
    }
    for key, bd, level in (
        ("breakdown_placement", "publisher_platform,platform_position", "account"),
        ("breakdown_age_gender", "age,gender", "account"),
        ("breakdown_device", "impression_device", "account"),
        ("breakdown_region", "region", "account"),
        ("breakdown_hour", "hourly_stats_aggregated_by_advertiser_time_zone", "account"),
        ("breakdown_adset_age_gender", "age,gender", "adset"),
        ("breakdown_adset_placement", "publisher_platform,platform_position", "adset"),
    ):
        fields = BREAKDOWN_FIELDS if level == "account" else "adset_id,adset_name," + BREAKDOWN_FIELDS
        jobs[key] = (key.replace("_", " "), insights(acc, p, level=level, fields=fields, breakdowns=bd))
    out.update(run_parallel(jobs))
    return out


def main():
    m.load_env_file()
    ap = argparse.ArgumentParser(prog="metaads_export")
    ap.add_argument("-a", "--account", help="Tek hesap (act_XXX). Verilmezse tüm aktif hesaplar.")
    ap.add_argument("--date", choices=m.DATE_PRESETS, default="last_30d")
    ap.add_argument("--since", help="YYYY-MM-DD")
    ap.add_argument("--until", help="YYYY-MM-DD")
    ap.add_argument("-o", "--out", help="Çıktı dosyası")
    args = ap.parse_args()

    all_accounts = m.get_all("me/adaccounts", {
        "fields": "account_id,name,account_status,currency,timezone_name,amount_spent,business"})
    if args.account:
        accounts = [args.account if args.account.startswith("act_") else f"act_{args.account}"]
    else:
        accounts = [f"act_{r['account_id']}" for r in all_accounts if r.get("account_status") == 1]
        print(f"Aktif hesaplar: {len(accounts)}")
        for r in all_accounts:
            if r.get("account_status") == 1:
                print(f"  act_{r['account_id']}  {r.get('name')}")
    if not accounts:
        m.die("Aktif reklam hesabı bulunamadı.")

    t0 = time.time()
    result = {
        "export_version": 3,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "api_version": m.API_VERSION,
        "period": period(args),
        "all_accounts": all_accounts,
        "accounts": [export_account(a, args) for a in accounts],
    }
    out = Path(args.out or f"meta_export_{datetime.now():%Y%m%d_%H%M}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n✓ {out.resolve()}  ({out.stat().st_size / 1024:,.0f} KB, {time.time() - t0:.0f} sn)")
    print("Bu dosyayı Claude'a yükle. İçinde token yok.")


if __name__ == "__main__":
    main()
