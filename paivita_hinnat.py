#!/usr/bin/env python3
"""
Automaattinen hintadatan päivittäjä (GitHub Actions):
1. Päivittää pörssisähkön tuntihinnat (spot_tuntihinnat.json) Sähkötin.fi / Pörssisähkö.net avoimista rajapinnoista.
2. Päivittää kevyen polttoöljyn kuukausihinnat (oljy_hinnat.json) Tilastokeskuksen PxWeb-rajapinnasta (taulukko 11xx).
"""

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FIN_TZ = ZoneInfo("Europe/Helsinki")
SPOT_FILE = "spot_tuntihinnat.json"
OIL_FILE = "oljy_hinnat.json"


def fetch_json(url, data=None):
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "Hybridilampo-Laskuri-Updater/1.0", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def update_spot_prices():
    with open(SPOT_FILE, "r", encoding="utf-8") as f:
        spot_data = json.load(f)

    prices = spot_data.get("prices", [])
    start_str = spot_data.get("start", "2021-01-01T01:00:00")
    start_dt = datetime.fromisoformat(start_str).replace(tzinfo=FIN_TZ)

    last_dt = start_dt + timedelta(hours=len(prices) - 1)
    next_dt = last_dt + timedelta(hours=1)
    now_dt = datetime.now(FIN_TZ).replace(minute=0, second=0, microsecond=0)

    if next_dt > now_dt:
        print(f"Spot-hinnat ovat jo ajan tasalla ({last_dt.isoformat()}).")
        return

    print(f"Haetaan puuttuvat spot-tunnit alkaen: {next_dt.isoformat()} -> {now_dt.isoformat()}")

    start_utc = next_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_utc = (now_dt + timedelta(hours=24)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url = f"https://sahkotin.fi/prices?fix&vat&start={start_utc}&end={end_utc}"

    hourly_map = {}
    try:
        res = fetch_json(url)
        for item in res.get("prices", []):
            dt_utc = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
            dt_fin = dt_utc.astimezone(FIN_TZ).replace(minute=0, second=0, microsecond=0)
            hourly_map.setdefault(dt_fin, []).append(float(item["value"]))
    except Exception as e:
        print(f"Varoitus: Sähkötin.fi-haku epäonnistui ({e}), kokeillaan porssisahko.net-rajapintaa...")

    try:
        latest_res = fetch_json("https://api.porssisahko.net/v1/latest-prices.json")
        for item in latest_res.get("prices", []):
            dt_utc = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
            dt_fin = dt_utc.astimezone(FIN_TZ).replace(minute=0, second=0, microsecond=0)
            hourly_map.setdefault(dt_fin, []).append(float(item["price"]))
    except Exception as e:
        print(f"Huom: porssisahko.net-haku ei onnistunut ({e})")

    if not hourly_map:
        print("Uusia spot-hintoja ei saatu haettua.")
        return

    max_fetched_dt = max(hourly_map.keys())
    cur_dt = next_dt
    added_count = 0

    while cur_dt <= min(now_dt, max_fetched_dt):
        vals = hourly_map.get(cur_dt)
        if vals:
            avg_val = round(sum(vals) / len(vals), 2)
        else:
            avg_val = prices[-1] if prices else 8.0
        prices.append(avg_val)
        added_count += 1
        cur_dt += timedelta(hours=1)

    spot_data["prices"] = prices
    spot_data["updated"] = datetime.now(FIN_TZ).strftime("%Y-%m-%d %H:%M")

    with open(SPOT_FILE, "w", encoding="utf-8") as f:
        json.dump(spot_data, f, separators=(",", ":"))

    print(f"Lisätty {added_count} uutta tuntihintaa. Yhteensä {len(prices)} tuntia.")


def update_oil_prices():
    with open(OIL_FILE, "r", encoding="utf-8") as f:
        oil_data = json.load(f)

    px_url = "https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/khi/statfin_khi_pxt_11xx.px"
    try:
        meta = fetch_json(px_url)
        variables = meta.get("variables", [])
        month_var = next(v for v in variables if v["code"] == "Kuukausi")
        comm_var = next(v for v in variables if v["code"] == "Hyödyke")
        info_var = next(v for v in variables if v["code"] == "Tiedot")

        oil_code = None
        for val, txt in zip(comm_var["values"], comm_var["valueTexts"]):
            if "Kevyt polttoöljy" in txt:
                oil_code = val
                break

        months_from_2021 = [m for m in month_var["values"] if m >= "2021M01"]

        query_payload = {
            "query": [
                {"code": "Kuukausi", "selection": {"filter": "item", "values": months_from_2021}},
                {"code": "Hyödyke", "selection": {"filter": "item", "values": [oil_code]}},
                {"code": "Tiedot", "selection": {"filter": "item", "values": [info_var["values"][0]]}}
            ],
            "response": {"format": "json-stat2"}
        }

        res = fetch_json(px_url, data=json.dumps(query_payload).encode("utf-8"))
        values = res.get("value", [])

        monthly_dict = oil_data.get("monthly", {})
        latest_valid_price = oil_data.get("current_price", 2.01)

        for m_code, val in zip(months_from_2021, values):
            if val is not None and val > 0:
                year_str, month_str = m_code.split("M")
                m_idx = int(month_str) - 1
                if year_str not in monthly_dict:
                    monthly_dict[year_str] = [latest_valid_price] * 12
                monthly_dict[year_str][m_idx] = round(float(val), 2)
                latest_valid_price = round(float(val), 2)

        oil_data["yearly_averages"] = {
            y: round(sum(v) / len(v), 2) for y, v in monthly_dict.items()
        }
        oil_data["monthly"] = monthly_dict
        oil_data["updated"] = datetime.now(FIN_TZ).strftime("%Y-%m-%d")

        with open(OIL_FILE, "w", encoding="utf-8") as f:
            json.dump(oil_data, f, ensure_ascii=False, indent=2)

        print("Polttoöljyn hintahistoria päivitetty Tilastokeskuksen tietokannasta.")
    except Exception as e:
        print(f"Polttoöljyn tilastopäivitys ohitettiin ({e}). Säilytetään nykyiset tiedot.")


if __name__ == "__main__":
    update_spot_prices()
    update_oil_prices()