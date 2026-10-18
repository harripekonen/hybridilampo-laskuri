#!/usr/bin/env python3
import glob
import json
import os
import urllib.request
import urllib.parse
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
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_from_local_csv():
    csv_files = glob.glob("*.csv")
    if not csv_files:
        return None
    csv_path = csv_files[0]
    print(f"Luetaan pohjadata paikallisesta CSV-tiedostosta: {csv_path}")
    hourly_buckets = {}
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            parts = line.strip().split(";")
            if len(parts) < 2:
                continue
            try:
                dt = datetime.strptime(parts[0].strip(), "%d.%m.%Y %H:%M:%S")
                val = float(parts[1].strip().replace(",", "."))
                hr_dt = dt.replace(minute=0, second=0, microsecond=0)
                hourly_buckets.setdefault(hr_dt, []).append(val)
            except ValueError:
                continue
    if not hourly_buckets:
        return None
    min_dt = min(hourly_buckets.keys())
    max_dt = max(hourly_buckets.keys())
    prices = []
    cur = min_dt
    while cur <= max_dt:
        vals = hourly_buckets.get(cur)
        if vals:
            prices.append(round(sum(vals) / len(vals), 2))
        else:
            prices.append(prices[-1] if prices else 8.0)
        cur += timedelta(hours=1)
    return {
        "start": min_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "resolution": "1h",
        "prices": prices
    }


def fetch_sahkotin_range(start_dt, end_dt):
    hourly_map = {}
    cur_start = start_dt
    while cur_start < end_dt:
        cur_end = min(cur_start + timedelta(days=180), end_dt)
        s_utc = urllib.parse.quote(cur_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        e_utc = urllib.parse.quote(cur_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        url = f"https://sahkotin.fi/prices?fix&vat&start={s_utc}&end={e_utc}"
        try:
            res = fetch_json(url)
            for item in res.get("prices", []):
                dt_utc = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                dt_fin = dt_utc.astimezone(FIN_TZ).replace(minute=0, second=0, microsecond=0)
                hourly_map.setdefault(dt_fin, []).append(float(item["value"]))
        except Exception as e:
            print(f"Varoitus haettaessa jaksoa {s_utc} -> {e_utc}: {e}")
        cur_start = cur_end
    return hourly_map


def update_spot_prices():
    spot_data = None
    if os.path.exists(SPOT_FILE):
        try:
            with open(SPOT_FILE, "r", encoding="utf-8") as f:
                spot_data = json.load(f)
        except Exception as e:
            print(f"Virhe luettaessa {SPOT_FILE}: {e}")

    if not spot_data or not spot_data.get("prices"):
        spot_data = build_from_local_csv()
        if not spot_data:
            spot_data = {"start": "2021-01-01T01:00:00", "resolution": "1h", "prices": []}

    prices = spot_data.get("prices", [])
    start_str = spot_data.get("start", "2021-01-01T01:00:00")
    start_naive = datetime.fromisoformat(start_str).replace(tzinfo=None)
    next_naive = start_naive + timedelta(hours=len(prices))
    next_dt = next_naive.replace(tzinfo=FIN_TZ)
    now_dt = datetime.now(FIN_TZ).replace(minute=0, second=0, microsecond=0)

    if next_dt <= now_dt:
        print(f"Haetaan puuttuvat spot-tunnit: {next_dt.isoformat()} -> {now_dt.isoformat()}")
        hourly_map = fetch_sahkotin_range(next_dt, now_dt + timedelta(hours=24))

        try:
            latest_res = fetch_json("https://api.porssisahko.net/v1/latest-prices.json")
            for item in latest_res.get("prices", []):
                dt_utc = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
                dt_fin = dt_utc.astimezone(FIN_TZ).replace(minute=0, second=0, microsecond=0)
                hourly_map.setdefault(dt_fin, []).append(float(item["price"]))
        except Exception as e:
            print(f"Huom: porssisahko.net-haku ohitettiin ({e})")

        if hourly_map:
            max_fetched_naive = max(k.replace(tzinfo=None) for k in hourly_map.keys())
            now_naive = now_dt.replace(tzinfo=None)
            cur_naive = next_naive
            naive_map = {k.replace(tzinfo=None): v for k, v in hourly_map.items()}
            while cur_naive <= min(now_naive, max_fetched_naive):
                vals = naive_map.get(cur_naive)
                avg_val = round(sum(vals) / len(vals), 2) if vals else (prices[-1] if prices else 8.0)
                prices.append(avg_val)
                cur_naive += timedelta(hours=1)

    spot_data["prices"] = prices
    spot_data["updated"] = datetime.now(FIN_TZ).strftime("%Y-%m-%d %H:%M")

    with open(SPOT_FILE, "w", encoding="utf-8") as f:
        json.dump(spot_data, f, separators=(",", ":"))
    print(f"Tallennettu {SPOT_FILE} ({len(prices)} tuntia).")


def update_oil_prices():
    if os.path.exists(OIL_FILE):
        with open(OIL_FILE, "r", encoding="utf-8") as f:
            oil_data = json.load(f)
    else:
        oil_data = {"updated": "2026-10-18", "current_price": 2.01, "monthly": {}}

    px_url = "https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/khi/statfin_khi_pxt_11xx.px"
    try:
        meta = fetch_json(px_url)
        variables = meta.get("variables", [])
        month_var = next(v for v in variables if v["code"] == "Kuukausi")
        comm_var = next(v for v in variables if v["code"] == "Hyödyke")
        info_var = next(v for v in variables if v["code"] == "Tiedot")

        oil_code = next(
            val for val, txt in zip(comm_var["values"], comm_var["valueTexts"])
            if "Kevyt polttoöljy" in txt
        )
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
        print("Polttoöljyn hintahistoria päivitetty.")
    except Exception as e:
        print(f"Polttoöljyn tilastopäivitys ohitettiin ({e}).")


if __name__ == "__main__":
    update_spot_prices()
    update_oil_prices()
