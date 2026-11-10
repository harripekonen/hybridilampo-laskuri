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
SPOT_15M_FILE = "spot_15min.json"
OIL_FILE = "oljy_hinnat.json"


def fetch_json(url, headers=None, data=None):
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_from_local_csv():
    csv_files = glob.glob("*.csv")
    if not csv_files:
        return None, None
    csv_path = csv_files[0]
    print(f"Luetaan pohjadata paikallisesta CSV-tiedostosta: {csv_path}")
    hourly_buckets = {}
    q15_map = {}
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
                q15_map[dt.replace(second=0, microsecond=0)] = round(val, 2)
            except ValueError:
                continue
    if not hourly_buckets:
        return None, None
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
    hourly_obj = {
        "start": min_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "resolution": "1h",
        "prices": prices
    }
    return hourly_obj, q15_map


def fetch_sahkotin_range(start_dt, end_dt):
    hourly_map = {}
    q15_map = {}
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
                dt_fin = dt_utc.astimezone(FIN_TZ).replace(second=0, microsecond=0)
                val = round(float(item["value"]), 2)
                q15_map[dt_fin.replace(tzinfo=None)] = val
                hr_fin = dt_fin.replace(minute=0)
                hourly_map.setdefault(hr_fin, []).append(val)
        except Exception as e:
            print(f"Varoitus haettaessa jaksoa {s_utc} -> {e_utc}: {e}")
        cur_start = cur_end
    return hourly_map, q15_map


def update_spot_prices():
    spot_data = None
    _, csv_q15_map = build_from_local_csv()

    if os.path.exists(SPOT_FILE):
        try:
            with open(SPOT_FILE, "r", encoding="utf-8") as f:
                spot_data = json.load(f)
        except Exception as e:
            print(f"Virhe luettaessa {SPOT_FILE}: {e}")

    if not spot_data or not spot_data.get("prices"):
        spot_data, _ = build_from_local_csv()
        if not spot_data:
            spot_data = {"start": "2021-01-01T01:00:00", "resolution": "1h", "prices": []}

    prices = spot_data.get("prices", [])
    start_str = spot_data.get("start", "2021-01-01T01:00:00")
    start_naive = datetime.fromisoformat(start_str).replace(tzinfo=None)
    next_naive = start_naive + timedelta(hours=len(prices))
    next_dt = next_naive.replace(tzinfo=FIN_TZ)
    target_end_dt = (datetime.now(FIN_TZ) + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)

    q15_all = csv_q15_map or {}
    if os.path.exists(SPOT_15M_FILE):
        try:
            with open(SPOT_15M_FILE, "r", encoding="utf-8") as f:
                old_15m = json.load(f)
                s15 = datetime.fromisoformat(old_15m["start"])
                for idx, v in enumerate(old_15m.get("prices", [])):
                    q15_all[s15 + timedelta(minutes=15 * idx)] = v
        except Exception:
            pass

    fetch_from = min(next_dt, datetime.now(FIN_TZ) - timedelta(days=7))
    print(f"Haetaan spot-hinnat: {fetch_from.isoformat()} -> {target_end_dt.isoformat()}")
    hourly_map, fetched_q15 = fetch_sahkotin_range(fetch_from, target_end_dt)
    q15_all.update(fetched_q15)

    try:
        latest_res = fetch_json("https://api.porssisahko.net/v1/latest-prices.json")
        for item in latest_res.get("prices", []):
            dt_utc = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
            dt_fin = dt_utc.astimezone(FIN_TZ).replace(second=0, microsecond=0)
            val = round(float(item["price"]), 2)
            q15_all[dt_fin.replace(tzinfo=None)] = val
            hr_fin = dt_fin.replace(minute=0)
            hourly_map.setdefault(hr_fin, []).append(val)
    except Exception as e:
        print(f"Huom: porssisahko.net-haku ohitettiin ({e})")

    if hourly_map:
        max_fetched_naive = max(k.replace(tzinfo=None) for k in hourly_map.keys())
        cur_naive = next_naive
        naive_map = {k.replace(tzinfo=None): v for k, v in hourly_map.items()}
        while cur_naive <= max_fetched_naive:
            vals = naive_map.get(cur_naive)
            avg_val = round(sum(vals) / len(vals), 2) if vals else (prices[-1] if prices else 8.0)
            prices.append(avg_val)
            cur_naive += timedelta(hours=1)

    spot_data["prices"] = prices
    spot_data["updated"] = datetime.now(FIN_TZ).strftime("%Y-%m-%d %H:%M")

    with open(SPOT_FILE, "w", encoding="utf-8") as f:
        json.dump(spot_data, f, separators=(",", ":"))
    print(f"Tallennettu {SPOT_FILE} ({len(prices)} tuntia).")

    if q15_all:
        max_15_dt = max(q15_all.keys())
        min_15_dt = max(min(q15_all.keys()), max_15_dt - timedelta(days=365)).replace(minute=0, second=0, microsecond=0)
        q15_list = []
        cur_15 = min_15_dt
        last_v = 8.0
        while cur_15 <= max_15_dt:
            v = q15_all.get(cur_15)
            if v is None:
                v = q15_all.get(cur_15.replace(minute=0), last_v)
            last_v = v
            q15_list.append(round(float(v), 2))
            cur_15 += timedelta(minutes=15)

        q15_obj = {
            "start": min_15_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "resolution": "15m",
            "updated": datetime.now(FIN_TZ).strftime("%Y-%m-%d %H:%M"),
            "prices": q15_list
        }
        with open(SPOT_15M_FILE, "w", encoding="utf-8") as f:
            json.dump(q15_obj, f, separators=(",", ":"))
        print(f"Tallennettu {SPOT_15M_FILE} ({len(q15_list)} varttia alkaen {q15_obj['start']}).")


