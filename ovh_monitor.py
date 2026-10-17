#!/usr/bin/env python3
"""
OVH / Kimsufi / So you Start 独服库存 + 低价监控  (v2)

思路参考 Operation-Chicken-Nugget（目录按价格排序 + 轮询库存 API）和 alerts.ovh
（列出当前有货的机器、补货即提醒），但只做"发现好机器 -> 通知你"，不自动下单。

数据来源：OVH 官方公开 API（无需 API Key）
  目录: /1.0/order/catalog/public/eco?ovhSubsidiary=XX
  库存: /1.0/dedicated/server/datacenter/availabilities

和 v1 的区别
  * 按"具体配置"算真实月租：基础价 + 内存 + 硬盘 + 带宽的必选加价
    （同一型号，32G 或大硬盘配置可能贵 5~10 刀，库存也是按配置分开的）
  * 每次都会算出"当前有货最便宜 Top N"，可单独打印，也会附在提醒邮件里
  * 补货检测：某个配置从缺货变成有货，且价格 <= 阈值 -> 立刻提醒（alerts.ovh 的核心功能）
  * --loop 常驻轮询模式（默认每 120 秒查一次库存，每小时刷新一次价格目录）
  * 通知渠道：邮件，另可选 Telegram

触发提醒的条件（都要求"有货"）
  1. 价格 <= 阈值，且这个配置是新出现的库存（之前缺货 / 第一次看到）
  2. 目录里带促销（promotions 非空）
  3. 比上次记录降价
同一配置在持续有货期间只提醒一次；卖光后再补货会再提醒。

用法
  python3 ovh_monitor.py --top          # 打印当前有货最便宜的机器（不发通知）
  python3 ovh_monitor.py --dry-run      # 跑一次检查，只打印不通知
  python3 ovh_monitor.py                # 跑一次检查并通知（适合 cron）
  python3 ovh_monitor.py --loop 120     # 常驻，每 120 秒检查一次（推荐，抢补货）
  python3 ovh_monitor.py --test-notify  # 发测试通知

配置（环境变量或同目录 .env）
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_TO   邮件
  TG_BOT_TOKEN / TG_CHAT_ID                                 Telegram（可选）
  SUBSIDIARIES=US,FR,CA      查哪些地区商店（价格币种不同）
  MAX_PRICE_US=10 MAX_PRICE_FR=10 MAX_PRICE_CA=14           阈值（含税前月租）
  BRANDS=Kimsufi,So you Start,OVHcloud
  DATACENTERS=               只看这些机房，如 bhs,vin,hil,gra,rbx（空=全部）
  INCLUDE_SLOW=0             1=交付期 72H/720H 的也算有货（默认只算 1 小时内可交付的现货）
  TOP_N=10
  FILTER_NAME=               型号正则，如 KS-1|KS-4|SYS（参考 buy_ovh）
  FILTER_DISK=               硬盘正则，如 nvme|ssd
  FILTER_MEMORY=             内存正则，如 32g|64g
  ADD_VAT=0                  1=价格加上当地增值税（欧区 FR 为 20%）
  COMMIT_MONTHS=1            1/12/24：按月付、年付、两年付折算月价（长约常更便宜）
  ALERT_NEW_MODELS=1         目录出现新型号时提醒
"""
import json, os, re, sys, time, smtplib, ssl, urllib.request, urllib.parse, datetime
from email.mime.text import MIMEText
from email.header import Header

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")

ENDPOINTS = {
    "US": "https://api.us.ovhcloud.com/1.0",
    "CA": "https://ca.api.ovh.com/1.0",
    "FR": "https://eu.api.ovh.com/1.0",
    "IE": "https://eu.api.ovh.com/1.0",
    "GB": "https://eu.api.ovh.com/1.0",
    "DE": "https://eu.api.ovh.com/1.0",
}
BRAND_LINKS = {
    "Kimsufi": "https://eco.ovhcloud.com/en/kimsufi/",
    "So you Start": "https://eco.ovhcloud.com/en/soyoustart/",
    "OVHcloud": "https://eco.ovhcloud.com/en/rise/",
}
DEFAULT_MAX = {"US": 10, "CA": 14, "FR": 10, "IE": 10, "GB": 9, "DE": 10}
FAST = {"1H-low", "1H-high"}                 # 1 小时内交付 = 真有现货
SLOW = {"24H", "72H", "240H", "480H", "720H", "1440H"}  # 可下单但要等

