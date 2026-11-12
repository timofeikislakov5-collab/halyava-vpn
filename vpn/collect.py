#!/usr/bin/env python3
"""
Халява VPN — сборщик бесплатных VPN-серверов из открытых публичных источников.

Что делает при каждом запуске:
  1. Скачивает публичные бесплатные списки серверов (VLESS, VMess, Trojan,
     Shadowsocks, Hysteria2, TUIC), которые их авторы сами выкладывают в открытый доступ,
     и официальный список добровольных серверов VPN Gate (Университет Цукубы, OpenVPN).
  2. Убирает дубли, мусор, заглушки (127.0.0.1 и т. п.).
  3. Проверяет каждый сервер — отвечает ли он (TCP-подключение), меряет пинг.
  4. Сохраняет рабочие серверы, самые быстрые первыми, в виде «подписок»,
     которые вставляются одной ссылкой в v2rayNG / Hiddify / Streisand / v2RayTun / Happ.

Чужие платные аккаунты, «слитые» ключи и т. п. сюда НЕ собираются — только то,
что владельцы серверов сами раздают бесплатно.

Запуск:  python vpn/collect.py              — обычный запуск
         python vpn/collect.py --no-check   — без проверки серверов (быстро, для отладки)
Настройки (переменные окружения):
  MAX_SERVERS   — сколько серверов максимум в общей подписке (по умолчанию 500)
  CHECK_TIMEOUT — сколько секунд ждать ответа сервера (по умолчанию 3)
"""
import base64
import csv
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "sub")
OVPN_DIR = os.path.join(ROOT, "openvpn")
UA = {"User-Agent": "Mozilla/5.0 (HalyavaVPN collector)"}
MSK = timezone(timedelta(hours=3))

MAX_SERVERS = int(os.environ.get("MAX_SERVERS", "500"))
MAX_PER_PROTO = 300
MAX_RU = 200
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "3"))
MAX_CHECK = 6000          # больше не проверяем — слишком долго
MAX_OVPN = 30

GH = "https://raw.githubusercontent.com/"
# Публичные бесплатные списки. Если какой-то пропадёт — просто будет пропущен.
SOURCES = [
    GH + "barry-far/V2ray-Config/main/All_Configs_Sub.txt",
    GH + "mahdibland/V2RayAggregator/master/sub/sub_merge.txt",
    GH + "MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/all_sub.txt",
    GH + "MhdiTaheri/V2rayCollector/main/sub/mix",
    GH + "Pawdroid/Free-servers/main/sub",
    GH + "peasoft/NoMoreWalls/master/list_raw.txt",
    GH + "roosterkid/openproxylist/main/V2RAY_RAW.txt",
]
# Списки, подобранные под блокировки в России: (метка в названии сервера, файл, ссылка).
# Все они вместе попадают в одну подписку russia.txt — и для Wi-Fi, и для мобильного, и для белых списков.
IG = GH + "igareck/vpn-configs-for-russia/main/"
SOURCES_RU = [
    ("Whitelist", "russia_white", IG + "Vless-Reality-White-Lists-Rus-Mobile.txt"),
    ("Mobile", "russia_mobile", IG + "BLACK_VLESS_RUS_mobile.txt"),
    ("Wi-Fi", "russia_wifi", IG + "BLACK_VLESS_RUS.txt"),
    ("Reserve", "russia_reserve", IG + "BLACK_SS+All_RUS.txt"),
]
VPNGATE_URL = "https://www.vpngate.net/api/iphone/"

