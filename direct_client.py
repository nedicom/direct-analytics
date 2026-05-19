from __future__ import annotations
import re
import requests
from datetime import datetime, timedelta


DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "ru",
        "Content-Type": "application/json",
    }


def _campaigns_request(token: str, selection_criteria: dict) -> list[dict]:
    payload = {
        "method": "get",
        "params": {
            "SelectionCriteria": selection_criteria,
            "FieldNames": ["Id", "Name", "Status", "State", "Type", "StartDate"],
            "Page": {"Limit": 1000},
        },
    }
    session = requests.Session()
    session.trust_env = False
    resp = session.post(DIRECT_API_URL + "campaigns", json=payload, headers=_headers(token))
    data = resp.json()
    error = data.get("error")
    if error:
        raise Exception(error.get("error_detail") or error.get("error_string", "Ошибка Яндекс.Директ"))
    return data.get("result", {}).get("Campaigns", [])


def get_campaigns(token: str) -> list[dict]:
    return _campaigns_request(token, {})


def get_campaigns_by_ids(token: str, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    return _campaigns_request(token, {"Ids": ids})


def get_campaign_stats(
    token: str,
    campaign_ids: list[int],
    days: int = 30,
    date_from: str = None,
    date_to: str = None,
) -> tuple[dict, dict]:
    """Returns (campaign_stats, daily_stats).

    campaign_stats: campaign_id → {name, impressions, clicks, cost, ctr,
                                    last7d_cost, last7d_clicks, last7d_impressions,
                                    prev7d_cost, prev7d_clicks, prev7d_impressions}
    daily_stats: date_str → {impressions, clicks, cost}
    """
    date_to = date_to or datetime.now().strftime("%Y-%m-%d")
    date_from = date_from or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    fourteen_days_ago = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    payload = {
        "method": "get",
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
            },
            "FieldNames": ["CampaignId", "CampaignName", "Date", "Impressions", "Clicks", "Cost", "Ctr", "Conversions"],
            "ReportName": f"statsv2_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    }

    headers = _headers(token)
    headers["processingMode"] = "auto"
    headers["returnMoneyInMicros"] = "false"

    session = requests.Session()
    session.trust_env = False
    resp = session.post(DIRECT_API_URL + "reports", json=payload, headers=headers)

    if resp.status_code not in (200, 201, 202):
        raise Exception(f"Reports API: HTTP {resp.status_code} — {resp.text[:300]}")

    # Columns: CampaignId(0), CampaignName(1), Date(2), Impressions(3), Clicks(4), Cost(5), Ctr(6)
    result: dict[int, dict] = {}
    daily: dict[str, dict] = {}

    lines = resp.text.strip().split("\n")
    for line in lines[2:]:  # skip report name + column headers
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            cid = int(parts[0])
            name = parts[1]
            date = parts[2]
            impressions = int(parts[3]) if parts[3] != "--" else 0
            clicks = int(parts[4]) if parts[4] != "--" else 0
            cost = float(parts[5]) if parts[5] != "--" else 0.0
            conversions = int(parts[7]) if len(parts) > 7 and parts[7] not in ("--", "") else 0

            if cid not in result:
                result[cid] = {
                    "name": name,
                    "impressions": 0, "clicks": 0, "cost": 0.0, "ctr": 0.0,
                    "conversions": 0,
                    "last7d_cost": 0.0, "last7d_clicks": 0, "last7d_impressions": 0,
                    "prev7d_cost": 0.0, "prev7d_clicks": 0, "prev7d_impressions": 0,
                    "daily": {},
                }
            result[cid]["impressions"] += impressions
            result[cid]["clicks"] += clicks
            result[cid]["cost"] += cost
            result[cid]["conversions"] += conversions

            if date not in result[cid]["daily"]:
                result[cid]["daily"][date] = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0}
            result[cid]["daily"][date]["impressions"] += impressions
            result[cid]["daily"][date]["clicks"] += clicks
            result[cid]["daily"][date]["cost"] += cost
            result[cid]["daily"][date]["conversions"] += conversions

            if date >= seven_days_ago:
                result[cid]["last7d_cost"] += cost
                result[cid]["last7d_clicks"] += clicks
                result[cid]["last7d_impressions"] += impressions
            elif date >= fourteen_days_ago:
                result[cid]["prev7d_cost"] += cost
                result[cid]["prev7d_clicks"] += clicks
                result[cid]["prev7d_impressions"] += impressions

            if date not in daily:
                daily[date] = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0}
            daily[date]["impressions"] += impressions
            daily[date]["clicks"] += clicks
            daily[date]["cost"] += cost
            daily[date]["conversions"] += conversions

        except (ValueError, IndexError):
            continue

    for cid in result:
        result[cid]["cost"] = round(result[cid]["cost"], 2)
        result[cid]["last7d_cost"] = round(result[cid]["last7d_cost"], 2)
        result[cid]["prev7d_cost"] = round(result[cid]["prev7d_cost"], 2)
        if result[cid]["impressions"]:
            result[cid]["ctr"] = round(result[cid]["clicks"] / result[cid]["impressions"] * 100, 2)

    for date in daily:
        daily[date]["cost"] = round(daily[date]["cost"], 2)

    return result, daily