_catalog_cache = {}   # sub -> (timestamp, parsed)


# ---------------------------------------------------------------- utils
def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ovh-stock-monitor/2.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def brand_of(name):
    n = name.upper()
    if n.startswith("KS-"):
        return "Kimsufi"
    if n.startswith("SYS-"):
        return "So you Start"
    return "OVHcloud"


def monthly(pricings, months=None):
    """返回 (按月折算后的价格, pricing)。months=1/12/24 对应月付/年付/两年付"""
    months = months or int(os.environ.get("COMMIT_MONTHS", "1"))
    mode = "default" if months == 1 else f"upfront{months}"
    for pr in pricings or []:
        if "renew" in pr.get("capacities", []) and pr.get("interval") == months \
                and pr.get("intervalUnit") == "month" and pr.get("mode") == mode:
            return pr["price"] / 1e8 / months, pr
    if months != 1:
        return monthly(pricings, 1)
    return None, None


def setup_fee(pricings):
    for pr in pricings or []:
        if "installation" in pr.get("capacities", []) and pr.get("mode") == "default":
            return pr["price"] / 1e8
    return 0.0


# ---------------------------------------------------------------- catalog
def load_catalog(sub, max_age=3600):
    ts, parsed = _catalog_cache.get(sub, (0, None))
    if parsed and time.time() - ts < max_age:
        return parsed
    cat = get_json(f"{ENDPOINTS[sub]}/order/catalog/public/eco?ovhSubsidiary={sub}")
    addons = {}
    for a in cat.get("addons", []):
        price, m = monthly(a.get("pricings"))
        addons[a["planCode"]] = (price or 0.0, (m or {}).get("promotions") or [])
    plans = {}
    for p in cat["plans"]:
        base, m = monthly(p.get("pricings"))
        if m is None:
            continue
        fams = {f["name"]: f.get("addons", []) for f in p.get("addonFamilies", [])}
        bw = min((addons.get(c, (0, []))[0] for c in fams.get("bandwidth", [])), default=0.0)
        name = p.get("invoiceName", p["planCode"])
        plans[p["planCode"]] = {
            "name": name, "brand": brand_of(name), "base": base,
            "setup": setup_fee(p.get("pricings")), "promotions": m.get("promotions") or [],
            "bandwidth": bw,
        }
    parsed = {"currency": cat["locale"]["currencyCode"], "plans": plans, "addons": addons,
              "tax": float(cat["locale"].get("taxRate") or 0)}
    _catalog_cache[sub] = (time.time(), parsed)
    return parsed


def scan(sub):
    """返回该地区所有配置（含库存状态和真实月租）"""
    cat = load_catalog(sub)
    avail = get_json(f"{ENDPOINTS[sub]}/dedicated/server/datacenter/availabilities?excludeDatacenters=false")
    include_slow = os.environ.get("INCLUDE_SLOW", "0") == "1"
    dc_filter = {d.lower() for d in env_list("DATACENTERS")}
    vat = 1 + cat["tax"] / 100 if os.environ.get("ADD_VAT", "0") == "1" else 1
    f_name = os.environ.get("FILTER_NAME", "")
    f_disk = os.environ.get("FILTER_DISK", "")
    f_mem = os.environ.get("FILTER_MEMORY", "")
    out = []
    for a in avail:
        plan = cat["plans"].get(a["planCode"])
        if not plan:
            continue
        mem = a.get("memory") or ""
        sto = a.get("storage") or ""
        if f_name and not re.search(f_name, plan["name"] + " " + a["planCode"], re.I):
            continue
        if f_disk and not re.search(f_disk, sto, re.I):
            continue
        if f_mem and not re.search(f_mem, mem, re.I):
            continue
        mem_price, mem_promo = cat["addons"].get(f"{mem}-{a['planCode']}", (0.0, []))
        sto_price, sto_promo = cat["addons"].get(f"{sto}-{a['planCode']}", (0.0, []))
        price = round((plan["base"] + mem_price + sto_price + plan["bandwidth"]) * vat, 2)
        dcs = []
        for d in a.get("datacenters", []):
            st = d.get("availability")
            if dc_filter and d["datacenter"].lower() not in dc_filter:
                continue
            if st in FAST or (include_slow and st in SLOW):
                dcs.append(f"{d['datacenter']}({st})")
        out.append({
            "sub": sub, "currency": cat["currency"], "fqn": a["fqn"], "planCode": a["planCode"],
            "name": plan["name"], "brand": plan["brand"], "price": price, "setup": round(plan["setup"] * vat, 2),
            "memory": mem.replace("ram-", ""), "storage": sto,
            "promotions": plan["promotions"] + mem_promo + sto_promo,
            "datacenters": dcs, "link": BRAND_LINKS[plan["brand"]],
        })
    return out