def fetch_neste_oil_price(postal_code="21260", product_number="160360"):
    """
    Hakee Nesteen suorasta REST APIsta päivän hinnan (kesälaatu = 160360, postinumero = 21260).
    """
    url = f"https://www.neste.fi/api/proxy/get-prices?postalCode={postal_code}&productNumbers={product_number}&quantities=0&installments=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.neste.fi/yksityisille/tuotteet/lammitysoljyt/lammitysoljytilaus"
    }
    try:
        data = fetch_json(url, headers=headers)
        item = data.get(str(product_number), {})
        price = item.get("unitPriceTaxed")
        if price is not None and float(price) > 0:
            val = round(float(price), 4)
            print(f"Neste API palautti litrahinnan: {val} €/l (postinumero {postal_code})")
            return val
    except Exception as e:
        print(f"Neste API -haku epäonnistui: {e}")
    return None


def update_oil_prices():
    if os.path.exists(OIL_FILE):
        with open(OIL_FILE, "r", encoding="utf-8") as f:
            oil_data = json.load(f)
    else:
        oil_data = {"updated": "2026-10-28", "current_price": 2.01, "monthly": {}, "daily_neste": {}}

    # 1. Tilastokeskuksen kuukausihinnat
    px_url = "https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/khi/statfin_khi_pxt_11xx.px"
    try:
        meta = fetch_json(px_url)
        variables = meta.get("variables", [])
        month_var = next(v for v in variables if v["code"] == "Kuukausi")
        comm_var = next(v for v in variables if v["code"] == "Hyödyke")
        info_var = next(v for v in variables if v["code"] == "Tiedot")

        oil_code = next(val for val, txt in zip(comm_var["values"], comm_var["valueTexts"]) if "Kevyt polttoöljy" in txt)
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

        oil_data["monthly"] = monthly_dict
    except Exception as e:
        print(f"Tilastokeskuksen päivitys ohitettiin ({e}).")

    # 2. Nesteen suora API-haku (21260 / kesälaatu)
    neste_price = fetch_neste_oil_price(postal_code="21260", product_number="160360")
    today_str = datetime.now(FIN_TZ).strftime("%Y-%m-%d")
    daily_neste = oil_data.get("daily_neste", {})

    if neste_price:
        daily_neste[today_str] = neste_price
        oil_data["current_price"] = round(neste_price, 2)
        cur_y = str(datetime.now(FIN_TZ).year)
        cur_m = datetime.now(FIN_TZ).month - 1
        if cur_y in oil_data.get("monthly", {}):
            oil_data["monthly"][cur_y][cur_m] = round(neste_price, 2)
        print(f"Päivitetty Nesteen hinta {today_str}: {neste_price} €/l")
    else:
        print("Huom: Nesteen hintaa ei saatu haettua tänään, ei lisätty virheellistä merkintää.")

    oil_data["daily_neste"] = daily_neste
    oil_data["neste_config"] = {"postal_code": "21260", "volume_l": 1000, "grade": "kesälaatu (160360)"}
    oil_data["updated"] = today_str

    with open(OIL_FILE, "w", encoding="utf-8") as f:
        json.dump(oil_data, f, ensure_ascii=False, indent=2)
    print("Polttoöljyn hintatiedot tallennettu.")


if __name__ == "__main__":
    update_spot_prices()
    update_oil_prices()