def _extract_negatives(field) -> list:
    """Yandex returns NegativeKeywords as {"Items": [...]} or plain list.
    Strips Yandex operator syntax (", !, +) so phrases are stored and displayed cleanly."""
    if not field:
        return []
    if isinstance(field, list):
        items = field
    elif isinstance(field, dict):
        items = field.get("Items") or []
    else:
        return []
    return [re.sub(r'\s+', ' ', re.sub(r'[!"+]', '', p)).strip().lower() for p in items if p]


def get_negatives(token: str, campaign_id: int) -> dict:
    """Returns campaign-level and adgroup-level negative keywords."""
    sess = requests.Session()
    sess.trust_env = False
    h = _headers(token)

    camp_resp = sess.post(
        DIRECT_API_URL + "campaigns",
        json={"method": "get", "params": {
            "SelectionCriteria": {"Ids": [campaign_id]},
            "FieldNames": ["Id", "Name", "NegativeKeywords"],
        }},
        headers=h,
    )
    camps = camp_resp.json().get("result", {}).get("Campaigns", [])
    camp_neg = _extract_negatives(camps[0].get("NegativeKeywords")) if camps else []

    groups = []
    groups_error = None
    try:
        grp_resp = sess.post(
            DIRECT_API_URL + "adgroups",
            json={"method": "get", "params": {
                "SelectionCriteria": {"CampaignIds": [campaign_id]},
                "FieldNames": ["Id", "Name", "NegativeKeywords"],
                "Page": {"Limit": 1000},
            }},
            headers=h,
        )
        grp_data = grp_resp.json()
        if "error" in grp_data:
            groups_error = grp_data["error"].get("error_detail") or grp_data["error"].get("error_string", "Ошибка API групп")
        else:
            groups = grp_data.get("result", {}).get("AdGroups", []) or []
    except Exception as e:
        groups_error = str(e)

    return {
        "campaign_negatives": camp_neg,
        "groups": [
            {"id": g["Id"], "name": g["Name"], "negatives": _extract_negatives(g.get("NegativeKeywords"))}
            for g in groups
        ],
        "groups_error": groups_error,
    }


def update_campaign_negatives(token: str, campaign_id: int, negatives: list) -> None:
    sess = requests.Session()
    sess.trust_env = False
    resp = sess.post(
        DIRECT_API_URL + "campaigns",
        json={"method": "update", "params": {"Campaigns": [{"Id": campaign_id, "NegativeKeywords": {"Items": negatives}}]}},
        headers=_headers(token),
    )
    err = resp.json().get("error")
    if err:
        raise Exception(err.get("error_detail") or err.get("error_string", "Ошибка API"))


def update_adgroup_negatives(token: str, adgroup_id: int, negatives: list) -> None:
    sess = requests.Session()
    sess.trust_env = False
    resp = sess.post(
        DIRECT_API_URL + "adgroups",
        json={"method": "update", "params": {"AdGroups": [{"Id": adgroup_id, "NegativeKeywords": {"Items": negatives}}]}},
        headers=_headers(token),
    )
    err = resp.json().get("error")
    if err:
        raise Exception(err.get("error_detail") or err.get("error_string", "Ошибка API"))