# ---------------------------------------------------------------- notify
def send_mail(subject, body):
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not all([host, user, pw, to]):
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"], msg["To"] = user, to
    rcpts = [x.strip() for x in to.split(",")]
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pw); s.sendmail(user, rcpts, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx); s.login(user, pw); s.sendmail(user, rcpts, msg.as_string())
    return True


def send_telegram(text):
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (tok and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)
    return True


def notify(subject, body):
    sent = []
    try:
        if send_mail(subject, body): sent.append("邮件")
    except Exception as e:
        print("[!] 邮件发送失败:", e)
    try:
        if send_telegram(subject + "\n\n" + body): sent.append("Telegram")
    except Exception as e:
        print("[!] Telegram 发送失败:", e)
    if sent:
        print(f"[+] 已通知: {'、'.join(sent)}")
    else:
        print("[!] 通知没有发出去（未配置或发送失败），内容如下：\n" + subject + "\n" + body)
        if os.environ.get("GITHUB_ACTIONS"):
            sys.exit(1)   # 让 GitHub Actions 显示失败，方便发现配置问题


# ---------------------------------------------------------------- report
def fmt(it, reasons=None):
    s = (f"[{it['brand']}] {it['name']}  {it['memory']} / {it['storage']}\n"
         f"  {it['price']} {it['currency']}/月  安装费 {it['setup']}  首月合计 {round(it['price'] + it['setup'], 2)}  商店 {it['sub']}\n"
         f"  有货: {', '.join(it['datacenters'])}\n")
    if reasons:
        s += f"  原因: {' | '.join(reasons)}\n"
    return s + f"  购买: {it['link']}\n"


def top_list(items, n):
    stock = [i for i in items if i["datacenters"]]
    stock.sort(key=lambda i: (i["sub"], i["price"]))
    by_sub = {}
    for i in stock:
        by_sub.setdefault(i["sub"], []).append(i)
    lines = []
    for sub, lst in by_sub.items():
        lines.append(f"== {sub} 商店 当前有货最便宜 Top {n} ==")
        for i in lst[:n]:
            lines.append(f"  {i['price']:>7} {i['currency']}  {i['name'][:38]:<38} "
                         f"{i['memory']:<16} {i['storage'][:22]:<22} {','.join(i['datacenters'])}")
    return "\n".join(lines) if lines else "（目前所有地区都没有符合条件的现货）"


