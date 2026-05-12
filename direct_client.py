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


def get_campaigns(token: str) -> list[dict]:
    payload = {
        "method": "get",
        "params": {
            "SelectionCriteria": {},
            "FieldNames": ["Id", "Name", "Status", "State", "DailyBudget", "StartDate", "Statistics"],
            "Page": {"Limit": 100},
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


def get_campaign_stats(token: str, campaign_ids: list[int], days: int = 30) -> dict[int, dict]:
    """Возвращает статистику по кампаниям за последние N дней."""
    if not campaign_ids:
        return {}

    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    payload = {
        "method": "get",
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
                "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(i) for i in campaign_ids]}],
            },
            "FieldNames": ["CampaignId", "Date"],
            "CampaignFields": ["Impressions", "Clicks", "Cost", "Ctr"],
            "ReportName": "campaign_stats",
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
        return {}

    result: dict[int, dict] = {}
    for line in resp.text.strip().split("\n")[1:]:  # пропускаем заголовок
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        try:
            cid = int(parts[0])
            impressions = int(parts[2]) if parts[2] != "--" else 0
            clicks = int(parts[3]) if parts[3] != "--" else 0
            cost = float(parts[4]) if parts[4] != "--" else 0.0
            ctr = float(parts[5]) if len(parts) > 5 and parts[5] != "--" else 0.0
            if cid not in result:
                result[cid] = {"impressions": 0, "clicks": 0, "cost": 0.0, "ctr": 0.0}
            result[cid]["impressions"] += impressions
            result[cid]["clicks"] += clicks
            result[cid]["cost"] += cost
            if impressions:
                result[cid]["ctr"] = round(result[cid]["clicks"] / result[cid]["impressions"] * 100, 2)
        except (ValueError, IndexError):
            continue

    for cid in result:
        result[cid]["cost"] = round(result[cid]["cost"], 2)

    return result