def _reports_request(token: str, payload: dict) -> str:
    """POST to Reports API with retry on 201/202. Returns response body text."""
    import time
    headers = _headers(token)
    headers["processingMode"] = "auto"
    headers["returnMoneyInMicros"] = "false"
    sess = requests.Session()
    sess.trust_env = False
    for _attempt in range(12):
        resp = sess.post(DIRECT_API_URL + "reports", json=payload, headers=headers)
        if resp.status_code == 200:
            body = resp.text.strip()
            if body.startswith("{"):
                try:
                    err = resp.json()
                    msg = (err.get("error", {}).get("error_detail")
                           or err.get("error", {}).get("error_string") or "неизвестная ошибка")
                except Exception:
                    msg = body[:300]
                raise Exception(f"Reports API: {msg}")
            return body
        if resp.status_code in (201, 202):
            wait = min(int(resp.headers.get("retryIn", 5)), 30)
            time.sleep(wait)
            continue
        body = resp.text.strip()
        try:
            err = resp.json()
            msg = (err.get("error", {}).get("error_detail")
                   or err.get("error", {}).get("error_string") or f"HTTP {resp.status_code}")
        except Exception:
            msg = body[:300] or f"HTTP {resp.status_code}"
        raise Exception(f"Reports API: {msg}")
    raise Exception("Reports API: отчёт не готов после нескольких попыток, попробуйте позже")


def get_search_queries(token: str, campaign_id: int, date_from: str, date_to: str) -> list[dict]:
    """Returns aggregated search queries for a campaign over a date range."""
    # Columns: CampaignId(0), Keyword(1), Query(2), Impressions(3), Clicks(4), Cost(5), Ctr(6), AvgCpc(7)
    body = _reports_request(token, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": ["CampaignId", "Keyword", "Query", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc"],
            "ReportName": f"sq2_{campaign_id}_{date_from}_{date_to}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    })

    def _int(s): return int(s) if s and s != "--" else 0
    def _float(s): return float(s) if s and s != "--" else 0.0

    # Aggregate by (keyword, query) pair
    agg: dict[tuple, dict] = {}
    for line in body.split("\n")[2:]:
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        try:
            if int(parts[0]) != campaign_id:
                continue
            key = (parts[1], parts[2])  # (keyword, query)
            if key not in agg:
                agg[key] = {"keyword": parts[1], "query": parts[2],
                            "impressions": 0, "clicks": 0, "cost": 0.0}
            agg[key]["impressions"] += _int(parts[3])
            agg[key]["clicks"] += _int(parts[4])
            agg[key]["cost"] += _float(parts[5])
        except (ValueError, IndexError):
            continue

    result = sorted(agg.values(), key=lambda x: x["impressions"], reverse=True)[:300]
    for r in result:
        r["cost"] = round(r["cost"], 2)
        clicks = r["clicks"]
        impr = r["impressions"]
        r["ctr"] = round(clicks / impr * 100, 2) if impr else 0.0
        r["avg_cpc"] = round(r["cost"] / clicks, 2) if clicks else 0.0
    return result


def get_keywords_with_stats(token: str, campaign_id: int, date_from: str, date_to: str) -> list[dict]:
    """Returns campaign keywords with bids and performance stats.
    Stats come from CRITERIA_PERFORMANCE_REPORT; bids from Keywords API."""

    def _int(s): return int(s) if s and s != "--" else 0
    def _float(s): return float(s) if s and s != "--" else 0.0

    # ── Step 1: performance stats from report ──
    # Columns: Criterion(0), CriteriaType(1), Impressions(2), Clicks(3), Cost(4), Ctr(5), AvgCpc(6)
    body = _reports_request(token, {
        "method": "get",
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
                "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(campaign_id)]}],
            },
            "FieldNames": ["Criterion", "CriteriaType", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc"],
            "ReportName": f"kws_{campaign_id}_{date_from}_{date_to}",
            "ReportType": "CRITERIA_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    })

    stats: dict[str, dict] = {}
    for line in body.split("\n")[2:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            if parts[1] != "KEYWORD":
                continue
            kw = parts[0]
            stats[kw] = {
                "keyword": kw,
                "impressions": _int(parts[2]),
                "clicks": _int(parts[3]),
                "cost": round(_float(parts[4]), 2),
                "ctr": round(_float(parts[5]), 2),
                "avg_cpc": round(_float(parts[6]), 2),
                "bid": None,
            }
        except (ValueError, IndexError):
            continue

    # ── Step 2: keyword bids from Keywords API ──
    sess = requests.Session()
    sess.trust_env = False
    try:
        resp = sess.post(
            DIRECT_API_URL + "keywords",
            json={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [campaign_id]},
                    "FieldNames": ["Keyword", "Status"],
                    "BiddingFieldNames": ["Bid"],
                    "Page": {"Limit": 10000},
                },
            },
            headers=_headers(token),
        )
        for kw_obj in resp.json().get("result", {}).get("Keywords", []):
            text = kw_obj.get("Keyword", "")
            bid_val = kw_obj.get("Bid")
            if text in stats and bid_val:
                stats[text]["bid"] = round(_float(str(bid_val)), 2)
    except Exception:
        pass  # bids are optional — don't fail the whole call

    result = sorted(stats.values(), key=lambda x: x["clicks"], reverse=True)
    return result


def get_keyword_bids(token: str, campaign_id: int) -> list[dict]:
    """Fetch all keywords with current bids and serving status from Keywords API."""
    sess = requests.Session()
    sess.trust_env = False
    result = []
    offset = 0
    while True:
        resp = sess.post(
            DIRECT_API_URL + "keywords",
            json={
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": [campaign_id]},
                    "FieldNames": ["Id", "Keyword", "Status", "State"],
                    "BiddingFieldNames": ["Bid"],
                    "Page": {"Limit": 1000, "Offset": offset},
                },
            },
            headers=_headers(token),
        )
        data = resp.json()
        err = data.get("error")
        if err:
            raise Exception(err.get("error_detail") or err.get("error_string", "Ошибка API"))
        items = data.get("result", {}).get("Keywords", [])
        for kw in items:
            bid = kw.get("Bid")
            result.append({
                "id": kw["Id"],
                "keyword": kw["Keyword"],
                "status": kw.get("Status", ""),
                "state": kw.get("State", ""),
                "bid": round(float(bid), 2) if bid else None,
            })
        if not data.get("result", {}).get("LimitedBy"):
            break
        offset = data["result"]["LimitedBy"]
    return result


