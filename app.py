import json
import os
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session

from direct_client import (
    get_campaigns, get_campaigns_by_ids, get_campaign_stats, get_keyword_stats,
    get_negatives, update_campaign_negatives, update_adgroup_negatives, get_search_queries,
    get_keywords_with_stats, get_keyword_bids, set_keyword_bid,
    get_campaign_goals, get_goal_period_stats,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-on-server")

DIRECT_TOKEN = os.getenv("DIRECT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

_HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(_HERE, "history.json")

SYSTEM_PROMPT = """Ты опытный аналитик рекламы в Яндекс.Директ. Помогаешь принимать решения на основе данных: какие кампании эффективны, где тратится бюджет впустую, как снизить стоимость клика и увеличить конверсии.

Стиль ответа: конкретно, с цифрами из данных, без воды. Называй конкретные кампании по имени. Сначала коротко — что работает, потом — 3-5 чётких рекомендаций с обоснованием и конкретными цифрами."""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if DASHBOARD_PASSWORD and not session.get("authenticated"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def _load_all_history() -> list:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def load_history(campaign_id=None) -> list:
    all_h = _load_all_history()
    if campaign_id is None:
        return [h for h in all_h if not h.get("campaign_id")]
    return [h for h in all_h if h.get("campaign_id") == campaign_id]


def _extract_title(analysis: str) -> str:
    for line in analysis.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:100]
    return "Анализ"


def save_to_history(analysis: str, question: str = "", campaign_id=None):
    all_h = _load_all_history()
    title = question.strip() if question.strip() else _extract_title(analysis)
    all_h.append({
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "analysis": analysis,
        "title": title,
        "is_question": bool(question.strip()),
        "campaign_id": campaign_id,
    })
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(all_h, f, ensure_ascii=False, indent=2)


def calc_stats(campaigns: list, stats: dict) -> dict:
    if not campaigns:
        return {}
    total_impressions = sum(stats.get(c["Id"], {}).get("impressions", 0) for c in campaigns)
    total_clicks = sum(stats.get(c["Id"], {}).get("clicks", 0) for c in campaigns)
    total_cost = sum(stats.get(c["Id"], {}).get("cost", 0.0) for c in campaigns)
    return {
        "total_impressions": total_impressions,
        "total_clicks": total_clicks,
        "total_cost": round(total_cost, 2),
        "ctr": round(total_clicks / total_impressions * 100, 2) if total_impressions else 0,
        "cpc": round(total_cost / total_clicks, 2) if total_clicks else 0,
        "count": len(campaigns),
    }


MASTER_CAMPAIGN_TYPES = {"UNIFIED_CAMPAIGN", "SMART_CAMPAIGN"}


def _ctr_class(ctr: float, impressions: int, campaign_type: str) -> str:
    if impressions <= 100:
        return "mid"
    if campaign_type in MASTER_CAMPAIGN_TYPES:
        if ctr < 0.15:
            return "low"
        if ctr > 2.0:
            return "high"
    else:
        if ctr < 1.0:
            return "low"
        if ctr > 5.0:
            return "high"
    return "mid"


def _enrich_stats(campaign_stats: dict) -> None:
    """Add cpc, ctr_class, cost_trend, ctr_trend to each campaign entry in-place."""
    for s in campaign_stats.values():
        s["cpc"] = round(s["cost"] / s["clicks"], 2) if s["clicks"] else 0

        # ctr_class is set to a default here; caller should override with campaign type info
        ctr = s["ctr"]
        if s["impressions"] > 100 and ctr < 1.0:
            s["ctr_class"] = "low"
        elif ctr > 5.0:
            s["ctr_class"] = "high"
        else:
            s["ctr_class"] = "mid"

        last = s["last7d_cost"]
        prev = s["prev7d_cost"]
        if prev == 0 and last == 0:
            s["cost_trend"] = "flat"
            s["cost_trend_pct"] = 0
        elif prev == 0:
            s["cost_trend"] = "up"
            s["cost_trend_pct"] = 100
        else:
            change = (last - prev) / prev * 100
            s["cost_trend"] = "up" if change > 10 else ("down" if change < -10 else "flat")
            s["cost_trend_pct"] = round(abs(change))

        last7d_ctr = (
            round(s["last7d_clicks"] / s["last7d_impressions"] * 100, 2)
            if s["last7d_impressions"] else 0
        )
        prev7d_ctr = (
            round(s["prev7d_clicks"] / s["prev7d_impressions"] * 100, 2)
            if s["prev7d_impressions"] else 0
        )
        s["last7d_ctr"] = last7d_ctr
        s["prev7d_ctr"] = prev7d_ctr
        if last7d_ctr > prev7d_ctr + 0.1:
            s["ctr_trend"] = "up"
        elif last7d_ctr < prev7d_ctr - 0.1:
            s["ctr_trend"] = "down"
        else:
            s["ctr_trend"] = "flat"


def _build_alerts(campaigns: list, campaign_stats: dict) -> list[dict]:
    alerts = []
    for c in campaigns:
        s = campaign_stats.get(c["Id"], {})
        state = c.get("State", "")
        ctype = c.get("Type", "")
        name = c["Name"]
        is_master = ctype in MASTER_CAMPAIGN_TYPES

        if state == "ON" and s.get("impressions", 0) == 0:
            alerts.append({"type": "red", "msg": f"Кампания «{name}» активна, но нет показов за 30 дней"})

        ctr = s.get("ctr", 0)
        impr = s.get("impressions", 0)
        if is_master:
            crit_threshold, warn_threshold = 0.1, 0.2
        else:
            crit_threshold, warn_threshold = 0.5, 1.0
        if impr > 1000 and ctr < crit_threshold:
            alerts.append({"type": "red", "msg": f"Кампания «{name}» — CTR {ctr}% (критически низкий)"})
        elif impr > 1000 and ctr < warn_threshold:
            alerts.append({"type": "orange", "msg": f"Кампания «{name}» — CTR {ctr}% (ниже нормы)"})

        if s.get("cost_trend") == "up" and s.get("cost_trend_pct", 0) >= 100:
            alerts.append({"type": "orange", "msg": f"Кампания «{name}» — расход вырос в 2+ раза за последние 7 дней"})

        prev7d_ctr = s.get("prev7d_ctr", 0)
        last7d_ctr = s.get("last7d_ctr", 0)
        if state == "ON" and prev7d_ctr > 0 and (prev7d_ctr - last7d_ctr) / prev7d_ctr > 0.3:
            alerts.append({
                "type": "orange",
                "msg": f"Кампания «{name}» — CTR упал с {prev7d_ctr}% до {last7d_ctr}% за последние 7 дней",
            })

    return alerts


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        error = "Неверный пароль"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    campaigns = []
    campaign_stats = {}
    daily_stats = {}
    error = None
    days = min(int(request.args.get("days", 30)), 365)
    try:
        campaign_stats, daily_stats = get_campaign_stats(DIRECT_TOKEN, [], days=days)
        api_by_id = {c["Id"]: c for c in get_campaigns(DIRECT_TOKEN)}
        missing_ids = [cid for cid in campaign_stats if cid not in api_by_id]
        if missing_ids:
            for c in get_campaigns_by_ids(DIRECT_TOKEN, missing_ids):
                api_by_id[c["Id"]] = c
        for cid, s in campaign_stats.items():
            meta = api_by_id.get(cid, {})
            campaigns.append({
                "Id": cid,
                "Name": s["name"],
                "Status": meta.get("Status", ""),
                "State": meta.get("State", ""),
                "Type": meta.get("Type", ""),
                "StartDate": meta.get("StartDate", ""),
            })
    except Exception as e:
        error = str(e)

    campaigns.sort(key=lambda c: -campaign_stats.get(c["Id"], {}).get("cost", 0))
    _enrich_stats(campaign_stats)
    alerts = _build_alerts(campaigns, campaign_stats)

    sorted_days = sorted(daily_stats.items())
    chart_labels = [d for d, _ in sorted_days]
    chart_costs = [v["cost"] for _, v in sorted_days]
    chart_clicks = [v["clicks"] for _, v in sorted_days]

    this_week_cost = round(sum(v["cost"] for _, v in sorted_days[-7:]), 2) if sorted_days else 0
    prev_week_cost = round(sum(v["cost"] for _, v in sorted_days[-14:-7]), 2) if len(sorted_days) >= 8 else 0
    this_week_clicks = sum(v["clicks"] for _, v in sorted_days[-7:]) if sorted_days else 0
    prev_week_clicks = sum(v["clicks"] for _, v in sorted_days[-14:-7]) if len(sorted_days) >= 8 else 0

    campaigns_js = []
    for c in campaigns:
        s = campaign_stats.get(c["Id"], {})
        ctype = c.get("Type", "")
        campaigns_js.append({
            "id": c["Id"],
            "name": c["Name"],
            "state": c.get("State", ""),
            "status": c.get("Status", ""),
            "type": ctype,
            "impressions": s.get("impressions", 0),
            "clicks": s.get("clicks", 0),
            "ctr": s.get("ctr", 0),
            "ctr_class": _ctr_class(s.get("ctr", 0), s.get("impressions", 0), ctype),
            "cost": s.get("cost", 0),
            "cpc": s.get("cpc", 0),
            "cost_trend": s.get("cost_trend", "flat"),
            "cost_trend_pct": s.get("cost_trend_pct", 0),
            "ctr_trend": s.get("ctr_trend", "flat"),
        })

    stats = calc_stats(campaigns, campaign_stats)
    history = load_history()

    return render_template(
        "index.html",
        campaigns=campaigns,
        campaign_stats=campaign_stats,
        stats=stats,
        history=history,
        error=error,
        alerts=alerts,
        days=days,
        campaigns_json=json.dumps(campaigns_js, ensure_ascii=False),
        chart_labels=json.dumps(chart_labels),
        chart_costs=json.dumps(chart_costs),
        chart_clicks=json.dumps(chart_clicks),
        this_week_cost=this_week_cost,
        prev_week_cost=prev_week_cost,
        this_week_clicks=this_week_clicks,
        prev_week_clicks=prev_week_clicks,
    )


@app.route("/api/debug-states")
@login_required
def debug_states():
    campaign_stats, _ = get_campaign_stats(DIRECT_TOKEN, [])
    api_by_id = {c["Id"]: c for c in get_campaigns(DIRECT_TOKEN)}
    missing_ids = [cid for cid in campaign_stats if cid not in api_by_id]
    if missing_ids:
        for c in get_campaigns_by_ids(DIRECT_TOKEN, missing_ids):
            api_by_id[c["Id"]] = c
    rows = []
    for cid, s in campaign_stats.items():
        meta = api_by_id.get(cid, {})
        rows.append({
            "id": cid,
            "name": s["name"][:40],
            "state": meta.get("State", "—MISSING—"),
            "status": meta.get("Status", "—MISSING—"),
            "type": meta.get("Type", "—MISSING—"),
            "in_api": cid in api_by_id,
        })
    rows.sort(key=lambda r: r["state"])
    return jsonify(rows)


@app.route("/api/debug-campaigns")
@login_required
def debug_campaigns():
    from direct_client import get_campaigns, get_campaigns_by_ids, get_campaign_stats
    campaigns = get_campaigns(DIRECT_TOKEN)
    stats, _ = get_campaign_stats(DIRECT_TOKEN, [])
    known_ids = {c["Id"] for c in campaigns}
    missing = [cid for cid in stats if cid not in known_ids]
    by_ids = get_campaigns_by_ids(DIRECT_TOKEN, missing) if missing else []
    all_campaigns = campaigns + by_ids
    all_ids = {c["Id"] for c in all_campaigns}
    matched = known_ids & set(stats.keys())
    matched_after = all_ids & set(stats.keys())
    return jsonify({
        "campaigns_from_list": len(campaigns),
        "campaigns_fetched_by_id": len(by_ids),
        "total_after_merge": len(all_campaigns),
        "stats_campaigns_count": len(stats),
        "matched_before": len(matched),
        "matched_after": len(matched_after),
        "impressions_before": sum(stats[cid]["impressions"] for cid in matched),
        "impressions_after": sum(stats[cid]["impressions"] for cid in matched_after),
        "fetched_by_id_sample": [{"id": c["Id"], "name": c["Name"][:30]} for c in by_ids[:5]],
    })


@app.route("/api/debug-stats")
@login_required
def debug_stats():
    import requests as _requests
    from datetime import datetime, timedelta
    from direct_client import DIRECT_API_URL, _headers
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    payload = {
        "method": "get",
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": ["CampaignId", "Date", "Impressions", "Clicks", "Cost", "Ctr"],
            "ReportName": f"dbg_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    }
    headers = _headers(DIRECT_TOKEN)
    headers["processingMode"] = "auto"
    headers["returnMoneyInMicros"] = "false"
    sess = _requests.Session()
    sess.trust_env = False
    resp = sess.post(DIRECT_API_URL + "reports", json=payload, headers=headers)
    lines = resp.text.strip().split("\n")
    return jsonify({
        "http_status": resp.status_code,
        "total_lines": len(lines),
        "header": lines[0] if lines else "",
        "first_5_rows": lines[1:6],
        "last_2_rows": lines[-2:],
        "raw_start": resp.text[:500],
    })


@app.route("/api/refresh")
@login_required
def refresh():
    try:
        campaign_stats, _ = get_campaign_stats(DIRECT_TOKEN, [])
        return jsonify({"ok": True, "count": len(campaign_stats)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
@login_required
def analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "API ключ Claude не настроен"}), 503

    try:
        campaign_stats, _ = get_campaign_stats(DIRECT_TOKEN, [])
        _enrich_stats(campaign_stats)
        api_by_id = {c["Id"]: c for c in get_campaigns(DIRECT_TOKEN)}
        missing_ids = [cid for cid in campaign_stats if cid not in api_by_id]
        if missing_ids:
            for c in get_campaigns_by_ids(DIRECT_TOKEN, missing_ids):
                api_by_id[c["Id"]] = c
        campaigns = []
        for cid, s in campaign_stats.items():
            meta = api_by_id.get(cid, {})
            campaigns.append({
                "Id": cid,
                "Name": s["name"],
                "State": meta.get("State", ""),
                "Status": meta.get("Status", ""),
                "Type": meta.get("Type", ""),
                "stats": s,
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    campaigns.sort(key=lambda c: -c["stats"].get("cost", 0))
    question = (request.json or {}).get("question", "").strip()

    past = load_history()
    past_context = ""
    if past:
        last = past[-1]
        past_context = f"\n\nПрошлый анализ ({last['date']}):\n{last['analysis'][:800]}\n"

    def state_label(c):
        state = c.get("State", "")
        status = c.get("Status", "")
        if state == "ON" or (not state and status == "ACCEPTED"):
            return "Активна"
        if state in ("SUSPENDED", "OFF", "OFF_BY_MONITORING"):
            return "Остановлена"
        if state in ("ENDED", "ARCHIVED", "CONVERTED"):
            return "Завершена"
        return state or status or "?"

    type_label = {
        "UNIFIED_CAMPAIGN": "Мастер", "SMART_CAMPAIGN": "Смарт",
        "TEXT_CAMPAIGN": "Текст", "DYNAMIC_TEXT_CAMPAIGN": "Динамика",
        "MOBILE_APP_CAMPAIGN": "Мобайл", "MCBANNER_CAMPAIGN": "Баннер",
    }
    campaigns_text = "\n".join([
        f"• «{c['Name']}» [{type_label.get(c.get('Type',''), c.get('Type','?'))}] ({state_label(c)}) | "
        f"Показы: {c['stats']['impressions']:,} | "
        f"Клики: {c['stats']['clicks']} | "
        f"CTR: {c['stats']['ctr']}% | "
        f"Расход: {c['stats']['cost']:,.0f} ₽ | "
        f"CPC: {c['stats']['cpc']} ₽ | "
        f"Тренд расхода 7д: {'↑' if c['stats']['cost_trend'] == 'up' else ('↓' if c['stats']['cost_trend'] == 'down' else '→')} "
        f"({c['stats'].get('cost_trend_pct', 0)}%)"
        for c in campaigns
    ])

    if question:
        user_message = (
            f"Данные кампаний Яндекс.Директ за 30 дней:{past_context}\n\n"
            f"{campaigns_text}\n\n"
            f"Вопрос пользователя: {question}"
        )
    else:
        user_message = (
            f"Статистика кампаний Яндекс.Директ за последние 30 дней:{past_context}\n\n"
            f"{campaigns_text}\n\n"
            "Проанализируй результаты. Какие кампании работают лучше всего? "
            "Где тратится бюджет неэффективно? Назови конкретные кампании с цифрами. "
            "Дай 3-5 конкретных рекомендаций: что приостановить, что улучшить, на что увеличить бюджет."
        )

    import anthropic
    import httpx
    _proxy = os.getenv("HTTPS_PROXY")
    _http_client = httpx.Client(proxy=_proxy) if _proxy else None
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, http_client=_http_client)
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        return jsonify({"ok": False, "error": f"Anthropic API: {e.status_code} — {e.message}"}), 200

    analysis = response.content[0].text
    title = question.strip() if question.strip() else _extract_title(analysis)
    save_to_history(analysis, question)
    return jsonify({"ok": True, "analysis": analysis, "title": title, "is_question": bool(question.strip())})


_CAMPAIGN_TYPE_LABELS = {
    "UNIFIED_CAMPAIGN": "Мастер кампания",
    "SMART_CAMPAIGN": "Смарт кампания",
    "TEXT_CAMPAIGN": "Текстовая",
    "DYNAMIC_TEXT_CAMPAIGN": "Динамическая",
    "MOBILE_APP_CAMPAIGN": "Мобильная",
    "MCBANNER_CAMPAIGN": "Медийная",
    "CPM_BANNER_CAMPAIGN": "CPM",
}


@app.route("/campaign/<int:campaign_id>")
@login_required
def campaign_detail(campaign_id):
    error = None
    campaign_meta = {}
    totals = {}
    daily_stats = {}

    date_to_default = datetime.now().strftime("%Y-%m-%d")
    date_from_default = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_from = request.args.get("date_from", date_from_default)
    date_to = request.args.get("date_to", date_to_default)

    try:
        campaign_stats, _ = get_campaign_stats(
            DIRECT_TOKEN, [], date_from=date_from, date_to=date_to
        )
        api_by_id = {c["Id"]: c for c in get_campaigns(DIRECT_TOKEN)}
        missing_ids = [cid for cid in campaign_stats if cid not in api_by_id]
        if missing_ids:
            for c in get_campaigns_by_ids(DIRECT_TOKEN, missing_ids):
                api_by_id[c["Id"]] = c
        campaign_meta = api_by_id.get(campaign_id, {})
        totals = campaign_stats.get(campaign_id, {})
        daily_stats = totals.pop("daily", {})
        if totals:
            _enrich_stats({campaign_id: totals})
    except Exception as e:
        error = str(e)

    sorted_days = sorted(daily_stats.items())

    # Chart data
    chart_labels = [d for d, _ in sorted_days]
    chart_costs = [round(v["cost"], 2) for _, v in sorted_days]
    chart_clicks = [v["clicks"] for _, v in sorted_days]
    chart_impressions = [v["impressions"] for _, v in sorted_days]
    chart_ctrs = [
        round(v["clicks"] / v["impressions"] * 100, 2) if v["impressions"] else 0
        for _, v in sorted_days
    ]

    # Week-over-week (always last 7 calendar days vs previous 7)
    this_week_cost = round(sum(v["cost"] for _, v in sorted_days[-7:]), 2) if sorted_days else 0
    prev_week_cost = round(sum(v["cost"] for _, v in sorted_days[-14:-7]), 2) if len(sorted_days) >= 8 else 0
    this_week_clicks = sum(v["clicks"] for _, v in sorted_days[-7:]) if sorted_days else 0
    prev_week_clicks = sum(v["clicks"] for _, v in sorted_days[-14:-7]) if len(sorted_days) >= 8 else 0

    # Extended summary stats
    active_days = [d for d, v in sorted_days if v["cost"] > 0 or v["clicks"] > 0]
    n_active = len(active_days)
    avg_daily_cost = round(totals.get("cost", 0) / n_active, 0) if n_active else 0
    avg_daily_clicks = round(totals.get("clicks", 0) / n_active, 1) if n_active else 0

    best_ctr_day = max(
        sorted_days,
        key=lambda x: x[1]["clicks"] / x[1]["impressions"] if x[1]["impressions"] else 0,
        default=None,
    )
    best_clicks_day = max(sorted_days, key=lambda x: x[1]["clicks"], default=None)
    worst_cost_day = max(sorted_days, key=lambda x: x[1]["cost"], default=None)

    # Daily breakdown table for JS rendering
    daily_table = []
    for d, v in sorted_days:
        ctr = round(v["clicks"] / v["impressions"] * 100, 2) if v["impressions"] else 0
        cpc = round(v["cost"] / v["clicks"], 2) if v["clicks"] else 0
        conv = v.get("conversions", 0)
        cpa = round(v["cost"] / conv, 2) if conv else 0
        daily_table.append({
            "date": d,
            "impressions": v["impressions"],
            "clicks": v["clicks"],
            "ctr": ctr,
            "cost": round(v["cost"], 2),
            "cpc": cpc,
            "conversions": conv,
            "cpa": cpa,
        })

    total_conversions = totals.get("conversions", 0)
    total_cpa = round(totals.get("cost", 0) / total_conversions, 2) if total_conversions else 0

    ctype = campaign_meta.get("Type", "")
    state = campaign_meta.get("State", "")
    campaign_name = totals.get("name") or campaign_meta.get("Name", f"Кампания {campaign_id}")
    history = load_history(campaign_id=campaign_id)

    return render_template(
        "campaign.html",
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        now=datetime.now().strftime("%d.%m.%Y %H:%M"),
        campaign_meta=campaign_meta,
        campaign_type=ctype,
        campaign_type_label=_CAMPAIGN_TYPE_LABELS.get(ctype, ctype),
        state=state,
        totals=totals,
        history=history,
        error=error,
        date_from=date_from,
        date_to=date_to,
        chart_labels=json.dumps(chart_labels),
        chart_costs=json.dumps(chart_costs),
        chart_clicks=json.dumps(chart_clicks),
        chart_impressions=json.dumps(chart_impressions),
        chart_ctrs=json.dumps(chart_ctrs),
        daily_table=json.dumps(daily_table, ensure_ascii=False),
        this_week_cost=this_week_cost,
        prev_week_cost=prev_week_cost,
        this_week_clicks=this_week_clicks,
        prev_week_clicks=prev_week_clicks,
        n_active=n_active,
        avg_daily_cost=int(avg_daily_cost),
        avg_daily_clicks=avg_daily_clicks,
        best_ctr_day=best_ctr_day,
        best_clicks_day=best_clicks_day,
        worst_cost_day=worst_cost_day,
        total_conversions=total_conversions,
        total_cpa=total_cpa,
    )


@app.route("/api/analyze/<int:campaign_id>", methods=["POST"])
@login_required
def analyze_campaign(campaign_id):
    if not ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "API ключ Claude не настроен"}), 503

    try:
        campaign_stats, _ = get_campaign_stats(DIRECT_TOKEN, [])
        _enrich_stats(campaign_stats)
        api_by_id = {c["Id"]: c for c in get_campaigns(DIRECT_TOKEN)}
        missing_ids = [cid for cid in campaign_stats if cid not in api_by_id]
        if missing_ids:
            for c in get_campaigns_by_ids(DIRECT_TOKEN, missing_ids):
                api_by_id[c["Id"]] = c
        campaign_meta = api_by_id.get(campaign_id, {})
        s = campaign_stats.get(campaign_id, {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    question = (request.json or {}).get("question", "").strip()

    past = load_history(campaign_id=campaign_id)
    past_context = ""
    if past:
        last = past[-1]
        past_context = f"\n\nПредыдущий анализ этой кампании ({last['date']}):\n{last['analysis'][:600]}\n"

    ctype = campaign_meta.get("Type", "")
    state = campaign_meta.get("State", "")
    name = s.get("name") or campaign_meta.get("Name", f"Кампания {campaign_id}")

    state_str = {"ON": "Активна", "SUSPENDED": "Остановлена", "OFF": "Остановлена",
                 "ENDED": "Завершена", "ARCHIVED": "В архиве"}.get(state, state or "неизвестен")

    campaign_info = (
        f"Кампания: «{name}»\n"
        f"Тип: {_CAMPAIGN_TYPE_LABELS.get(ctype, ctype or '?')}\n"
        f"Статус: {state_str}\n\n"
        f"Статистика за 30 дней:\n"
        f"  Показы: {s.get('impressions', 0):,}\n"
        f"  Клики: {s.get('clicks', 0)}\n"
        f"  CTR: {s.get('ctr', 0)}%\n"
        f"  Расход: {s.get('cost', 0):,.0f} ₽\n"
        f"  CPC: {s.get('cpc', 0)} ₽\n"
        f"  Расход последние 7 дней: {s.get('last7d_cost', 0):,.0f} ₽\n"
        f"  Расход предыдущие 7 дней: {s.get('prev7d_cost', 0):,.0f} ₽\n"
        f"  CTR последние 7 дней: {s.get('last7d_ctr', 0)}%\n"
        f"  CTR предыдущие 7 дней: {s.get('prev7d_ctr', 0)}%"
    )

    if question:
        user_message = f"{campaign_info}{past_context}\n\nВопрос: {question}"
    else:
        user_message = (
            f"{campaign_info}{past_context}\n\n"
            "Проанализируй эту кампанию детально. "
            "Эффективен ли CTR и CPC для данного типа кампании? "
            "Есть ли тревожные тренды? Что конкретно стоит изменить или улучшить?"
        )

    import anthropic
    import httpx
    _proxy = os.getenv("HTTPS_PROXY")
    _http_client = httpx.Client(proxy=_proxy) if _proxy else None
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, http_client=_http_client)
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        return jsonify({"ok": False, "error": f"Anthropic API: {e.status_code} — {e.message}"}), 200

    analysis = response.content[0].text
    title = question.strip() if question.strip() else _extract_title(analysis)
    save_to_history(analysis, question, campaign_id=campaign_id)
    return jsonify({"ok": True, "analysis": analysis, "title": title, "is_question": bool(question.strip())})


@app.route("/api/negatives/<int:campaign_id>")
@login_required
def get_negatives_endpoint(campaign_id):
    try:
        return jsonify({"ok": True, **get_negatives(DIRECT_TOKEN, campaign_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/debug/campaign/<int:campaign_id>")
@login_required
def debug_campaign(campaign_id):
    import requests as req
    from direct_client import DIRECT_API_URL, _headers
    sess = req.Session()
    sess.trust_env = False
    resp = sess.post(
        DIRECT_API_URL + "campaigns",
        json={"method": "get", "params": {
            "SelectionCriteria": {"Ids": [campaign_id]},
            "FieldNames": ["Id", "Name", "Type"],
            "TextCampaignFieldNames": ["CounterIds"],
            "UnifiedCampaignFieldNames": ["CounterIds"],
            "SmartCampaignFieldNames": ["CounterId", "PriorityGoals"],
            "DynamicTextCampaignFieldNames": ["CounterIds"],
        }},
        headers=_headers(DIRECT_TOKEN),
    )
    return jsonify(resp.json())


@app.route("/api/negatives/<int:campaign_id>/campaign", methods=["POST"])
@login_required
def add_campaign_negatives(campaign_id):
    phrases = [p.strip().lower() for p in request.json.get("phrases", []) if p.strip()]
    if not phrases:
        return jsonify({"ok": False, "error": "Нет фраз"}), 400
    try:
        data = get_negatives(DIRECT_TOKEN, campaign_id)
        merged = list(dict.fromkeys(data["campaign_negatives"] + phrases))
        update_campaign_negatives(DIRECT_TOKEN, campaign_id, merged)
        return jsonify({"ok": True, "total": len(merged)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/negatives/group/<int:adgroup_id>", methods=["POST"])
@login_required
def add_adgroup_negatives(adgroup_id):
    phrases = [p.strip().lower() for p in request.json.get("phrases", []) if p.strip()]
    current = request.json.get("current", [])
    if not phrases:
        return jsonify({"ok": False, "error": "Нет фраз"}), 400
    try:
        merged = list(dict.fromkeys(current + phrases))
        update_adgroup_negatives(DIRECT_TOKEN, adgroup_id, merged)
        return jsonify({"ok": True, "total": len(merged)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/negatives/<int:campaign_id>/analyze", methods=["POST"])
@login_required
def analyze_negatives(campaign_id):
    if not ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "API ключ Claude не настроен"}), 503
    date_from = request.json.get("date_from", "")
    date_to = request.json.get("date_to", "")
    if not date_from or not date_to:
        return jsonify({"ok": False, "error": "Укажите период"}), 400
    # Frontend sends the current negatives it knows about (guards against Yandex API update lag)
    client_neg = [p.strip() for p in request.json.get("current_negatives", []) if p.strip()]
    try:
        neg_data = get_negatives(DIRECT_TOKEN, campaign_id)
        queries = get_search_queries(DIRECT_TOKEN, campaign_id, date_from, date_to)
        camp_neg = neg_data["campaign_negatives"]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Collect all negatives: campaign + all groups + client-side (dedup set for filtering)
    group_neg = [n for g in neg_data.get("groups", []) for n in g.get("negatives", [])]
    all_neg = list(dict.fromkeys(camp_neg + group_neg + client_neg))

    neg_str = "\n".join(all_neg) if all_neg else "не установлены"
    rows = "\n".join(
        f"{q['query']} | ключ: {q['keyword']} | {q['impressions']} показов | {q['clicks']} кликов | CTR {q['ctr']}% | ср.цена {q['avg_cpc']} ₽ | расход {q['cost']} ₽"
        for q in queries[:200]
    )
    if not rows:
        return jsonify({"ok": False, "error": "Нет поисковых запросов за период. Возможно, это смарт-кампания или медийная — для них поисковые запросы недоступны."}), 400

    system = ("Ты — аналитик Яндекс.Директ. Найди нерелевантные поисковые запросы для добавления в минус-фразы. "
              "Верни ТОЛЬКО JSON-массив: [{\"phrase\":\"текст\",\"reason\":\"пояснение\"}]. "
              "Важные правила:\n"
              "- Не включай то, что уже есть в минус-фразах\n"
              "- Максимум 30 предложений\n"
              "- Предпочитай одиночные слова фразам: одно слово в Директе блокирует все словоформы (лемматизация). "
              "Например, вместо 'конфликтом интересов' предлагай 'конфликт' и 'интерес' как два отдельных элемента\n"
              "- НИКОГДА не предлагай предлоги, союзы и частицы (в, на, с, к, по, от, до, из, за, под, при, без, для, и, или, но, не, ни и т.п.) — "
              "они стоп-слова в Директе и игнорируются в запросах\n"
              "- Имена, фамилии, отчества разбивай на отдельные слова: вместо 'щербаков станислав николаевич' предлагай три отдельных элемента\n"
              "- Телефоны и числа добавляй целиком как одну фразу\n"
              "- Фразу (несколько слов) используй только когда оба слова по отдельности релевантны, а вместе — нет "
              "(например, 'своими руками', 'что такое')")
    user_msg = (f"Период: {date_from} — {date_to}\n\n"
                f"Текущие минус-фразы кампании:\n{neg_str}\n\n"
                f"Поисковые запросы (запрос | ключевая фраза объявления | показы | клики | CTR | ср.цена клика | расход):\n{rows}")

    import anthropic, httpx
    _proxy = os.getenv("HTTPS_PROXY")
    _http_client = httpx.Client(proxy=_proxy) if _proxy else None
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, http_client=_http_client)
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIError as e:
        return jsonify({"ok": False, "error": f"Claude: {e.status_code} — {e.message}"}), 200

    import re
    text = resp.content[0].text
    # Strip markdown code fences if present
    clean = re.sub(r'```(?:json)?\s*', '', text).strip()
    suggestions = None
    # Try whole response first, then extract first [...] block
    for candidate in [clean, (re.search(r'\[.*\]', clean, re.DOTALL) or type('', (), {'group': lambda s: None})()).group()]:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                suggestions = parsed
                break
        except json.JSONDecodeError:
            continue
    if suggestions is None:
        app.logger.error("Claude raw response: %s", text)
        return jsonify({"ok": False, "error": f"Ошибка разбора ответа Claude. Ответ: {text[:300]}"}), 200

    STOPWORDS = {
        'в', 'на', 'с', 'к', 'по', 'от', 'до', 'из', 'за', 'под', 'над',
        'при', 'без', 'про', 'для', 'через', 'об', 'о', 'у', 'во', 'со',
        'и', 'или', 'но', 'а', 'да', 'то', 'что', 'как', 'так', 'если',
        'не', 'ни', 'бы', 'ли', 'же',
    }

    def _norm(p):
        return re.sub(r'\s+', ' ', re.sub(r'[!"+]', '', p)).strip().lower()

    existing_norm = {_norm(n) for n in all_neg}
    # Single-word negatives already block any query containing that word
    single_word_negs = {n for n in existing_norm if n and ' ' not in n}

    def is_covered(phrase):
        norm = _norm(phrase)
        if not norm:
            return True
        if norm in existing_norm:
            return True
        return any(w in single_word_negs for w in norm.split())

    def is_stopword(phrase):
        return _norm(phrase) in STOPWORDS

    suggestions = [s for s in suggestions
                   if s.get("phrase", "").strip() and not is_covered(s["phrase"]) and not is_stopword(s["phrase"])]

    return jsonify({"ok": True, "suggestions": suggestions, "current_negatives": camp_neg})


@app.route("/api/negatives/<int:campaign_id>/set", methods=["POST"])
@login_required
def set_campaign_negatives(campaign_id):
    phrases = [p.strip() for p in request.json.get("phrases", []) if p.strip()]
    try:
        update_campaign_negatives(DIRECT_TOKEN, campaign_id, phrases)
        return jsonify({"ok": True, "total": len(phrases)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/negatives/group/<int:adgroup_id>/set", methods=["POST"])
@login_required
def set_adgroup_negatives(adgroup_id):
    phrases = [p.strip() for p in request.json.get("phrases", []) if p.strip()]
    try:
        update_adgroup_negatives(DIRECT_TOKEN, adgroup_id, phrases)
        return jsonify({"ok": True, "total": len(phrases)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/keywords/<int:campaign_id>/<date>")
@login_required
def keywords(campaign_id, date):
    try:
        kws = get_keyword_stats(DIRECT_TOKEN, campaign_id, date)
        return jsonify({"ok": True, "keywords": kws, "date": date})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/search_queries/<int:campaign_id>")
@login_required
def search_queries_range(campaign_id):
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    if not date_from or not date_to:
        return jsonify({"ok": False, "error": "Укажите период"}), 400
    try:
        queries = get_search_queries(DIRECT_TOKEN, campaign_id, date_from, date_to)
        return jsonify({"ok": True, "queries": queries})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _kw_cache_file(campaign_id):
    return os.path.join("/tmp", f"direct_kw_cache_{campaign_id}.json")

def _read_kw_cache(campaign_id):
    path = _kw_cache_file(campaign_id)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return None

def _write_kw_cache(campaign_id, keywords, date_from, date_to):
    path = _kw_cache_file(campaign_id)
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "date_from": date_from,
        "date_to": date_to,
        "keywords": keywords,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


@app.route("/api/campaign_keywords/<int:campaign_id>")
@login_required
def campaign_keywords_endpoint(campaign_id):
    cached = _read_kw_cache(campaign_id)
    if cached:
        return jsonify({"ok": True, **cached})
    # No cache yet — fetch fresh
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        kws = get_keywords_with_stats(DIRECT_TOKEN, campaign_id, date_from, date_to)
        data = _write_kw_cache(campaign_id, kws, date_from, date_to)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/campaign_keywords/<int:campaign_id>/refresh", methods=["POST"])
@login_required
def campaign_keywords_refresh(campaign_id):
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        kws = get_keywords_with_stats(DIRECT_TOKEN, campaign_id, date_from, date_to)
        data = _write_kw_cache(campaign_id, kws, date_from, date_to)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/campaign_keywords/<int:campaign_id>/analyze", methods=["POST"])
@login_required
def campaign_keywords_analyze(campaign_id):
    if not ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "API ключ Claude не настроен"}), 503
    cached = _read_kw_cache(campaign_id)
    if not cached or not cached.get("keywords"):
        return jsonify({"ok": False, "error": "Нет данных — сначала обновите список фраз"}), 400

    kws = cached["keywords"]
    period = f"{cached.get('date_from', '')} — {cached.get('date_to', '')}"
    rows = "\n".join(
        f"{k['keyword']} | {k['impressions']} показов | {k['clicks']} кликов | CTR {k['ctr']}% "
        f"| ср.CPC {k['avg_cpc']} ₽ | расход {k['cost']} ₽"
        + (f" | ставка {k['bid']} ₽" if k.get('bid') else "")
        for k in kws
    )
    question = (request.json or {}).get("question", "").strip()
    system = (
        "Ты — эксперт по контекстной рекламе Яндекс.Директ. "
        "Анализируй ключевые фразы кампании и давай конкретные actionable-рекомендации. "
        "Будь краток и по делу. Используй цифры из данных."
    )
    if question:
        user_msg = f"Период: {period}\n\nКлючевые фразы:\n{rows}\n\nВопрос: {question}"
    else:
        user_msg = (
            f"Период: {period}\n\nКлючевые фразы кампании:\n{rows}\n\n"
            "Проанализируй:\n"
            "1. Самые эффективные фразы — стоит ли поднять ставку?\n"
            "2. Неэффективные фразы — снизить ставку или приостановить?\n"
            "3. Где есть потенциал роста?\n"
            "4. Приоритетные действия (топ-3)."
        )

    import anthropic, httpx
    _proxy = os.getenv("HTTPS_PROXY")
    _http = httpx.Client(proxy=_proxy) if _proxy else None
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, http_client=_http)
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return jsonify({"ok": True, "analysis": resp.content[0].text})
    except anthropic.APIError as e:
        return jsonify({"ok": False, "error": f"Claude: {e.status_code} — {e.message}"}), 200


@app.route("/api/keyword_bids/<int:campaign_id>")
@login_required
def keyword_bids_endpoint(campaign_id):
    try:
        bids = get_keyword_bids(DIRECT_TOKEN, campaign_id)
        return jsonify({"ok": True, "keywords": bids})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/keyword_bid/<int:keyword_id>", methods=["POST"])
@login_required
def update_keyword_bid(keyword_id):
    body = request.json or {}
    bid = body.get("bid")
    if bid is None:
        return jsonify({"ok": False, "error": "Укажите ставку"}), 400
    try:
        bid = float(bid)
        if bid <= 0:
            return jsonify({"ok": False, "error": "Ставка должна быть больше 0"}), 400
        set_keyword_bid(DIRECT_TOKEN, keyword_id, bid)
        return jsonify({"ok": True})
    except ValueError:
        return jsonify({"ok": False, "error": "Некорректное значение ставки"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _goals_cache_file(campaign_id):
    return os.path.join("/tmp", f"direct_goals_{campaign_id}.json")

def _goalstats_cache_file(campaign_id, date_from, date_to):
    return os.path.join("/tmp", f"direct_goalstats_{campaign_id}_{date_from}_{date_to}.json")

def _read_json_cache(path, max_age_seconds):
    if not os.path.exists(path):
        return None
    age = datetime.now().timestamp() - os.path.getmtime(path)
    if age > max_age_seconds:
        return None
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return None

def _write_json_cache(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fetch_and_cache_goals(campaign_id):
    result = get_campaign_goals(DIRECT_TOKEN, campaign_id)
    if result.get("goals"):  # only cache successful results
        _write_json_cache(_goals_cache_file(campaign_id), result)
    return result


@app.route("/api/campaign_goals/<int:campaign_id>/refresh", methods=["POST"])
@login_required
def campaign_goals_refresh(campaign_id):
    try:
        result = _fetch_and_cache_goals(campaign_id)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/campaign_goals/<int:campaign_id>")
@login_required
def campaign_goals_endpoint(campaign_id):
    cached = _read_json_cache(_goals_cache_file(campaign_id), 4 * 3600)
    if cached:
        return jsonify({"ok": True, **cached})
    try:
        result = _fetch_and_cache_goals(campaign_id)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/campaign_goal_stats/<int:campaign_id>")
@login_required
def campaign_goal_stats_endpoint(campaign_id):
    date_to = request.args.get("date_to", datetime.now().strftime("%Y-%m-%d"))
    date_from = request.args.get("date_from", (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
    path = _goalstats_cache_file(campaign_id, date_from, date_to)
    cached = _read_json_cache(path, 3600)
    if cached:
        return jsonify({"ok": True, "goals_stats": cached})

    # First get goal list
    goals_data = get_campaign_goals(DIRECT_TOKEN, campaign_id)
    if goals_data.get("error") or not goals_data.get("goals"):
        return jsonify({"ok": False, "error": goals_data.get("error", "Нет целей")}), 400

    goals_stats = {}
    for g in goals_data["goals"]:
        try:
            stats = get_goal_period_stats(DIRECT_TOKEN, campaign_id, g["id"], date_from, date_to)
            goals_stats[str(g["id"])] = {"name": g["name"], **stats}
        except Exception:
            goals_stats[str(g["id"])] = {"name": g["name"], "total": 0, "daily": {}, "cpa": 0}

    _write_json_cache(path, goals_stats)
    return jsonify({"ok": True, "goals_stats": goals_stats})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8002))
    app.run(host="0.0.0.0", port=port, debug=False)
