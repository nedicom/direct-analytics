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
            "FieldNames": ["CampaignId", "CampaignName", "Date", "Impressions", "Clicks", "Cost", "Ctr"],
            "ReportName": f"stats_{date_from}_{date_to}",
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

            if cid not in result:
                result[cid] = {
                    "name": name,
                    "impressions": 0, "clicks": 0, "cost": 0.0, "ctr": 0.0,
                    "last7d_cost": 0.0, "last7d_clicks": 0, "last7d_impressions": 0,
                    "prev7d_cost": 0.0, "prev7d_clicks": 0, "prev7d_impressions": 0,
                    "daily": {},
                }
            result[cid]["impressions"] += impressions
            result[cid]["clicks"] += clicks
            result[cid]["cost"] += cost

            if date not in result[cid]["daily"]:
                result[cid]["daily"][date] = {"impressions": 0, "clicks": 0, "cost": 0.0}
            result[cid]["daily"][date]["impressions"] += impressions
            result[cid]["daily"][date]["clicks"] += clicks
            result[cid]["daily"][date]["cost"] += cost

            if date >= seven_days_ago:
                result[cid]["last7d_cost"] += cost
                result[cid]["last7d_clicks"] += clicks
                result[cid]["last7d_impressions"] += impressions
            elif date >= fourteen_days_ago:
                result[cid]["prev7d_cost"] += cost
                result[cid]["prev7d_clicks"] += clicks
                result[cid]["prev7d_impressions"] += impressions

            if date not in daily:
                daily[date] = {"impressions": 0, "clicks": 0, "cost": 0.0}
            daily[date]["impressions"] += impressions
            daily[date]["clicks"] += clicks
            daily[date]["cost"] += cost

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
                    "FieldNames": ["Id", "Keyword", "Status", "State", "ServingStatus"],
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
                "serving": kw.get("ServingStatus", ""),
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


def get_keyword_stats(token: str, campaign_id: int, date: str) -> list[dict]:
    """Returns search queries for a campaign on a specific date, sorted by clicks desc."""
    body = _reports_request(token, {
        "method": "get",
        "params": {
            "SelectionCriteria": {
                "DateFrom": date,
                "DateTo": date,
                "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(campaign_id)]}],
            },
            "FieldNames": ["Query", "Impressions", "Clicks", "Cost", "Ctr"],
            "ReportName": f"kw_{campaign_id}_{date}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        },
    })
    result = []
    for line in body.split("\n")[2:]:
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        try:
            result.append({
                "query": parts[0],
                "impressions": int(parts[1]) if parts[1] != "--" else 0,
                "clicks": int(parts[2]) if parts[2] != "--" else 0,
                "cost": round(float(parts[3]), 2) if parts[3] != "--" else 0.0,
                "ctr": round(float(parts[4]), 2) if parts[4] != "--" else 0.0,
            })
        except (ValueError, IndexError):
            continue
    result.sort(key=lambda x: x["clicks"], reverse=True)
    return result