def set_keyword_bid(token: str, keyword_id: int, bid: float) -> None:
    """Update bid for a single keyword via bids resource."""
    sess = requests.Session()
    sess.trust_env = False
    resp = sess.post(
        DIRECT_API_URL + "bids",
        json={
            "method": "set",
            "params": {"Bids": [{"KeywordId": keyword_id, "Bid": bid}]},
        },
        headers=_headers(token),
    )
    data = resp.json()
    err = data.get("error")
    if err:
        raise Exception(err.get("error_detail") or err.get("error_string", "Ошибка API"))
    results = (data.get("result") or {}).get("SetResults", [])
    if results:
        item_errors = results[0].get("Errors", [])
        if item_errors:
            raise Exception(item_errors[0].get("Message", "Ошибка установки ставки"))


_CAMPAIGN_TYPE_MAP = {
    "TEXT_CAMPAIGN":         ("TextCampaignFieldNames",        "TextCampaign",        "CounterIds", True),
    "UNIFIED_CAMPAIGN":      ("UnifiedCampaignFieldNames",     "UnifiedCampaign",     "CounterIds", True),
    "DYNAMIC_TEXT_CAMPAIGN": ("DynamicTextCampaignFieldNames", "DynamicTextCampaign", "CounterIds", True),
    "MOBILE_APP_CAMPAIGN":   ("MobileAppCampaignFieldNames",   "MobileAppCampaign",   "CounterIds", True),
    "SMART_CAMPAIGN":        ("SmartCampaignFieldNames",       "SmartCampaign",       "CounterId",  False),
    "CPM_BANNER_CAMPAIGN":   ("CpmBannerCampaignFieldNames",   "CpmBannerCampaign",   "CounterIds", True),
    "MCBANNER_CAMPAIGN":     ("McBannerCampaignFieldNames",    "McBannerCampaign",    "CounterIds", True),
}


def _get_campaign_type(sess, token: str, campaign_id: int) -> str:
    resp = sess.post(DIRECT_API_URL + "campaigns", json={
        "method": "get",
        "params": {"SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id", "Type"]},
    }, headers=_headers(token))
    camps = resp.json().get("result", {}).get("Campaigns", [])
    return camps[0].get("Type", "") if camps else ""


def get_counter_goals(token: str, counter_id: int) -> list[dict]:
    """Fetch all goals from a Metrica counter. Returns [{id, name}]."""
    sess = requests.Session()
    sess.trust_env = False
    r = sess.get(
        f"https://api-metrika.yandex.net/management/v1/counter/{counter_id}/goals",
        headers={"Authorization": f"OAuth {token}"},
        timeout=10,
    )
    if r.status_code == 403:
        raise Exception("Нет доступа к Метрике — добавьте разрешение metrika:read в OAuth-приложение")
    if r.status_code != 200:
        raise Exception(f"Метрика API: HTTP {r.status_code}")
    return [{"id": g["id"], "name": g.get("name", f"Цель {g['id']}")} for g in r.json().get("goals", [])]