SCHEMES = ("vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "hy2", "tuic")
LINK_RE = re.compile(r"(?:%s)://[^\s<>\"'`]+" % "|".join(SCHEMES))


# ---------- загрузка ----------

def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def b64decode(s):
    s = re.sub(r"\s+", "", s)
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_")).decode("utf-8", "ignore")
    except Exception:
        return ""


def extract_links(text):
    if "://" not in text[:5000]:          # подписка целиком в base64
        decoded = b64decode(text)
        if "://" in decoded:
            text = decoded
    return [html.unescape(m.group(0)).rstrip(",;") for m in LINK_RE.finditer(text)]


def load(urls):
    links = []

    def one(u):
        try:
            got = extract_links(fetch(u))
            print(f"  + {len(got):6d}  {u}")
            return got
        except Exception as e:
            print(f"  ! ошибка {u}: {e}")
            return []

    with ThreadPoolExecutor(8) as ex:
        for got in ex.map(one, urls):
            links.extend(got)
    return links


# ---------- разбор ссылок ----------

def vmess_json(link):
    try:
        return json.loads(b64decode(link[len("vmess://"):]))
    except Exception:
        return None


def endpoint(link):
    """(протокол, хост, порт) или None, если ссылка битая."""
    scheme = link.split("://", 1)[0].lower()
    try:
        if scheme == "vmess":
            j = vmess_json(link)
            if not j:
                return None
            host, port = str(j.get("add", "")), int(j.get("port", 0))
        elif scheme == "ssr":
            raw = b64decode(link[len("ssr://"):])
            host, port = raw.split(":")[0], int(raw.split(":")[1])
        else:
            u = urllib.parse.urlsplit(link)
            host, port = u.hostname, u.port
            if scheme == "ss" and not host:      # ss://base64(method:pass@host:port)
                raw = b64decode(link[5:].split("#")[0])
                hp = raw.rsplit("@", 1)[-1]
                host, port = hp.rsplit(":", 1)[0], int(hp.rsplit(":", 1)[1])
        if scheme == "hy2":
            scheme = "hysteria2"
        if not host or not port or not (0 < int(port) < 65536):
            return None
        host = host.strip("[]").lower()
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return None                      # 127.0.0.1, 0.0.0.0, локальные сети — заглушки
        except ValueError:
            if "." not in host:
                return None
        return scheme, host, int(port)
    except Exception:
        return None


def dedup_key(link):
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        j = vmess_json(link) or {}
        return ("vmess", j.get("add"), str(j.get("port")), j.get("id"), j.get("net"), j.get("path"))
    return link.split("#", 1)[0]


def rename(link, name):
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        j = vmess_json(link)
        if j is None:
            return link
        j["ps"] = name
        return "vmess://" + base64.b64encode(json.dumps(j, ensure_ascii=False).encode()).decode()
    if scheme == "ssr":
        return link
    return link.split("#", 1)[0] + "#" + urllib.parse.quote(name)


def prepare(links):
    seen, items = set(), []
    for l in links:
        ep = endpoint(l)
        if not ep:
            continue
        k = dedup_key(l)
        if k in seen:
            continue
        seen.add(k)
        items.append({"link": l, "proto": ep[0], "host": ep[1], "port": ep[2]})
    return items


# ---------- проверка и рейтинг ----------

def params(item):
    """Тип защиты (reality / tls / none), транспорт и SNI сервера."""
    link = item["link"]
    if item["proto"] == "vmess":
        j = vmess_json(link) or {}
        sec = "tls" if j.get("tls") == "tls" else "none"
        return sec, j.get("net") or "tcp", j.get("sni") or j.get("host") or ""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
    sec = (q.get("security") or "").lower()
    if item["proto"] in ("trojan", "hysteria2", "tuic") and sec in ("", "none"):
        sec = "tls"
    return sec or "none", (q.get("type") or "tcp").lower(), q.get("sni") or q.get("host") or ""


def probe(item):
    """Подключается к серверу и, если он с TLS/Reality, проходит TLS-рукопожатие.
    Так отсеиваются не только выключенные серверы, но и «полуживые», которые
    принимают подключение, но не отвечают как VPN."""
    sec, net, sni = params(item)
    item.update(sec=sec, net=net)
    t = time.time()
    try:
        with socket.create_connection((item["host"], item["port"]), timeout=CHECK_TIMEOUT) as sock:
            item["ms"] = max(1, int((time.time() - t) * 1000))
            item["ip"] = sock.getpeername()[0]
            if sec in ("tls", "reality"):
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock.settimeout(CHECK_TIMEOUT)
                with ctx.wrap_socket(sock, server_hostname=sni or None):
                    pass
        item["ok"] = True
    except Exception:
        item["ok"] = False
        item["ms"] = None
    return item


# Адреса CDN (Cloudflare, Fastly и др.): они отвечают всегда, даже если сам VPN за ними
# выключен, поэтому такие серверы не попадают в ТОП и не считаются «быстрыми».
CDN_NETS = [ipaddress.ip_network(n) for n in (
    "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "162.158.0.0/15", "188.114.96.0/20",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "131.0.72.0/22",
    "151.101.0.0/16", "199.232.0.0/16", "146.75.0.0/16", "23.235.32.0/20", "43.249.72.0/22")]


def is_cdn(ip):
    try:
        a = ipaddress.ip_address(ip)
        return any(a in n for n in CDN_NETS)
    except Exception:
        return False


def check(items):
    tcp = [i for i in items if i["proto"] not in ("hysteria2", "tuic")][:MAX_CHECK]
    udp = [i for i in items if i["proto"] in ("hysteria2", "tuic")]
    with ThreadPoolExecutor(200) as ex:
        list(ex.map(probe, tcp))
    for i in tcp:
        i["cdn"] = is_cdn(i.get("ip") or i["host"])
    for i in udp:                      # UDP-протоколы так не проверить
        i.update(ok=None, ms=None, sec="tls", net="udp")
    return tcp + udp[:50]


PROTO_SCORE = {("vless", "reality"): 40, ("vless", "tls"): 30, ("trojan", "tls"): 26,
               ("vmess", "tls"): 20, ("hysteria2", "tls"): 18, ("tuic", "tls"): 16,
               ("ss", "none"): 14, ("vless", "none"): 8, ("vmess", "none"): 8}


def score(i):
    sc = PROTO_SCORE.get((i["proto"], i.get("sec")), 10)
    if i["port"] == 443:
        sc += 5
    if i.get("net") in ("grpc", "xhttp", "ws", "httpupgrade", "splithttp"):
        sc += 3
    sc += min(i.get("streak", 0), 10) * 3          # стабильно живёт много проверок подряд
    sc += min(i.get("sources", 1) - 1, 3) * 5      # встречается в нескольких списках
    if i.get("cdn"):
        sc -= 25                                   # за CDN не видно, жив ли сам сервер
    elif i.get("ms"):
        sc -= min(i["ms"] // 20, 30)               # чем быстрее отвечает, тем выше
    if i.get("ok") is False:
        sc -= 200
    elif i.get("ok") is None:
        sc -= 100
    return sc


# ---------- история (стабильность) ----------

HISTORY = os.path.join(ROOT, "history.json")
STABLE = 4          # живой 4 проверки подряд (~12 часов) — «стабильный»


def hkey(item):
    return hashlib.sha1(repr(dedup_key(item["link"])).encode()).hexdigest()[:12]


def apply_history(items):
    try:
        with open(HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    now = int(time.time())
    for i in items:
        k = hkey(i)
        h = hist.get(k, {"streak": 0})
        if i.get("ok"):
            h["streak"] = h.get("streak", 0) + 1
        elif i.get("ok") is False:
            h["streak"] = 0
        h["t"] = now
        hist[k] = h
        i["streak"] = h["streak"]
    hist = {k: v for k, v in hist.items() if now - v.get("t", 0) < 3 * 86400}
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))


# ---------- страна сервера ----------

GEO = os.path.join(ROOT, "geo.json")


def resolve(host):
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        try:
            return socket.gethostbyname(host)
        except Exception:
            return None


def add_countries(items):
    try:
        with open(GEO, encoding="utf-8") as f:
            geo = json.load(f)
    except Exception:
        geo = {}
    socket.setdefaulttimeout(5)
    with ThreadPoolExecutor(50) as ex:
        ips = list(ex.map(lambda i: i.get("ip") or resolve(i["host"]), items))
    socket.setdefaulttimeout(None)
    for i, ip in zip(items, ips):
        i["ip"] = ip
    need = sorted({ip for ip in ips if ip and ip not in geo})
    for n in range(0, min(len(need), 1400), 100):     # бесплатный ip-api: 100 адресов за запрос
        try:
            body = json.dumps([{"query": ip, "fields": "countryCode,query"} for ip in need[n:n + 100]]).encode()
            req = urllib.request.Request("http://ip-api.com/batch", data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                for row in json.loads(r.read().decode()):
                    if row.get("countryCode"):
                        geo[row["query"]] = row["countryCode"]
            time.sleep(4.5)                              # не больше 15 запросов в минуту
        except Exception as e:
            print(f"  ! страны не определились: {e}")
            break
    for i in items:
        i["cc"] = geo.get(i.get("ip") or "", "")
    if len(geo) > 20000:
        geo = dict(list(geo.items())[-20000:])
    with open(GEO, "w", encoding="utf-8") as f:
        json.dump(geo, f, separators=(",", ":"))


def flag(cc):
    if len(cc) != 2 or not cc.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())


# ---------- VPN Gate (OpenVPN) ----------

def vpngate():
    try:
        text = fetch(VPNGATE_URL, timeout=60)
    except Exception as e:
        print(f"  ! VPN Gate недоступен: {e}")
        return []
    rows = [l for l in text.splitlines() if l and not l.startswith("*")]
    if rows and rows[0].startswith("#"):
        rows[0] = rows[0][1:]
    servers = []
    for r in csv.DictReader(io.StringIO("\n".join(rows))):
        cfg = r.get("OpenVPN_ConfigData_Base64")
        if not cfg:
            continue
        try:
            servers.append({
                "ip": r["IP"], "country": r.get("CountryShort", "??"),
                "ping": int(r.get("Ping") or 9999), "speed": int(r.get("Speed") or 0),
                "score": int(r.get("Score") or 0),
                "config": base64.b64decode(cfg).decode("utf-8", "ignore"),
            })
        except Exception:
            continue
    servers.sort(key=lambda s: -s["score"])
    print(f"  + {len(servers):6d}  VPN Gate (OpenVPN)")
    return servers[:MAX_OVPN]


# ---------- сохранение ----------

FLAG = {"vless": "VLESS", "vmess": "VMess", "trojan": "Trojan", "ss": "SS",
        "ssr": "SSR", "hysteria2": "Hy2", "tuic": "TUIC"}


TOP_N = 10


def rank(items):
    """Сортирует серверы и вешает значки: 🔥 ТОП, ⚡ быстрый, ⭐ стабильный, ✅ работает."""
    for i in items:
        i["score"] = score(i)
    items.sort(key=lambda i: -i["score"])
    top = 0
    for i in items:
        if i.get("ok") and not i.get("cdn"):
            top += 1
            if top <= TOP_N:
                i["badge"] = f"🔥 TOP-{top}"
            elif i.get("ms") and i["ms"] < 150:
                i["badge"] = "⚡"
            else:
                i["badge"] = "✅"
        elif i.get("ok"):
            i["badge"] = "☁️"                    # через CDN: доступен, но не проверить до конца
        elif i.get("tag") == "Whitelist":
            i["badge"] = "📱"
        else:
            i["badge"] = "🟡"
    return items


def label(i):
    proto = "Reality" if i.get("sec") == "reality" else FLAG.get(i["proto"], i["proto"])
    parts = [f"{i.get('badge', '✅')} {flag(i.get('cc', ''))}"]
    if i.get("tag"):
        parts.append(i["tag"])
    parts.append(proto)
    if i.get("ok") and i.get("ms") and not i.get("cdn"):
        parts.append(f"{i['ms']}ms")
    name = " · ".join(parts)
    if i.get("ok") and i.get("streak", 0) >= STABLE:
        name += " ⭐"
    return name


def write_sub(name, items, title):
    links = [rename(i["link"], label(i)) for i in items]
    header = (f"#profile-title: {title}\n#profile-update-interval: 3\n"
              f"#Updated: {datetime.now(MSK):%d.%m.%Y %H:%M} MSK, servers: {len(links)}\n"
              f"#🔥 TOP — best now · ⚡ fast · ⭐ stable · ✅ working · "
              f"☁️ via CDN · 📱 whitelist · 🟡 unchecked\n")
    body = "\n".join(links)
    with open(os.path.join(OUT, name + ".txt"), "w", encoding="utf-8") as f:
        f.write(header + body + "\n")
    with open(os.path.join(OUT, name + "_base64.txt"), "w", encoding="utf-8") as f:
        f.write(base64.b64encode(body.encode()).decode())
    return len(links)


def main():
    do_check = "--no-check" not in sys.argv
    os.makedirs(OUT, exist_ok=True)

    print("Скачиваю общие списки:")
    raw = load(SOURCES)
    common = prepare(raw)
    counts = {}
    for l in raw:                                   # в скольких списках встречается сервер
        k = dedup_key(l)
        counts[k] = counts.get(k, 0) + 1
    for i in common:
        i["sources"] = counts.get(dedup_key(i["link"]), 1)

    print("Скачиваю списки для России:")
    russia, seen = [], set()
    for tag, fname, url in SOURCES_RU:
        for i in prepare(load([url])):
            k = dedup_key(i["link"])
            if k not in seen:            # один сервер — только в одной группе
                seen.add(k)
                i["tag"] = tag
                i["group"] = fname
                russia.append(i)
    print(f"Уникальных: общие {len(common)}, для России {len(russia)}")

    if do_check:
        print("Проверяю серверы (подключение + TLS-рукопожатие)…")
        t = time.time()
        common = check(common)
        russia = check(russia)
        print(f"Отвечают: общие {sum(1 for i in common if i.get('ok'))}, "
              f"для России {sum(1 for i in russia if i.get('ok'))} из {len(russia)} "
              f"({int(time.time() - t)} c)")
        # Неответившие российские серверы НЕ выкидываем: серверы для белых списков стоят в РФ
        # и часто не отвечают на проверку из-за границы, но у пользователей в России работают.
        common = [i for i in common if i.get("ok") is not False]
        apply_history(common + russia)
    else:
        for i in common + russia:
            i["ok"] = None
            i["sec"], i["net"], _ = params(i)

    common = rank(common)
    by_proto = {p: [i for i in common if i["proto"] == p][:MAX_PER_PROTO] for p in ("vless", "vmess", "trojan", "ss")}
    common = common[:MAX_SERVERS]
    russia = rank(russia)
    print("Определяю страны серверов…")
    add_countries(list({id(i): i for i in common + russia + sum(by_proto.values(), [])}.values()))

    stats = {"all": write_sub("all", common, "Halyava VPN")}
    for tag, fname, _ in SOURCES_RU:
        stats[fname] = write_sub(fname, [i for i in russia if i["group"] == fname][:MAX_RU],
                                 f"Halyava VPN · {tag}")
    # Главная подписка: всё в одной ссылке, лучшие серверы сверху
    stats["russia"] = write_sub("russia", russia, "Halyava VPN")
    for proto in ("vless", "vmess", "trojan", "ss"):
        stats[proto] = write_sub(proto, by_proto[proto], f"Halyava VPN · {FLAG[proto]}")
    stats["top"] = [{"name": label(i), "proto": i["proto"], "cc": i.get("cc", ""), "ms": i.get("ms"),
                     "tag": i.get("tag", ""), "type": "Reality" if i.get("sec") == "reality" else FLAG.get(i["proto"], i["proto"]),
                     "stable": i.get("streak", 0) >= STABLE} for i in russia if i.get("badge", "").startswith("🔥")]
    stats["working"] = sum(1 for i in russia if i.get("ok"))

    print("Скачиваю VPN Gate:")
    ovpn = vpngate()
    if ovpn:
        os.makedirs(OVPN_DIR, exist_ok=True)
        for f in os.listdir(OVPN_DIR):
            if f.endswith(".ovpn"):
                os.remove(os.path.join(OVPN_DIR, f))
        for n, s in enumerate(ovpn, 1):
            fn = f"{n:02d}_{s['country']}_{s['ip']}.ovpn"
            with open(os.path.join(OVPN_DIR, fn), "w", encoding="utf-8") as f:
                f.write(s["config"])
        stats["openvpn"] = len(ovpn)

    stats["updated"] = datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")
    with open(os.path.join(OUT, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print("Готово:", stats)
    if not common and do_check:
        print("Ни одного рабочего сервера — что-то не так с источниками или сетью.")
        sys.exit(1)


if __name__ == "__main__":
    main()
