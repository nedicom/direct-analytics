from __future__ import annotations
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


def get_campaign_stats(token: str, campaign_ids: list[int], days: int = 30) -> tuple[dict, dict]:
    """Returns (campaign_stats, daily_stats).

    campaign_stats: campaign_id → {name, impressions, clicks, cost, ctr,
                                    last7d_cost, last7d_clicks, last7d_impressions,
                                    prev7d_cost, prev7d_clicks, prev7d_impressions}
    daily_stats: date_str → {impressions, clicks, cost}
    """
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
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
                }
            result[cid]["impressions"] += impressions
            result[cid]["clicks"] += clicks
            result[cid]["cost"] += cost

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