def get_campaign_counter_id(token: str, campaign_id: int) -> int | None:
    """Returns the Metrica counter ID for a campaign, or None."""
    sess = requests.Session()
    sess.trust_env = False
    ctype = _get_campaign_type(sess, token, campaign_id)
    mapping = _CAMPAIGN_TYPE_MAP.get(ctype)
    if not mapping:
        return None
    fn_key, sub_key, counter_field, is_array = mapping
    resp = sess.post(DIRECT_API_URL + "campaigns", json={
        "method": "get",
        "params": {"SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id"], fn_key: [counter_field]},
    }, headers=_headers(token))
    camps = resp.json().get("result", {}).get("Campaigns", [])
    sub = camps[0].get(sub_key, {}) if camps else {}
    raw = sub.get(counter_field)
    if is_array:
        ids = (raw.get("Items") if isinstance(raw, dict) else raw) or []
        return ids[0] if ids else None
    return raw if raw else None


def get_campaign_goal_ids(token: str, campaign_id: int) -> list[int]:
    """Returns goal IDs from campaign PriorityGoals (strategy optimization targets)."""
    sess = requests.Session()
    sess.trust_env = False
    ctype = _get_campaign_type(sess, token, campaign_id)
    mapping = _CAMPAIGN_TYPE_MAP.get(ctype)
    if not mapping:
        return []
    fn_key, sub_key = mapping[0], mapping[1]
    try:
        resp = sess.post(DIRECT_API_URL + "campaigns", json={
            "method": "get",
            "params": {"SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id"], fn_key: ["PriorityGoals"]},
        }, headers=_headers(token))
        camps = resp.json().get("result", {}).get("Campaigns", [])
        sub = camps[0].get(sub_key, {}) if camps else {}
        pg = sub.get("PriorityGoals", {})
        items = pg.get("Items", []) if isinstance(pg, dict) else []
        return [item["GoalId"] for item in items if "GoalId" in item]
    except Exception:
        return []


def get_goal_period_stats(token: str, campaign_id: int, goal_id: int,
                           date_from: str, date_to: str) -> dict:
    """Returns {total, daily: {date: conv}, cpa} for one Metrica goal."""
    try:
        body = _reports_request(token, {
            "method": "get",
            "params": {
                "SelectionCriteria": {
                    "DateFrom": date_from, "DateTo": date_to,
                    "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(campaign_id)]}],
                },
                "Goals": [goal_id],
                "FieldNames": ["Date", "Conversions", "CostPerConversion"],
                "ReportName": f"goal_{campaign_id}_{goal_id}_{date_from}_{date_to}",
                "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "YES",
                "IncludeDiscount": "NO",
            },
        })
    except Exception:
        return {"total": 0, "daily": {}, "cpa": 0}

    def _int(s): return int(s) if s and s != "--" else 0
    def _float(s): return float(s) if s and s != "--" else 0.0

    daily, total, weighted_cost = {}, 0, 0.0
    for line in body.split("\n")[2:]:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            date = parts[0]
            conv = _int(parts[1])
            cpa = _float(parts[2]) if len(parts) > 2 else 0.0
            if conv:
                daily[date] = daily.get(date, 0) + conv
                weighted_cost += conv * cpa
            total += conv
        except (ValueError, IndexError):
            continue

    return {"total": total, "daily": daily, "cpa": round(weighted_cost / total, 2) if total else 0}


def get_keyword_stats(token: str, campaign_id: int, date: str) -> list[dict]:
    """Returns search queries for a campaign on a specific date, sorted by clicks desc.
    Columns: Query(0) Impressions(1) Clicks(2) Cost(3) Ctr(4) AvgImpressionPosition(5) AvgClickPosition(6)"""
    body = _reports_request(token, {
        "method": "get",
        "params": {
            "SelectionCriteria": {
                "DateFrom": date,
                "DateTo": date,
                "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(campaign_id)]}],
            },
            "FieldNames": ["Query", "Impressions", "Clicks", "Cost", "Ctr",
                           "AvgImpressionPosition", "AvgClickPosition"],
            "ReportName": f"kwp_{campaign_id}_{date}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    })

    def _int(s): return int(s) if s and s != "--" else 0
    def _float(s): return float(s) if s and s != "--" else 0.0

    result = []
    for line in body.split("\n")[2:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            result.append({
                "query": parts[0],
                "impressions": _int(parts[1]),
                "clicks": _int(parts[2]),
                "cost": round(_float(parts[3]), 2),
                "ctr": round(_float(parts[4]), 2),
                "avg_imp_pos": round(_float(parts[5]), 1),
                "avg_clk_pos": round(_float(parts[6]), 1),
            })
        except (ValueError, IndexError):
            continue
    result.sort(key=lambda x: x["clicks"], reverse=True)
    return result