# ---------------------------------------------------------------- main check
def check_once(dry=False, quiet=False):
    subs = [s.upper() for s in env_list("SUBSIDIARIES", "US,FR,CA")]
    brands = set(env_list("BRANDS", "Kimsufi,So you Start,OVHcloud"))
    top_n = int(os.environ.get("TOP_N", "10"))
    state = {} if dry or not os.path.exists(STATE_FILE) else json.load(open(STATE_FILE))
    first_run = not state
    new_state, alerts, items_all, new_models = {}, [], [], []

    for sub in subs:
        if sub not in ENDPOINTS:
            print("[!] 未知地区", sub); continue
        cap = float(os.environ.get(f"MAX_PRICE_{sub}", DEFAULT_MAX.get(sub, 10)))
        try:
            items = [i for i in scan(sub) if i["brand"] in brands]
        except Exception as e:
            print(f"[!] {sub} 查询失败: {e}"); continue
        items_all += items
        # 新型号上架检测（参考 buy_ovh 的 catalog monitor）
        plans_now = sorted({i["planCode"] for i in items})
        plans_prev = state.get(f"_plans:{sub}")
        new_state[f"_plans:{sub}"] = plans_now
        if plans_prev is not None and os.environ.get("ALERT_NEW_MODELS", "1") == "1":
            for pc in set(plans_now) - set(plans_prev):
                it = min((i for i in items if i["planCode"] == pc), key=lambda i: i["price"])
                new_models.append(it)
        for it in items:
            key = f"{sub}:{it['fqn']}"
            prev = state.get(key, {})
            in_stock = bool(it["datacenters"])
            rec = {"price": it["price"], "in_stock": in_stock, "alerted": False}
            new_state[key] = rec
            if not in_stock:
                continue
            reasons = []
            if it["price"] <= cap:
                if not prev.get("in_stock"):
                    reasons.append(f"补货/新上架，<= {cap:g} {it['currency']}")
                elif not prev.get("alerted"):
                    reasons.append(f"<= {cap:g} {it['currency']}")
            if it["promotions"]:
                reasons.append("促销: " + "; ".join(str(p.get("description") or p.get("name") or p)
                                                   for p in it["promotions"]))
            if prev.get("price") and it["price"] < prev["price"]:
                reasons.append(f"降价 {prev['price']} -> {it['price']}")
            already = prev.get("alerted") and prev.get("in_stock") and prev.get("price") == it["price"]
            if reasons or already:
                rec["alerted"] = True
            if reasons and not already:
                alerts.append((it, reasons))

    if new_models:
        subject = f"[OVH 监控] 新型号上架: " + ", ".join(sorted({i['name'].split(' |')[0] for i in new_models}))
        body = "\n".join(f"[{i['brand']}] {i['name']}  起价 {i['price']} {i['currency']}/月  商店 {i['sub']}  "
                         f"{'有货: ' + ', '.join(i['datacenters']) if i['datacenters'] else '暂未有货'}\n  {i['link']}\n"
                         for i in new_models)
        print(subject + "\n" + body) if dry else notify(subject, body)
    if alerts:
        alerts.sort(key=lambda x: x[0]["price"])
        subject = f"[OVH 监控] {len(alerts)} 个好机器有货：最低 {alerts[0][0]['price']} {alerts[0][0]['currency']}"
        body = "\n".join(fmt(i, r) for i, r in alerts)
        body += "\n" + top_list(items_all, top_n)
        body += f"\n\n检查时间: {datetime.datetime.now().isoformat(timespec='seconds')}"
        if dry:
            print(subject + "\n\n" + body)
        else:
            notify(subject, body)
    elif not quiet:
        print(f"{datetime.datetime.now():%H:%M:%S} 没有新的好机器。"
              + (" （首次运行，已记录当前库存）" if first_run and not dry else ""))
    if not dry:
        json.dump(new_state, open(STATE_FILE, "w"), indent=1)
    return items_all


def main():
    load_env()
    args = sys.argv[1:]
    if "--test-notify" in args:
        notify("[OVH 监控] 测试通知", "通知配置正常。发现便宜机器补货或促销时会提醒你。")
        return
    if "--top" in args:
        subs = [s.upper() for s in env_list("SUBSIDIARIES", "US,FR,CA")]
        brands = set(env_list("BRANDS", "Kimsufi,So you Start,OVHcloud"))
        items = []
        for sub in subs:
            items += [i for i in scan(sub) if i["brand"] in brands]
        print(top_list(items, int(os.environ.get("TOP_N", "10"))))
        return
    if "--loop" in args:
        i = args.index("--loop")
        interval = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 120
        interval = max(interval, 60)   # 别太频繁，避免被 OVH 限流
        print(f"常驻监控中，每 {interval} 秒检查一次。Ctrl+C 退出。")
        while True:
            try:
                check_once(quiet=True)
            except Exception as e:
                print("[!] 本轮出错:", e)
            time.sleep(interval)
    check_once(dry="--dry-run" in args)


if __name__ == "__main__":
    main()
