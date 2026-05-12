import json
import os
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session

from direct_client import get_campaigns, get_campaigns_by_ids, get_campaign_stats

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-on-server")

DIRECT_TOKEN = os.getenv("DIRECT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

HISTORY_FILE = "history.json"

SYSTEM_PROMPT = """Ты опытный аналитик рекламы в Яндекс.Директ. Помогаешь принимать решения на основе данных: какие кампании эффективны, где тратится бюджет впустую, как снизить стоимость клика и увеличить конверсии.

Стиль ответа: конкретно, с цифрами из данных, без воды. Сначала коротко — что работает, потом — 3-5 чётких рекомендаций с обоснованием."""


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if DASHBOARD_PASSWORD and not session.get("authenticated"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def _extract_title(analysis: str) -> str:
    for line in analysis.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:100]
    return "Анализ"


def save_to_history(analysis: str, question: str = ""):
    history = load_history()
    title = question.strip() if question.strip() else _extract_title(analysis)
    history.append({
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "analysis": analysis,
        "title": title,
        "is_question": bool(question.strip()),
    })
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


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
        "count": len(campaigns),
    }


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
    error = None
    try:
        campaigns = get_campaigns(DIRECT_TOKEN)
        campaign_stats = get_campaign_stats(DIRECT_TOKEN, [])
        known_ids = {c["Id"] for c in campaigns}
        missing = [cid for cid in campaign_stats if cid not in known_ids]
        if missing:
            campaigns += get_campaigns_by_ids(DIRECT_TOKEN, missing)
    except Exception as e:
        error = str(e)
    def _sort_key(c):
        state = c.get("State", "")
        status = c.get("Status", "")
        is_active = (state == "ON") or (not state and status == "ACCEPTED")
        return (0 if is_active else 1, -(int(c.get("StartDate", "0").replace("-", "") or 0)))
    campaigns.sort(key=_sort_key)
    stats = calc_stats(campaigns, campaign_stats)
    history = load_history()
    return render_template("index.html",
                           campaigns=campaigns,
                           campaign_stats=campaign_stats,
                           stats=stats,
                           history=history,
                           error=error)


@app.route("/api/debug-campaigns")
@login_required
def debug_campaigns():
    from direct_client import get_campaigns, get_campaigns_by_ids, get_campaign_stats
    campaigns = get_campaigns(DIRECT_TOKEN)
    stats = get_campaign_stats(DIRECT_TOKEN, [])
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
        campaigns = get_campaigns(DIRECT_TOKEN)
        ids = [c["Id"] for c in campaigns]
        campaign_stats = get_campaign_stats(DIRECT_TOKEN, ids)
        return jsonify({"ok": True, "count": len(campaigns)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
@login_required
def analyze():
    if not ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "error": "API ключ Claude не настроен"}), 503

    try:
        campaigns = get_campaigns(DIRECT_TOKEN)
        campaign_stats = get_campaign_stats(DIRECT_TOKEN, [])
        known_ids = {c["Id"] for c in campaigns}
        missing = [cid for cid in campaign_stats if cid not in known_ids]
        if missing:
            campaigns += get_campaigns_by_ids(DIRECT_TOKEN, missing)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    question = (request.json or {}).get("question", "").strip()

    past = load_history()
    past_context = ""
    if past:
        last = past[-1]
        past_context = f"\n\nПрошлый анализ ({last['date']}):\n{last['analysis'][:800]}\n"

    campaigns_text = "\n".join([
        f"• «{c['Name']}» | {c.get('Status', '')} "
        f"| 👁 {campaign_stats.get(c['Id'], {}).get('impressions', 0)} "
        f"| 🖱 {campaign_stats.get(c['Id'], {}).get('clicks', 0)} "
        f"| CTR {campaign_stats.get(c['Id'], {}).get('ctr', 0)}% "
        f"| 💰 {campaign_stats.get(c['Id'], {}).get('cost', 0)} ₽"
        for c in campaigns
    ])

    if question:
        user_message = f"Данные кампаний Яндекс.Директ:{past_context}\n\n{campaigns_text}\n\nВопрос: {question}"
    else:
        user_message = (
            f"Статистика кампаний Яндекс.Директ за последние 30 дней:{past_context}\n\n{campaigns_text}\n\n"
            "Проанализируй результаты. Какие кампании работают лучше всего? "
            "Где тратится бюджет неэффективно? Дай конкретные рекомендации."
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8002))
    app.run(host="0.0.0.0", port=port, debug=False)
