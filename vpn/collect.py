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
import html
import io
import ipaddress
import json
import os
import re
import socket
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
    ("Белые списки", "russia_white", IG + "Vless-Reality-White-Lists-Rus-Mobile.txt"),
    ("Моб. интернет", "russia_mobile", IG + "BLACK_VLESS_RUS_mobile.txt"),
    ("Wi-Fi", "russia_wifi", IG + "BLACK_VLESS_RUS.txt"),
    ("Запасной", "russia_reserve", IG + "BLACK_SS+All_RUS.txt"),
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


# ---------- проверка ----------

def ping(item):
    t = time.time()
    try:
        with socket.create_connection((item["host"], item["port"]), timeout=CHECK_TIMEOUT):
            item["ms"] = int((time.time() - t) * 1000)
    except Exception:
        item["ms"] = None
    return item


def check(items):
    # UDP-протоколы (hysteria2, tuic) по TCP не проверить — оставляем без пинга в конце списка
    tcp = [i for i in items if i["proto"] not in ("hysteria2", "tuic")][:MAX_CHECK]
    udp = [i for i in items if i["proto"] in ("hysteria2", "tuic")]
    with ThreadPoolExecutor(200) as ex:
        alive = [i for i in ex.map(ping, tcp) if i["ms"] is not None]
    alive.sort(key=lambda i: i["ms"])
    for i in udp:
        i["ms"] = None
    return alive, udp[:50]


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


def write_sub(name, items, title):
    links = []
    for n, i in enumerate(items, 1):
        ms = f" {i['ms']}ms" if i.get("ms") else ""
        tag = f"{i['tag']} | " if i.get("tag") else ""
        links.append(rename(i["link"], f"Халява {n} | {tag}{FLAG.get(i['proto'], i['proto'])}{ms}"))
    header = (f"#profile-title: {title}\n#profile-update-interval: 3\n"
              f"#Обновлено: {datetime.now(MSK):%d.%m.%Y %H:%M} МСК, серверов: {len(links)}\n")
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
    common = prepare(load(SOURCES))
    print("Скачиваю списки для России:")
    groups, seen = [], set()
    for tag, fname, url in SOURCES_RU:
        part = []
        for i in prepare(load([url])):
            k = dedup_key(i["link"])
            if k not in seen:            # один сервер — только в одной группе
                seen.add(k)
                i["tag"] = tag
                part.append(i)
        groups.append((tag, fname, part))
    print(f"Уникальных: общие {len(common)}, для России {sum(len(g[2]) for g in groups)}")

    common_udp = []
    if do_check:
        print("Проверяю серверы…")
        t = time.time()
        common, common_udp = check(common)
        # Российские списки не проверяем: серверы для белых списков стоят в РФ и не отвечают
        # на проверку с зарубежных серверов GitHub, а автор списков проверяет их сам.
        print(f"Живых: общие {len(common)}, для России {sum(len(g[2]) for g in groups)} ({int(time.time() - t)} c)")

    stats = {"all": write_sub("all", common[:MAX_SERVERS] + common_udp, "🆓 Халява VPN — все")}
    russia = []
    for tag, fname, part in groups:
        part = part[:MAX_RU]
        stats[fname] = write_sub(fname, part, f"🇷🇺 Халява VPN — {tag}")
        russia += part
    # Главная подписка: Wi-Fi + мобильный интернет + белые списки + запасные — всё в одной ссылке
    stats["russia"] = write_sub("russia", russia, "🇷🇺 Халява VPN — Wi-Fi + мобильный + белые списки")
    for proto in ("vless", "vmess", "trojan", "ss"):
        part = [i for i in common if i["proto"] == proto][:MAX_PER_PROTO]
        stats[proto] = write_sub(proto, part, f"🆓 Халява VPN — {FLAG[proto]}")

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
    if stats["all"] == 0 and do_check:
        print("Ни одного рабочего сервера — что-то не так с источниками или сетью.")
        sys.exit(1)


if __name__ == "__main__":
    main()
