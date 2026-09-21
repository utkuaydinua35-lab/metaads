#!/usr/bin/env python3
"""
metaads_export v2 — Detaylı analiz için tüm reklam verisini tek JSON dosyasına döker.
metaads.py ile aynı klasörde durmalı (token/hesap ayarlarını ondan kullanır).

  python3 metaads_export.py                  # tüm aktif hesaplar, son 30 gün
  python3 metaads_export.py -a act_XXXX      # tek hesap
  python3 metaads_export.py --date last_90d

v2: kitleler, piksel/CAPI teşhisi, 30 günlük gerçek frekans, kampanya ve set
toplamları, düzeltilmiş reklam/kreatif çekimi, tüm hesapların listesi.
"""
import argparse
import json
import sys
import time
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


def safe(label, fn):
    """Bir bölüm hata verirse tüm export'u öldürme, hatayı kaydet."""
    print(f"  · {label}...", end="", flush=True)
    try:
        res = fn()
        n = len(res) if isinstance(res, list) else 1
        print(f" {n}")
        return res
    except SystemExit:
        print(" HATA (atlandı)")
        return {"error": f"{label} çekilemedi"}


def first_ok(label, attempts):
    """Sırayla dener; ilk başarılı sonucu döner (ağır alanlar hata verirse hafife düşer)."""
    print(f"  · {label}...", end="", flush=True)
    for i, fn in enumerate(attempts):
        try:
            res = fn()
            n = len(res) if isinstance(res, list) else 1
            print(f" {n}" + (f" (sade mod {i})" if i else ""))
            return res
        except SystemExit:
            continue
    print(" HATA (atlandı)")
    return {"error": f"{label} çekilemedi"}


def period(args):
    if args.since:
        return {"time_range": {"since": args.since, "until": args.until or args.since}}
    return {"date_preset": args.date}


def insights(acc, p, **params):
    return lambda: m.get_all(f"{acc}/insights", {**params, **p}, limit=10000)


def pixel_section(acc):
    pixels = safe("pikseller", lambda: m.get_all(f"{acc}/adspixels", {"fields": PIXEL_FIELDS}))
    if not isinstance(pixels, list):
        return pixels
    now = int(time.time())
    week = {"start_time": now - 7 * 86400, "end_time": now}
    for px in pixels:
        pid = px["id"]
        stats = {}
        for agg in ("event", "event_source", "match_keys", "device_os", "event_detection_method"):
            stats[agg] = safe(f"piksel {pid} · {agg}", lambda a=agg: m.get_all(
                f"{pid}/stats", {"aggregation": a, **week}, limit=2000))
        px["stats_7d"] = stats
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
        "effective_status": m.LIVE_STATUSES, "limit": 50}))
    out["creatives"] = first_ok("kreatifler", [
        (lambda f=f: m.get_all(f"{acc}/adcreatives", {"fields": f, "limit": 25}, limit=2000))
        for f in CREATIVE_FIELD_SETS])
    out["audiences"] = first_ok("özel kitleler", [
        lambda: m.get_all(f"{acc}/customaudiences", {"fields": AUDIENCE_FIELDS, "limit": 50}),
        lambda: m.get_all(f"{acc}/customaudiences", {
            "fields": "id,name,subtype,delivery_status,operation_status,retention_days,"
                      "lookalike_spec,time_updated", "limit": 50}),
    ])
    out["saved_audiences"] = safe("kayıtlı kitleler", lambda: m.get_all(
        f"{acc}/saved_audiences", {"fields": "id,name,targeting,time_updated"}))
    out["pixels"] = pixel_section(acc)

    out["totals"] = safe("30 günlük toplam (gerçek erişim/frekans)",
                         insights(acc, p, level="account", fields=PERF))
    out["insights_campaigns"] = safe("kampanya performansı", insights(
        acc, p, level="campaign", fields="campaign_id,campaign_name," + PERF))
    out["insights_adsets"] = safe("set performansı", insights(
        acc, p, level="adset", fields="campaign_id,campaign_name,adset_id,adset_name," + PERF))
    out["insights_ads"] = safe("reklam performansı", insights(
        acc, p, level="ad", fields=AD_INSIGHT_FIELDS))
    out["insights_adsets_daily"] = safe("günlük set performansı", insights(
        acc, p, level="adset", fields=DAILY_FIELDS, time_increment=1))
    out["insights_account_daily"] = safe("günlük hesap toplamı", insights(
        acc, p, level="account", fields=BREAKDOWN_FIELDS + ",frequency", time_increment=1))

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
        out[key] = safe(key.replace("_", " "), insights(acc, p, level=level, fields=fields, breakdowns=bd))

    out["campaigns_90d"] = safe("90 günlük kampanya geçmişi", lambda: m.get_all(
        f"{acc}/insights", {"level": "campaign", "date_preset": "last_90d",
                            "fields": "campaign_id,campaign_name," + BREAKDOWN_FIELDS + ",frequency"},
        limit=5000))
    out["account_weekly_90d"] = safe("90 günlük haftalık trend", lambda: m.get_all(
        f"{acc}/insights", {"level": "account", "date_preset": "last_90d", "time_increment": 7,
                            "fields": BREAKDOWN_FIELDS + ",frequency"}))
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

    result = {
        "export_version": 2,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "api_version": m.API_VERSION,
        "period": period(args),
        "all_accounts": all_accounts,
        "accounts": [export_account(a, args) for a in accounts],
    }
    out = Path(args.out or f"meta_export_{datetime.now():%Y%m%d_%H%M}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n✓ {out.resolve()}  ({out.stat().st_size / 1024:,.0f} KB)")
    print("Bu dosyayı Claude'a yükle. İçinde token yok.")


if __name__ == "__main__":
    main()
