#!/usr/bin/env python3
"""
hayk@porto terminal profile renderer.

Pulls live data (GitHub GraphQL + REST, production health checks, Porto
weather) and renders a set of animated, terminal-style SVG panels that look
like one tmux session:

    0:boot  1:neofetch  2:systemctl  3:htop  4:chrono  5:pulse  (+ matrix footer)

Pure stdlib, no dependencies. Run by .github/workflows/terminal.yml every hour.

    python terminal/generate.py --out out           # live (needs GH_TOKEN)
    python terminal/generate.py --out out --demo    # offline sample data
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
USER = os.environ.get("GH_USER", "haykeminyan")
TZ = ZoneInfo("Europe/Lisbon")
PORTO = (41.1496, -8.6110)
SITES = [
    ("shoplux", "https://shoplux.casa", "async e-commerce platform"),
    ("exhibitions", "https://exhibitions.baregorc.com", "exhibitions aggregator"),
    ("baregorc", "https://baregorc.com", "NGO directory platform"),
    ("history", "https://history.baregorc.com", "history of Armenia, ancient times to today"),
]
HISTORY_DAYS = 30
UA = "Mozilla/5.0 (compatible; hayk-terminal-profile/1.0; +https://github.com/haykeminyan)"

W = 880          # every panel shares the same width so they stack like panes
PAD = 22
CW = 8.4         # nominal monospace char width @ 14px (used for column layout)
LH = 20          # line height

# palette (phosphor green CRT)
BG = "#05050a"
BG2 = "#0a120e"
GREEN = "#3ddc84"
DIM = "#2f5a44"
TEXT = "#7fcda3"
BRIGHT = "#baf7d5"
WHITE = "#e8fff2"
CYAN = "#5ccfe6"
AMBER = "#ffcc66"
RED = "#ff5f56"
MAGENTA = "#c792ea"
BLUE = "#6c9ef8"

WINDOWS = ["boot", "neofetch", "shoplux", "systemctl", "htop", "chrono", "pulse"]


# --------------------------------------------------------------------------- #
# http helpers
# --------------------------------------------------------------------------- #
def http_json(url: str, *, data: dict | None = None, token: str | None = None, timeout: int = 30):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


GQL = """
query($login: String!) {
  user(login: $login) {
    name login createdAt
    followers { totalCount }
    following { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount totalDiskUsage
      nodes {
        name isFork isArchived isPrivate stargazerCount forkCount diskUsage createdAt pushedAt
        primaryLanguage { name }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
      }
    }
    contributionsCollection {
      totalCommitContributions restrictedContributionsCount
      totalPullRequestContributions totalIssueContributions
      commitContributionsByRepository(maxRepositories: 50) {
        repository { name isPrivate }
        contributions { totalCount }
      }
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_github(token: str) -> dict:
    res = http_json("https://api.github.com/graphql", data={"query": GQL, "variables": {"login": USER}}, token=token)
    if res.get("errors"):
        raise RuntimeError(f"graphql errors: {res['errors']}")
    u = res["data"]["user"]
    repos = u["repositories"]["nodes"]
    cc = u["contributionsCollection"]

    langs: Counter = Counter()
    for r in repos:
        if r["isFork"]:
            continue
        for e in r["languages"]["edges"]:
            langs[e["node"]["name"]] += e["size"]

    by_repo = {c["repository"]["name"]: c["contributions"]["totalCount"] for c in cc["commitContributionsByRepository"]}
    days = [(d["date"], d["contributionCount"]) for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]

    return {
        "name": u["name"] or USER,
        "created_at": u["createdAt"],
        "followers": u["followers"]["totalCount"],
        "following": u["following"]["totalCount"],
        "contributed_to": u["repositoriesContributedTo"]["totalCount"],
        "repo_count": u["repositories"]["totalCount"],
        "disk_kb": u["repositories"]["totalDiskUsage"],
        "repos": [
            {
                "name": r["name"], "fork": r["isFork"], "archived": r["isArchived"], "private": r["isPrivate"],
                "stars": r["stargazerCount"], "forks": r["forkCount"], "disk_kb": r["diskUsage"] or 0,
                "created_at": r["createdAt"], "pushed_at": r["pushedAt"],
                "lang": (r["primaryLanguage"] or {}).get("name"), "commits": by_repo.get(r["name"], 0),
            }
            for r in repos
        ],
        "languages": dict(langs),
        "commits_year": cc["totalCommitContributions"] + cc["restrictedContributionsCount"],
        "prs": cc["totalPullRequestContributions"],
        "issues": cc["totalIssueContributions"],
        "calendar": days,
    }


def fetch_commit_times(token: str, pages: int = 5) -> list[str]:
    """Author timestamps (with original tz offset) of recent public commits."""
    out: list[str] = []
    for page in range(1, pages + 1):
        url = (f"https://api.github.com/search/commits?q=author:{USER}"
               f"&sort=author-date&order=desc&per_page=100&page={page}")
        try:
            res = http_json(url, token=token)
        except urllib.error.HTTPError as e:  # 422 past the last page, 403 rate limit
            print(f"  search/commits page {page}: HTTP {e.code}", file=sys.stderr)
            break
        items = res.get("items", [])
        out += [i["commit"]["author"]["date"] for i in items]
        if len(items) < 100:
            break
        time.sleep(2.5)  # search API: 30 req/min
    return out


def probe(url: str) -> dict:
    t0 = time.perf_counter()
    code = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
            r.read(2048)
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # dns, tls, timeout
        print(f"  probe {url}: {type(e).__name__}: {e}", file=sys.stderr)
        code = 0
    ms = int((time.perf_counter() - t0) * 1000)
    return {"t": int(time.time()), "s": code, "ms": ms}


WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm + hail", 99: "thunderstorm + hail",
}


def fetch_weather() -> dict | None:
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={PORTO[0]}&longitude={PORTO[1]}"
           "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&timezone=Europe%2FLisbon")
    try:
        c = http_json(url, timeout=15)["current"]
        return {"temp": c["temperature_2m"], "hum": c["relative_humidity_2m"],
                "desc": WMO.get(c["weather_code"], "weather"), "wind": c["wind_speed_10m"]}
    except Exception as e:
        print(f"  weather: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# demo data (offline preview) - seeded from the real metrics card
# --------------------------------------------------------------------------- #
def demo_github() -> dict:
    rnd = random.Random(7)
    today = dt.date.today()
    cal = []
    for i in range(370, -1, -1):
        d = today - dt.timedelta(days=i)
        base = 0 if rnd.random() < 0.28 else int(rnd.expovariate(1 / 4.5))
        if d.weekday() >= 5:
            base //= 2
        cal.append((d.isoformat(), min(base, 53)))
    for i in range(8):
        d, c = cal[-1 - i]
        cal[-1 - i] = (d, max(c, 2))
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda days: (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = [
        ("shoplux", "Python", 1, 412, 51200, 120, True),
        ("culture_aggregator", "Python", 4, 600, 48000, 0, False),
        ("baregorc", "Python", 9, 380, 30500, 0, True),
        ("haykeminyan", None, 0, 900, 900, 0, False),
        ("ml-serving-k8s", "Jupyter Notebook", 60, 1400, 420000, 0, False),
        ("price-api", "Python", 120, 1500, 22000, 0, False),
        ("angular-dashboard", "TypeScript", 200, 1300, 64000, 0, False),
        ("realty-scraper", "Python", 300, 1900, 9000, 0, False),
        ("dotfiles", "Shell", 25, 2200, 200, 0, False),
        ("landing-scss", "SCSS", 500, 1700, 7000, 0, False),
        ("old-flask-blog", "HTML", 900, 2600, 3000, 0, False),
    ]
    rr = []
    commits_left = [1700, 1100, 800, 90, 0, 0, 0, 0, 12, 0, 0]
    for (n, lang, pushed, created, kb, _, priv), c in zip(repos, commits_left):
        rr.append({"name": n, "fork": False, "archived": n == "old-flask-blog", "private": priv,
                   "stars": 1 if n == "culture_aggregator" else 0, "forks": 1 if n == "culture_aggregator" else 0,
                   "disk_kb": kb, "created_at": iso(created), "pushed_at": iso(pushed), "lang": lang, "commits": c})
    return {
        "name": "Hayk Eminyan", "created_at": iso(8 * 365 + 95), "followers": 7, "following": 9,
        "contributed_to": 18, "repo_count": 47, "disk_kb": 2_090_000, "repos": rr,
        "languages": {"Python": 6_100_000, "Jupyter Notebook": 1_900_000, "HTML": 820_000, "CSS": 430_000,
                      "TypeScript": 390_000, "JavaScript": 260_000, "SCSS": 90_000, "Less": 30_000},
        "commits_year": 4242, "prs": 13, "issues": 1, "calendar": cal,
    }


def demo_commit_times() -> list[str]:
    rnd = random.Random(3)
    weights = [6, 4, 3, 1, 0.5, 0.3, 0.3, 0.8, 2, 4, 6, 7, 5, 6, 7, 7, 6, 6, 5, 6, 8, 9, 10, 9]
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=0)))
    out = []
    for _ in range(500):
        h = rnd.choices(range(24), weights)[0]
        d = now - dt.timedelta(days=rnd.randint(0, 200))
        out.append(d.replace(hour=h, minute=rnd.randint(0, 59)).isoformat())
    return out


def demo_history(now: int) -> dict:
    rnd = random.Random(11)
    hist = {}
    for name, _, _ in SITES:
        rows = []
        for i in range(HISTORY_DAYS * 24, 0, -1):
            t = now - i * 3600
            down = rnd.random() < (0.004 if name != "travelculturehub" else 0.01)
            if name == "travelculturehub" and i < 30:
                down = True
            rows.append({"t": t, "s": 0 if down else 200, "ms": int(rnd.gauss(380, 90))})
        hist[name] = rows
    return hist


# --------------------------------------------------------------------------- #
# derived metrics
# --------------------------------------------------------------------------- #
def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def streaks(cal: list[tuple[str, int]]) -> tuple[int, int]:
    vals = [c for _, c in cal]
    best = run = 0
    for v in vals:
        run = run + 1 if v > 0 else 0
        best = max(best, run)
    cur = 0
    i = len(vals) - 1
    if i >= 0 and vals[i] == 0:  # today not yet committed doesn't break the streak
        i -= 1
    while i >= 0 and vals[i] > 0:
        cur += 1
        i -= 1
    return cur, best


def human_kb(kb: float) -> str:
    for unit, div in (("G", 1024 * 1024), ("M", 1024), ("K", 1)):
        if kb >= div:
            v = kb / div
            return f"{v:.2f}{unit}" if unit == "G" else f"{v:.0f}{unit}" if v >= 10 else f"{v:.1f}{unit}"
    return "0K"


def ago(t: dt.datetime, now: dt.datetime) -> str:
    s = (now - t).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    d = int(s // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


LANG_SHORT = {"Python": "py", "Jupyter Notebook": "ipynb", "HTML": "html", "CSS": "css", "TypeScript": "ts",
              "JavaScript": "js", "SCSS": "scss", "Less": "less", "Shell": "sh", "Go": "go", "Dockerfile": "dock",
              "Makefile": "make", "Mako": "mako", "Jinja": "jinja", "Rust": "rs", "C++": "c++", "Java": "java"}


def lang_cmd(lang: str | None, name: str) -> str:
    return {
        "Python": f"python3 -m {name.replace('-', '_')}",
        "Jupyter Notebook": f"jupyter lab {name}.ipynb",
        "TypeScript": f"node {name}/dist/main.js",
        "JavaScript": f"node {name}/index.js",
        "HTML": f"nginx -g 'root /srv/{name};'",
        "CSS": f"sass --watch {name}",
        "SCSS": f"sass --watch {name}/scss",
        "Shell": f"/bin/zsh {name}/install.sh",
        "Go": f"./{name}",
    }.get(lang or "", f"cat {name}/README.md")


# --------------------------------------------------------------------------- #
# svg primitives
# --------------------------------------------------------------------------- #
def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def tspans(segs) -> str:
    """segs: list of (text, css-class) -> tspans."""
    return "".join(f'<tspan class="{c}">{esc(t)}</tspan>' if c else esc(t) for t, c in segs)


def text(x, y, segs, cls="", delay=None, anchor=None, extra=""):
    if isinstance(segs, str):
        segs = [(segs, "")]
    a = f' text-anchor="{anchor}"' if anchor else ""
    st = f' style="animation-delay:{delay:.2f}s"' if delay is not None else ""
    c = (cls + (" a" if delay is not None else "")).strip()
    cattr = f' class="{c}"' if c else ""
    return f'<text x="{x:.1f}" y="{y:.1f}"{cattr}{a}{st}{extra}>{tspans(segs)}</text>'


def typed(x, y, segs_prompt, cmd, begin=0.2, cps=0.045, cursor=True):
    """Prompt is shown immediately, command is typed char by char."""
    parts = [tspans(segs_prompt)]
    for i, ch in enumerate(cmd):
        parts.append(f'<tspan class="w t" style="animation-delay:{begin + i * cps:.2f}s">{esc(ch)}</tspan>')
    end = begin + len(cmd) * cps
    if cursor:
        parts.append(f'<tspan class="g cur" style="animation-delay:{end:.2f}s"> █</tspan>')
    return f'<text x="{x}" y="{y}">{"".join(parts)}</text>', end


PROMPT = [("hayk", "g b"), ("@", "d"), ("porto", "c"), (":", "d"), ("~", "m"), ("$ ", "w")]

BASE_CSS = f"""
text{{font-family:'JetBrains Mono','Fira Code','Cascadia Code','SF Mono',Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace;font-size:14px;fill:{TEXT};white-space:pre}}
.g{{fill:{GREEN}}}.b{{font-weight:700}}.d{{fill:{DIM}}}.c{{fill:{CYAN}}}.y{{fill:{AMBER}}}.r{{fill:{RED}}}
.m{{fill:{MAGENTA}}}.w{{fill:{WHITE}}}.l{{fill:{BRIGHT}}}.bl{{fill:{BLUE}}}.k{{fill:{BG}}}
.o{{fill-opacity:.55}}.sm{{font-size:11px}}.xs{{font-size:10px}}.lg{{font-size:16px}}
.glow{{filter:url(#glow)}}
@keyframes on{{from{{opacity:0}}to{{opacity:1}}}}
@keyframes ton{{from{{fill-opacity:0}}to{{fill-opacity:1}}}}
@keyframes blink{{0%,49%{{fill-opacity:1}}50%,100%{{fill-opacity:0}}}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
@keyframes flicker{{0%,100%{{opacity:.06}}50%{{opacity:.03}}}}
.a{{animation:on .08s linear both}}
.t{{animation:ton .01s linear both}}
.cur{{animation:blink 1.05s step-end infinite}}
.pulse{{animation:pulse 1.6s ease-in-out infinite}}
.flick{{animation:flicker 4s ease-in-out infinite}}
"""


def doc(h: int, body: str, css: str = "", title: str = "") -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" xml:space="preserve" role="img" aria-label="{esc(title)}">
<title>{esc(title)}</title>
<defs>
<style>{BASE_CSS}{css}</style>
<filter id="glow" x="-10%" y="-30%" width="120%" height="160%"><feGaussianBlur stdDeviation="1.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<filter id="bigglow" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<pattern id="scan" width="3" height="3" patternUnits="userSpaceOnUse"><rect width="3" height="1" fill="#000" opacity=".35"/></pattern>
<pattern id="pipes" width="4" height="10" patternUnits="userSpaceOnUse"><rect width="2" height="10" fill="{GREEN}"/></pattern>
<pattern id="pipes-b" width="4" height="10" patternUnits="userSpaceOnUse"><rect width="2" height="10" fill="{BLUE}"/></pattern>
<pattern id="pipes-y" width="4" height="10" patternUnits="userSpaceOnUse"><rect width="2" height="10" fill="{AMBER}"/></pattern>
<pattern id="pipes-r" width="4" height="10" patternUnits="userSpaceOnUse"><rect width="2" height="10" fill="{RED}"/></pattern>
<pattern id="pipes-m" width="4" height="10" patternUnits="userSpaceOnUse"><rect width="2" height="10" fill="{MAGENTA}"/></pattern>
<radialGradient id="vig" cx="50%" cy="45%" r="75%"><stop offset="60%" stop-color="#000" stop-opacity="0"/><stop offset="100%" stop-color="#000" stop-opacity=".55"/></radialGradient>
<linearGradient id="phos" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BRIGHT}"/><stop offset=".5" stop-color="{GREEN}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
<clipPath id="screen"><rect x="1" y="1" width="{W - 2}" height="{h - 2}" rx="10"/></clipPath>
</defs>
<g clip-path="url(#screen)">
<rect width="{W}" height="{h}" fill="{BG}"/>
<rect width="{W}" height="{h}" fill="{GREEN}" class="flick"/>
{body}
<rect width="{W}" height="{h}" fill="url(#scan)" pointer-events="none"/>
<rect width="{W}" height="{h}" fill="url(#vig)" pointer-events="none"/>
</g>
<rect x="1" y="1" width="{W - 2}" height="{h - 2}" rx="10" fill="none" stroke="{GREEN}" stroke-opacity=".35"/>
</svg>
"""


def tmux_bar(h: int, active: int, now: dt.datetime) -> str:
    y = h - 22
    wins = []
    for i, n in enumerate(WINDOWS):
        if i == active:
            wins.append((f" [{i}:{n}*] ", "k b"))
        else:
            wins.append((f" {i}:{n} ", "k o"))
    left = [("[hayk] ", "k b")] + wins
    right = [(f'"porto.pt" {now:%H:%M} {now:%d-%b-%y}', "k")]
    return (f'<rect x="0" y="{y}" width="{W}" height="22" fill="{GREEN}" opacity=".92"/>'
            f'{text(10, y + 15.5, left, "sm")}{text(W - 10, y + 15.5, right, "sm", anchor="end")}')


# 5x7 pixel font for the hero banner - font independent, crisp everywhere
FONT = {
    "H": ["1...1", "1...1", "1...1", "11111", "1...1", "1...1", "1...1"],
    "A": [".111.", "1...1", "1...1", "11111", "1...1", "1...1", "1...1"],
    "Y": ["1...1", "1...1", ".1.1.", "..1..", "..1..", "..1..", "..1.."],
    "K": ["1...1", "1..1.", "1.1..", "11...", "1.1..", "1..1.", "1...1"],
    "E": ["11111", "1....", "1....", "1111.", "1....", "1....", "11111"],
    "M": ["1...1", "11.11", "1.1.1", "1.1.1", "1...1", "1...1", "1...1"],
    "I": [".111.", "..1..", "..1..", "..1..", "..1..", "..1..", ".111."],
    "N": ["1...1", "11..1", "1.1.1", "1..11", "1...1", "1...1", "1...1"],
    " ": ["..", "..", "..", "..", "..", "..", ".."],
}


def pixel_banner(word: str, x0: float, y0: float, p: float, begin: float) -> tuple[str, float]:
    cols: list[list[int]] = []  # column bitmaps
    for ch in word:
        g = FONT[ch]
        for cx in range(len(g[0])):
            cols.append([1 if g[r][cx] == "1" else 0 for r in range(7)])
        cols.append([0] * 7)
    cols.pop()
    shadow, main = [], []
    for ci, col in enumerate(cols):
        d = begin + ci * 0.018
        rs = [r for r in range(7) if col[r]]
        if not rs:
            continue
        sh = "".join(f'<rect x="{x0 + ci * p + 3:.1f}" y="{y0 + r * p + 3:.1f}" width="{p - 1:.1f}" height="{p - 1:.1f}"/>' for r in rs)
        mn = "".join(f'<rect x="{x0 + ci * p:.1f}" y="{y0 + r * p:.1f}" width="{p - 1:.1f}" height="{p - 1:.1f}"/>' for r in rs)
        shadow.append(f'<g class="a" style="animation-delay:{d:.2f}s">{sh}</g>')
        main.append(f'<g class="a" style="animation-delay:{d:.2f}s">{mn}</g>')
    svg = (f'<g fill="{DIM}">{"".join(shadow)}</g>'
           f'<g fill="url(#phos)" filter="url(#bigglow)">{"".join(main)}</g>')
    return svg, len(cols) * p


# --------------------------------------------------------------------------- #
# panels
# --------------------------------------------------------------------------- #
def ok_tag(kind="ok"):
    return {
        "ok": [("[", "w"), ("  OK  ", "g b"), ("] ", "w")],
        "fail": [("[", "w"), ("FAILED", "r b"), ("] ", "w")],
        "warn": [("[", "w"), (" WARN ", "y b"), ("] ", "w")],
    }[kind]


def panel_boot(ctx) -> str:
    g, now = ctx["gh"], ctx["now"]
    h = 430
    rnd = random.Random(int(now.strftime("%Y%m%d%H")))
    lines = [
        [("HaykBIOS v5.0  (C) 1995-2026  Mech&Math Dept., Lomonosov Moscow State University", "w")],
        [("CPU  : ", "d"), ("CPython 3.13 async-core  x  5+ years production", "g")],
        [("RAM  : ", "d"), (f"{g['repo_count']} repositories  /  {human_kb(g['disk_kb'])}B  ", "g"), ("OK", "l b")],
        [("BUS  : ", "d"), ("/dev/fastapi /dev/django /dev/postgres /dev/redis /dev/k8s", "g")],
        [("Booting from ", "y"), (f"github.com/{USER}", "c"), (" ...", "y")],
        [],
    ]
    kt = 0.0
    for msg in ("Linux version 6.12-porto (hayk@ararat) (python 3.13) #1 SMP PREEMPT_DYNAMIC",
                "Command line: BOOT_IMAGE=/vmlinuz root=/dev/github ro quiet coffee=on",
                f"Memory: {g['commits_year']}K commits available (last 365d)"):
        kt += rnd.uniform(0.0003, 0.4)
        lines.append([(f"[{kt:12.6f}] ", "d"), (msg, "")])
    lines.append(ok_tag() + [("Mounted ", ""), (f"/home/hayk/repos", "l"), (f" ({g['repo_count']} repos, {human_kb(g['disk_kb'])}B)", "")])
    lines.append(ok_tag() + [("Started ", ""), ("postgresql@16.service", "l"), (" - PostgreSQL RDBMS", "")])
    lines.append(ok_tag() + [("Started ", ""), ("redis-server.service", "l"), (" + ", "d"), ("celery-worker@shoplux.service", "l")])
    lines.append(ok_tag() + [("Started ", ""), ("rabbitmq-server.service", "l"), (" + ", "d"), ("pydantic-ai@llm-validators.service", "l")])
    up = 0
    for name, url, _ in SITES:
        p = ctx["probes"][name]
        if 200 <= p["s"] < 400:
            up += 1
            lines.append(ok_tag() + [("Started ", ""), (f"{name}.service", "l"), (f" - {url.split('//')[1]} ", ""),
                                     (f"HTTP {p['s']} {p['ms']}ms", "g")])
        else:
            why = f"HTTP {p['s']}" if p["s"] else "unreachable"
            lines.append(ok_tag("fail") + [("Failed to start ", ""), (f"{name}.service", "r"), (f" - {url.split('//')[1]} ", ""), (why, "r b")])
    tgt = "ok" if up == len(SITES) else "warn"
    lines.append(ok_tag(tgt) + [("Reached target ", ""), ("production.target", "l"), (f" ({up}/{len(SITES)} live)", "g" if tgt == "ok" else "y")])
    lines.append(ok_tag() + [("Started ", ""), ("open-to-work.service", "l"), (" - remote / hybrid, EU teams", "")])

    body = []
    y = 30
    for i, segs in enumerate(lines):
        if segs:
            body.append(text(PAD, y, segs, "sm", delay=0.15 + i * 0.13))
        y += 17.5
    t_overlay = 0.15 + len(lines) * 0.13 + 0.5

    # overlay: the "login screen" painted on top of the boot log
    ov = [f'<rect width="{W}" height="{h}" fill="{BG}" fill-opacity=".9" class="a" style="animation-delay:{t_overlay:.2f}s"/>']
    p = 10.0
    svg, bw = pixel_banner("HAYK EMINYAN", 0, 0, p, t_overlay + 0.1)
    bx = (W - bw) / 2
    ov.append(f'<g transform="translate({bx:.1f},62)">{svg}</g>')
    t2 = t_overlay + 0.1 + 70 * 0.018 + 0.2
    cy = 62 + 7 * p + 46
    ov.append(text(W / 2, cy, [("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "d")], anchor="middle", delay=t2))
    ov.append(text(W / 2, cy + 30, [(">> ", "d"), ("python backend engineer", "l b"), ("  ::  ", "d"), ("fastapi / django / async / k8s", "g")],
                   "lg glow", anchor="middle", delay=t2 + 0.25))
    ov.append(text(W / 2, cy + 56, [(">> ", "d"), ("ships solo, from ", ""), ("empty repo", "c"), (" to ", ""), ("real users", "c"),
                                    ("  ::  ", "d"), ("porto, portugal", "m")], anchor="middle", delay=t2 + 0.5))
    created = parse_iso(g["created_at"])
    up_days = (now - created.astimezone(TZ)).days
    cur, best = ctx["streak"]
    chips = [("[ ", "d"), ("uptime ", ""), (f"{up_days}d", "l b"), (" ]  [ ", "d"), ("repos ", ""), (str(g["repo_count"]), "l b"),
             (" ]  [ ", "d"), ("commits/yr ", ""), (str(g["commits_year"]), "l b"), (" ]  [ ", "d"), ("streak ", ""), (f"{cur}d", "l b"),
             (" ]  [ ", "d"), ("prod ", ""), (f"{up}/{len(SITES)}", "g b" if up == len(SITES) else "y b"), (" ]", "d")]
    ov.append(text(W / 2, cy + 96, chips, anchor="middle", delay=t2 + 0.8))
    tline, _ = typed(PAD, h - 40, PROMPT, "ssh recruiter@your-company --hire hayk", begin=t2 + 1.3, cps=0.05)
    ov.append(f'<g class="a" style="animation-delay:{t2 + 1.2:.2f}s">{tline}</g>')

    return doc(h, "".join(body) + "".join(ov) + tmux_bar(h, 0, now), title="hayk@porto boot sequence")


def ararat_rows(w: int = 44, h: int = 11) -> list[str]:
    """Procedural ASCII of Mount Ararat: Masis (big cone) + Sis (small cone) with glacier tongues."""
    def H(x):
        return max(10.6 - abs(x - 15) * 0.78, 6.2 - abs(x - 33) * 0.72, 0.6)

    rows = []
    for r in range(h):
        lvl = h - r - 0.5
        line = []
        for x in range(w):
            hx, hl, hr = H(x), H(x - 1), H(x + 1)
            if lvl > hx:
                line.append(" ")
            elif lvl > hx - 1:
                line.append("^" if hl < hx > hr else "/" if hl < hr else "\\" if hl > hr else "_")
            else:
                big, small = x < 25, x >= 25
                if (big and lvl > 5.4) or (small and lvl > 3.6):
                    line.append("#")
                elif (big and lvl > 5.4 - (2 if x % 4 == 1 else 1 if x % 3 == 0 else 0)) or (small and lvl > 2.6 and x % 3 == 1):
                    line.append(":")
                else:
                    line.append("." if (x * 5 + r * 3) % 7 == 0 else " ")
        rows.append("".join(line))
    return ["    *          .                 *      .    ",
            "        .               *                  "] + rows + [
            "~" * 44,
            "      m a s i s               s i s        "]


ART_CLS = {"#": "w", ":": "l", "/": "g", "\\": "g", "^": "y", "_": "g", ".": "d", "*": "y", "~": "c"}


def art_segs(line: str) -> list[tuple[str, str]]:
    segs: list[tuple[str, str]] = []
    for ch in line:
        c = ART_CLS.get(ch, "d")
        if segs and segs[-1][1] == c:
            segs[-1] = (segs[-1][0] + ch, c)
        else:
            segs.append((ch, c))
    return segs


def panel_neofetch(ctx) -> str:
    g, now, w = ctx["gh"], ctx["now"], ctx["weather"]
    created = parse_iso(g["created_at"]).astimezone(TZ)
    delta = now - created
    years = delta.days // 365
    months = (delta.days % 365) // 30
    cur, best = ctx["streak"]
    tot = sum(g["languages"].values()) or 1
    top = sorted(g["languages"].items(), key=lambda kv: -kv[1])[:4]
    langs = "  ".join(f"{LANG_SHORT.get(k, k.lower())} {v / tot * 100:.0f}%" for k, v in top)
    stars = sum(r["stars"] for r in g["repos"])
    forks = sum(r["forks"] for r in g["repos"])
    wx = f"{w['temp']:.0f}°C {w['desc']}, wind {w['wind']:.0f} km/h, hum {w['hum']}%" if w else "n/a (open-meteo timeout)"

    info = [
        [("hayk", "g b"), ("@", "d"), ("porto", "c b")],
        [("─" * 22, "d")],
        [("OS", "g b"), (': HaykOS 26.11 "Ararat" x86_64', "")],
        [("Host", "g b"), (": Porto, Portugal (EU work authorised)", "")],
        [("Kernel", "g b"), (": python-3.13-backend-5y", "")],
        [("Uptime", "g b"), (f": {years} years, {months} months (on github)", "")],
        [("Packages", "g b"), (f": {g['repo_count']} (git), {human_kb(g['disk_kb'])}B", "")],
        [("Shell", "g b"), (": zsh + ipython + psql", "")],
        [("Stack", "g b"), (": FastAPI · Django · SQLAlchemy2 · Celery", "")],
        [("Infra", "g b"), (": k8s · docker · nginx · gcp · azure", "")],
        [("Commits", "g b"), (f": {g['commits_year']} (365d)  streak {cur}d / best {best}d", "")],
        [("Social", "g b"), (f": {g['followers']} followers · {stars}★ · {forks} forks", "")],
        [("Langs", "g b"), (f": {langs}", "")],
        [("Weather", "g b"), (f": {wx}", "")],
        [("Locale", "g b"), (": en_GB · hy_AM · ru_RU · pt_PT (A2)", "")],
        [("Status", "g b"), (": ", ""), ("● ", "g pulse"), ("open to backend roles", "l")],
    ]
    art = ararat_rows()
    h = 52 + max(len(info), len(art)) * LH + 58
    body = []
    tl, end = typed(PAD, 30, PROMPT, "neofetch --ascii_distro ararat", begin=0.2, cps=0.04, cursor=False)
    body.append(tl)
    t0 = end + 0.25
    art_w = 44 * CW
    for i, line in enumerate(art):
        body.append(text(PAD, 62 + i * LH, art_segs(line), "glow" if 1 < i < 13 else "", delay=t0 + i * 0.05,
                         extra=f' textLength="{art_w:.0f}" lengthAdjust="spacingAndGlyphs"'))
    # greeting in the four languages (no alignment needed)
    body.append(text(PAD + art_w / 2, 62 + len(art) * LH, [("Բարև", "m"), (" · ", "d"), ("Привет", "m"), (" · ", "d"), ("Olá", "m"), (" · ", "d"), ("Hello", "m")],
                     anchor="middle", delay=t0 + 0.8))
    ix = PAD + art_w + 30
    for i, segs in enumerate(info):
        body.append(text(ix, 62 + i * LH, segs, delay=t0 + 0.1 + i * 0.06))
    sy = 62 + len(info) * LH - 6
    for i, col in enumerate([BG2, RED, GREEN, AMBER, BLUE, MAGENTA, CYAN, WHITE]):
        body.append(f'<rect x="{ix + i * 26}" y="{sy}" width="24" height="14" fill="{col}" class="a" style="animation-delay:{t0 + 1.2 + i * 0.05:.2f}s"/>')
    return doc(h, "".join(body) + tmux_bar(h, 1, now), title="neofetch")


def panel_shoplux(ctx) -> str:
    """Architecture of shoplux.casa as an animated terminal diagram."""
    now = ctx["now"]
    h = 520
    body = []
    tl, end = typed(PAD, 30, PROMPT, "shoplux --explain architecture --live", begin=0.2, cps=0.035, cursor=False)
    body.append(tl)
    t0 = end + 0.2
    step = [t0]

    def nxt(dt_=0.07):
        step[0] += dt_
        return step[0]

    def box(x, y, w, hh, title, subs=(), col=GREEN, fill=BG2, tcls="l b"):
        d = nxt()
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{hh}" rx="4" fill="{fill}" stroke="{col}" stroke-opacity=".75"/>',
               text(x + 10, y + 18, [(title, tcls)], "sm")]
        for i, sub in enumerate(subs):
            out.append(text(x + 10, y + 34 + i * 14, sub if isinstance(sub, list) else [(sub, "")], "xs"))
        body.append(f'<g class="a" style="animation-delay:{d:.2f}s">{"".join(out)}</g>')

    def wire(pts, col=GREEN, dash=False, packet=True, dur=2.2, delay=0.0, label=None, lpos=None):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        da = ' stroke-dasharray="4 4"' if dash else ""
        (ex, ey), (px, py) = pts[-1], pts[-2]
        ang = math.degrees(math.atan2(ey - py, ex - px))
        head = f'<path d="M0,0 L-7,-4 L-7,4 Z" fill="{col}" transform="translate({ex},{ey}) rotate({ang:.0f})"/>'
        g = f'<path d="{d}" fill="none" stroke="{col}" stroke-opacity=".55" stroke-width="1.3"{da}/>{head}'
        if label:
            g += text(lpos[0], lpos[1], [(label, "d")], "xs")
        start = t0 + 1.6 + delay
        if packet:
            g += (f'<circle r="3" fill="{col}" filter="url(#bigglow)" opacity="0"><set attributeName="opacity" to="1" begin="{start:.2f}s"/>'
                  f'<animateMotion dur="{dur}s" begin="{start:.2f}s" repeatCount="indefinite" path="{d}"/></circle>')
        body.append(f'<g class="a" style="animation-delay:{t0 + 1.2:.2f}s">{g}</g>')

    # edge
    box(22, 50, 150, 44, "browser", ["jinja2 · gettext i18n"], CYAN)
    box(206, 50, 150, 44, "cloudflare", ["tls · cdn · waf"], CYAN)
    box(390, 50, 140, 44, "nginx", ["reverse proxy"], CYAN)
    box(600, 50, 258, 44, "stripe", ["checkout · refunds · signed webhooks"], MAGENTA, tcls="m b")

    # app container
    body.append(f'<g class="a" style="animation-delay:{nxt():.2f}s"><rect x="22" y="118" width="540" height="252" rx="6" fill="none" '
                f'stroke="{GREEN}" stroke-dasharray="2 3" stroke-opacity=".6"/>'
                f'{text(32, 134, [("fastapi app ", "g b"), ("(async · docker compose)", "d")], "xs")}</g>')
    layers = [
        ("routers", "jwt cookies · google sso · pagination (6+ filters)"),
        ("facades", "one entry point per use case · reused as llm tools"),
        ("services + permissions", "business rules · strategy pattern for feeds"),
        ("repositories", "generic repo · async sqlalchemy 2 · no N+1"),
    ]
    for i, (t, sub) in enumerate(layers):
        y = 146 + i * 54
        box(36, y, 318, 40, t, [sub])
        if i:
            wire([(195, y - 14), (195, y)], packet=False)
    box(372, 146, 176, 94, "ai assistant", [[("pydantic-ai agent", "c")], "tools -> facades", "structured output", "usage limits · credits"], AMBER, tcls="y b")
    box(372, 254, 176, 88, "auth", ["fastapi-users", "jwt cookies + google sso", "email verify · reset", "onboarding profile"], CYAN, tcls="c b")

    # data / async side
    box(600, 118, 258, 50, "postgresql · asyncpg", [[("atomic stock reservation · tx", "")]], GREEN)
    box(600, 180, 258, 50, "redis", ["cache-aside + warm-up · chat memory"], RED, tcls="r b")
    box(600, 242, 258, 50, "rabbitmq", ["-> llm validator agents (moderation)"], AMBER, tcls="y b")
    box(600, 304, 258, 66, "celery", ["webhook reconciliation (missed events)", "newsletter digests"], BLUE, tcls="bl b")

    # bottom row
    box(22, 392, 250, 58, "llm backends", [[("ollama", "c"), (" (local dev)  ", "d")], [("gemini", "y"), (" (production)", "d")]], AMBER, tcls="y b")
    box(290, 392, 272, 58, "observability", ["logfire (llm traces)", "prometheus · grafana · loki"], GREEN)
    box(600, 392, 258, 58, "tests", ["pytest + real postgres", "concurrent checkout races"], MAGENTA, tcls="m b")

    # wires (request path + async paths)
    wire([(172, 72), (206, 72)], CYAN, dur=1.0)
    wire([(356, 72), (390, 72)], CYAN, dur=1.0, delay=0.3)
    wire([(460, 94), (460, 110), (195, 110), (195, 146)], CYAN, dur=1.6, delay=0.6)
    wire([(660, 94), (660, 106), (580, 106), (580, 140), (562, 140)], MAGENTA, dur=1.8, delay=0.2)
    wire([(280, 348), (280, 356), (585, 356), (585, 143), (600, 143)], GREEN, dur=2.0, delay=0.9)
    wire([(320, 348), (320, 363), (591, 363), (591, 205), (600, 205)], RED, dur=2.0, delay=1.3)
    wire([(562, 300), (575, 300), (575, 267), (600, 267)], AMBER, dash=True, dur=1.4, delay=0.4)
    wire([(562, 330), (578, 330), (578, 337), (600, 337)], BLUE, dash=True, dur=1.2, delay=0.8)
    wire([(372, 216), (354, 216)], AMBER, dur=0.8, delay=0.2)
    wire([(440, 240), (440, 248), (363, 248), (363, 380), (147, 380), (147, 392)], AMBER, dash=True, dur=2.0, delay=0.5)

    legend = [("── ", "c"), ("request  ", "d"), ("── ", "m"), ("stripe webhook  ", "d"), ("┄┄ ", "y"), ("async / queue  ", "d"),
              ("● ", "g"), ("packets animate the paths", "d")]
    body.append(text(PAD, 472, legend, "xs", delay=t0 + 1.6))
    return doc(h, "".join(body) + tmux_bar(h, 2, now), title="shoplux architecture")


def site_days(rows: list[dict], now: dt.datetime) -> list[float | None]:
    """Per-day uptime ratio for the last HISTORY_DAYS days (oldest -> newest)."""
    today = now.date()
    buckets: dict[int, list[int]] = {}
    for r in rows:
        d = dt.datetime.fromtimestamp(r["t"], TZ).date()
        idx = (today - d).days
        if 0 <= idx < HISTORY_DAYS:
            buckets.setdefault(idx, []).append(1 if 200 <= r["s"] < 400 else 0)
    return [(sum(buckets[i]) / len(buckets[i]) if i in buckets else None) for i in range(HISTORY_DAYS - 1, -1, -1)]


def panel_systemctl(ctx) -> str:
    now, hist = ctx["now"], ctx["history"]
    block = 5 * LH + 12
    h = 52 + len(SITES) * block + 30 + 30
    body = []
    tl, end = typed(PAD, 30, PROMPT, "systemctl status 'prod-*' --no-pager", begin=0.2, cps=0.04, cursor=False)
    body.append(tl)
    t0 = end + 0.2
    y = 62
    for si, (name, url, desc) in enumerate(SITES):
        rows = hist.get(name, [])
        p = ctx["probes"][name]
        is_up = 200 <= p["s"] < 400
        ups = [1 if 200 <= r["s"] < 400 else 0 for r in rows]
        pct = (sum(ups) / len(ups) * 100) if ups else 0.0
        lat = [r["ms"] for r in rows[-24:] if 200 <= r["s"] < 400]
        p50 = sorted(lat)[len(lat) // 2] if lat else 0
        # 'since' = start of current run of identical state
        since_t = rows[-1]["t"] if rows else int(now.timestamp())
        for r in reversed(rows):
            if (200 <= r["s"] < 400) != is_up:
                break
            since_t = r["t"]
        since = dt.datetime.fromtimestamp(since_t, TZ)
        d = t0 + si * 0.35
        dot = "g pulse" if is_up else "r pulse"
        body.append(text(PAD, y, [("● ", dot), (f"{name}.service", "l b"), (f" - {desc}", "")], delay=d))
        body.append(text(PAD, y + LH, [("     Loaded: ", "d"), ("loaded ", ""), (f"(/etc/systemd/system/{name}.service; ", "d"), ("enabled", "g"), (")", "d")], "sm", delay=d + 0.05))
        if is_up:
            act = [("     Active: ", "d"), ("active (running)", "g b"), (f" since {since:%a %Y-%m-%d %H:%M} {since.tzname()}; {ago(since, now)}", "")]
        else:
            code = f"HTTP {p['s']}" if p["s"] else "no response"
            act = [("     Active: ", "d"), ("failed", "r b"), (f" (Result: {code}) since {since:%a %Y-%m-%d %H:%M}; {ago(since, now)}", "y")]
        body.append(text(PAD, y + 2 * LH, act, "sm", delay=d + 0.1))
        body.append(text(PAD, y + 3 * LH, [("     Probe : ", "d"), (url, "c"), (f"  ->  {p['s'] or '---'}  {p['ms']}ms", "w" if is_up else "r"),
                                           (f"   p50(24h) {p50}ms", "d")], "sm", delay=d + 0.15))
        # 30-day uptime strip
        days = site_days(rows, now)
        bx, by = PAD + 100, y + 4 * LH - 11
        body.append(text(PAD, y + 4 * LH, [("     30d   ", "d")], "sm", delay=d + 0.2))
        bars = []
        for i, v in enumerate(days):
            col = DIM if v is None else GREEN if v >= 0.995 else AMBER if v >= 0.95 else RED
            op = ".35" if v is None else "1"
            bars.append(f'<rect x="{bx + i * 17:.1f}" y="{by}" width="13" height="14" rx="2" fill="{col}" fill-opacity="{op}"/>')
        body.append(f'<g class="a" style="animation-delay:{d + 0.22:.2f}s">{"".join(bars)}</g>')
        pc = "g b" if pct >= 99.5 else "y b" if pct >= 95 else "r b"
        body.append(text(bx + HISTORY_DAYS * 17 + 10, y + 4 * LH, [(f"{pct:6.2f}%", pc), (" uptime", "d")], "sm", delay=d + 0.25))
        y += block
    nxt = now + dt.timedelta(hours=1)
    body.append(text(PAD, y + 4, [("-- ", "d"), ("probed from a GitHub Actions runner at ", ""), (f"{now:%Y-%m-%d %H:%M} {now.tzname()}", "l"),
                                  (f" · next probe ~{nxt:%H:%M} · history {len(next(iter(hist.values()), []))} checks", ""), (" --", "d")],
                     "xs", delay=t0 + 1.2))
    return doc(h, "".join(body) + tmux_bar(h, 3, now), title="systemctl status of production sites")


def panel_htop(ctx) -> str:
    g, now = ctx["gh"], ctx["now"]
    cal = ctx["gh"]["calendar"]
    vals = [c for _, c in cal]
    load = (vals[-1] if vals else 0, sum(vals[-7:]) / 7, sum(vals[-30:]) / 30)
    created = parse_iso(g["created_at"]).astimezone(TZ)
    up = now - created
    hh, rem = divmod(up.seconds, 3600)
    mm, ss = divmod(rem, 60)
    running = sum(1 for r in g["repos"] if (now - parse_iso(r["pushed_at"])).days <= 7)
    tot = sum(g["languages"].values()) or 1
    top = sorted(g["languages"].items(), key=lambda kv: -kv[1])[:8]

    body = []
    tl, end = typed(PAD, 30, PROMPT, "htop --user=hayk --sort-key=PERCENT_CPU", begin=0.2, cps=0.035, cursor=False)
    body.append(tl)
    t0 = end + 0.15
    y0 = 58
    barx, barw = PAD + 58, 330
    # cpu "cores" = languages
    for i, (lang, size) in enumerate(top):
        y = y0 + i * 18
        pct = size / tot * 100
        wv = max(2.0, barw * pct / 100)
        hi = min(barw, wv * 1.08 + 2)
        lo = max(2.0, wv * 0.93)
        pat = "pipes" if i % 3 != 2 else "pipes-b" if i % 2 else "pipes-r"
        pat = "pipes" if pct > 8 else pat
        short = LANG_SHORT.get(lang, lang[:5].lower())
        body.append(f'<g class="a" style="animation-delay:{t0 + i * 0.05:.2f}s">'
                    f'{text(barx - 14, y, [(short, "c")], "sm", anchor="end")}'
                    f'{text(barx - 10, y, [("[", "w b")], "sm")}'
                    f'<rect x="{barx}" y="{y - 10}" width="{wv:.1f}" height="11" fill="url(#{pat})">'
                    f'<animate attributeName="width" values="{wv:.1f};{hi:.1f};{lo:.1f};{wv:.1f}" dur="{1.7 + i * 0.37:.2f}s" repeatCount="indefinite"/></rect>'
                    f'{text(barx + barw - 2, y, [(f"{pct:.1f}%", "d")], "sm", anchor="end")}'
                    f'{text(barx + barw + 2, y, [("]", "w b")], "sm")}</g>')
    # Mem / Swp
    disk_gb = g["disk_kb"] / 1024 / 1024
    cap = 2 ** math.ceil(math.log2(max(disk_gb, 0.5)) + 0.0001)
    my = y0 + len(top) * 18
    mem_w = barw * disk_gb / cap
    seg = [(0.62, "pipes"), (0.23, "pipes-b"), (0.15, "pipes-y")]
    xs = barx
    rects = ""
    for frac, pat in seg:
        rects += f'<rect x="{xs:.1f}" y="{my - 10}" width="{mem_w * frac:.1f}" height="11" fill="url(#{pat})"/>'
        xs += mem_w * frac
    body.append(f'<g class="a" style="animation-delay:{t0 + 0.45:.2f}s">'
                f'{text(barx - 14, my, [("Mem", "c")], "sm", anchor="end")}{text(barx - 10, my, [("[", "w b")], "sm")}{rects}'
                f'{text(barx + barw - 2, my, [(f"{disk_gb:.2f}G/{cap:.2f}G", "d")], "sm", anchor="end")}{text(barx + barw + 2, my, [("]", "w b")], "sm")}</g>')
    stars = sum(r["stars"] for r in g["repos"])
    forks = sum(r["forks"] for r in g["repos"])
    sw = barw * min(1, (stars + forks) / 50) + 2
    body.append(f'<g class="a" style="animation-delay:{t0 + 0.5:.2f}s">'
                f'{text(barx - 14, my + 18, [("Swp", "c")], "sm", anchor="end")}{text(barx - 10, my + 18, [("[", "w b")], "sm")}'
                f'<rect x="{barx}" y="{my + 8}" width="{sw:.1f}" height="11" fill="url(#pipes-r)"/>'
                f'{text(barx + barw - 2, my + 18, [(f"{stars}★ {forks}fk /50", "d")], "sm", anchor="end")}{text(barx + barw + 2, my + 18, [("]", "w b")], "sm")}</g>')

    rx = PAD + 470
    right = [
        [("Tasks: ", "c"), (str(g["repo_count"]), "l b"), (", ", ""), (str(g["commits_year"]), "l b"), (" thr; ", ""), (str(running), "g b"), (" running", "")],
        [("Load average: ", "c"), (f"{load[0]:.2f} ", "l b"), (f"{load[1]:.2f} ", "l"), (f"{load[2]:.2f}", "")],
        [("Uptime: ", "c"), (f"{up.days} days, {hh:02d}:{mm:02d}:{ss:02d}", "l b")],
        [],
        [("PRs opened: ", "c"), (str(g["prs"]), "l b"), ("   Issues: ", "c"), (str(g["issues"]), "l b")],
        [("Contributed to: ", "c"), (f"{g['contributed_to']} repos", "l b")],
        [("Followers: ", "c"), (str(g["followers"]), "l b"), ("   Following: ", "c"), (str(g["following"]), "l b")],
        [("Timezone: ", "c"), (f"Europe/Lisbon ({now:%Z} {now:%z})", "")],
        [("Hostname: ", "c"), ("porto.pt.eu-west", "")],
    ]
    for i, segs in enumerate(right):
        if segs:
            body.append(text(rx, y0 + i * 18, segs, "sm", delay=t0 + 0.1 + i * 0.05))

    # process table
    ty = my + 50
    cols = [("PID", 6, "end"), ("USER", 7.5, "start"), ("PRI", 18, "end"), ("NI", 21.5, "end"), ("VIRT", 28.5, "end"),
            ("S", 30.5, "start"), ("CPU%", 37, "end"), ("MEM%", 42.5, "end"), ("TIME+", 50.5, "end"), ("Command", 52, "start")]
    body.append(f'<rect x="0" y="{ty - 14}" width="{W}" height="19" fill="{GREEN}" class="a" style="animation-delay:{t0 + 0.6:.2f}s"/>')
    for name, cx, anc in cols:
        body.append(text(PAD + cx * CW, ty, [(name, "k b")], "sm", anchor=anc, delay=t0 + 0.6))

    commits_total = sum(r["commits"] for r in g["repos"]) or 1
    disk_total = sum(r["disk_kb"] for r in g["repos"]) or 1
    public = [r for r in g["repos"] if not r["private"] and not r["fork"]]
    private = [r for r in g["repos"] if r["private"]]
    public.sort(key=lambda r: (-r["commits"], -parse_iso(r["pushed_at"]).timestamp()))
    procs = []
    for r in public[:9]:
        age = (now - parse_iso(r["pushed_at"])).days
        state = "Z" if r["archived"] else "R" if age <= 7 else "S" if age <= 90 else "I"
        life = (now - parse_iso(r["created_at"])).days
        procs.append({
            "pid": int(hashlib.md5(r["name"].encode()).hexdigest(), 16) % 89000 + 1000, "user": "hayk",
            "pri": 20, "ni": -5 if r["name"] in {s[0] for s in SITES} or r["name"] == "culture_aggregator" else 0,
            "virt": human_kb(r["disk_kb"]), "s": state, "cpu": r["commits"] / commits_total * 100,
            "mem": r["disk_kb"] / disk_total * 100, "time": f"{life // 365}y{life % 365:03d}d",
            "cmd": lang_cmd(r["lang"], r["name"]),
        })
    if private:
        pc = sum(r["commits"] for r in private)
        pk = sum(r["disk_kb"] for r in private)
        procs.append({"pid": 2, "user": "root", "pri": 0, "ni": -20, "virt": human_kb(pk), "s": "S",
                      "cpu": pc / commits_total * 100, "mem": pk / disk_total * 100, "time": "--",
                      "cmd": f"[kworker/u{len(private)}:private-repos]"})
    procs.sort(key=lambda p: -p["cpu"])
    for i, p in enumerate(procs[:10]):
        y = ty + 19 + i * 18
        d = t0 + 0.7 + i * 0.06
        sel = i == 0
        if sel:
            body.append(f'<rect x="0" y="{y - 13}" width="{W}" height="18" fill="{CYAN}" class="a" style="animation-delay:{d:.2f}s"/>')
        sc = {"R": "g b", "S": "", "I": "d", "Z": "r b"}[p["s"]]
        cmdc = "k b" if sel else ("d" if p["cmd"].startswith("[") else "l" if p["s"] == "R" else "")
        vals = [str(p["pid"]), p["user"], str(p["pri"]), str(p["ni"]), p["virt"], p["s"], f"{p['cpu']:.1f}", f"{p['mem']:.1f}", p["time"], p["cmd"]]
        cls = ["", "c" if p["user"] == "hayk" else "r", "d", "r" if p["ni"] < 0 else "d", "", sc, "w" if p["cpu"] > 10 else "", "", "d", cmdc]
        for (cn, cx, anc), v, c in zip(cols, vals, cls):
            body.append(text(PAD + cx * CW, y, [(v, "k" if sel and cn != "Command" else c)], "sm", anchor=anc, delay=d))
    # function key bar
    fy = ty + 19 + 10 * 18 + 6
    keys = ["Help", "Setup", "Search", "Filter", "Tree", "SortBy", "Nice -", "Nice +", "Kill", "Quit"]
    fx = 0.0
    fk = []
    for i, k in enumerate(keys):
        n = f"F{i + 1}"
        fk.append(text(fx + 4, fy, [(n, "w b")], "sm"))
        fk.append(f'<rect x="{fx + 4 + len(n) * 7.2 + 2:.1f}" y="{fy - 12}" width="{len(k) * 7.0 + 8:.1f}" height="16" fill="{CYAN}"/>')
        fk.append(text(fx + 4 + len(n) * 7.2 + 6, fy, [(k, "k")], "sm"))
        fx += len(n) * 7.2 + len(k) * 7.0 + 20
    body.append(f'<g class="a" style="animation-delay:{t0 + 1.4:.2f}s">{"".join(fk)}</g>')
    h = int(fy + 14 + 26)
    return doc(h, "".join(body) + tmux_bar(h, 4, now), title="htop of github activity")


def panel_chrono(ctx) -> str:
    now = ctx["now"]
    times = [parse_iso(t) for t in ctx["commit_times"]]
    hours = Counter(t.hour for t in times)
    wdays = Counter(t.weekday() for t in times)
    n = len(times)
    h = 360
    body = []
    tl, end = typed(PAD, 30, PROMPT, "git log --author=hayk --date=format:%H | chronotype --hist", begin=0.2, cps=0.03, cursor=False)
    body.append(tl)
    t0 = end + 0.2
    if n < 20:
        body.append(text(PAD, 80, [("chronotype: not enough public commits to profile yet (", "y"), (str(n), "l"), (")", "y")]))
        return doc(160, "".join(body) + tmux_bar(160, 5, now), title="commit chronotype")

    cx0, cy0, ch = PAD + 34, 84, 140
    mx = max(hours.values())
    body.append(text(PAD, 60, [("commits by hour of day", "l b"), (f"   (local time of each commit, n={n})", "d")], "sm", delay=t0))
    for k in range(0, 5):
        yy = cy0 + ch - ch * k / 4
        body.append(f'<line x1="{cx0 - 4}" y1="{yy:.1f}" x2="{cx0 + 24 * 22}" y2="{yy:.1f}" stroke="{DIM}" stroke-opacity=".5" stroke-dasharray="2 4"/>')
        body.append(text(cx0 - 8, yy + 4, [(f"{mx * k / 4:.0f}", "d")], "xs", anchor="end"))
    peak = max(range(24), key=lambda i: hours.get(i, 0))
    for i in range(24):
        v = hours.get(i, 0)
        bh = max(1.5, ch * v / mx)
        col = MAGENTA if i < 6 else AMBER if i < 12 else GREEN if i < 18 else CYAN
        x = cx0 + i * 22
        d = t0 + 0.1 + i * 0.03
        body.append(f'<rect x="{x}" y="{cy0 + ch - bh:.1f}" width="16" height="{bh:.1f}" fill="{col}" fill-opacity="{1 if i == peak else .8}" class="a" style="animation-delay:{d:.2f}s">'
                    f'<animate attributeName="height" from="0" to="{bh:.1f}" begin="{d:.2f}s" dur=".5s" fill="freeze"/>'
                    f'<animate attributeName="y" from="{cy0 + ch}" to="{cy0 + ch - bh:.1f}" begin="{d:.2f}s" dur=".5s" fill="freeze"/></rect>')
        if i % 3 == 0:
            body.append(text(x + 8, cy0 + ch + 16, [(f"{i:02d}", "d")], "xs", anchor="middle"))
    body.append(text(cx0 + peak * 22 + 8, cy0 - 5, [("peak", "w")], "xs", anchor="middle", delay=t0 + 1))
    # legend
    lg = [("■ ", "m"), ("00-06 ", "d"), ("■ ", "y"), ("06-12 ", "d"), ("■ ", "g"), ("12-18 ", "d"), ("■ ", "c"), ("18-24", "d")]
    body.append(text(cx0, cy0 + ch + 34, lg, "xs", delay=t0 + 0.9))

    # weekday bars
    wx0 = PAD + 620
    body.append(text(wx0, 60, [("by weekday", "l b")], "sm", delay=t0))
    wmx = max(wdays.values())
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i in range(7):
        v = wdays.get(i, 0)
        y = cy0 + 12 + i * 21
        bw = 150 * v / wmx
        col = AMBER if i >= 5 else GREEN
        body.append(f'<g class="a" style="animation-delay:{t0 + 0.2 + i * 0.05:.2f}s">'
                    f'{text(wx0, y + 10, [(names[i], "c")], "sm")}'
                    f'<rect x="{wx0 + 38}" y="{y}" width="{bw:.1f}" height="12" fill="url(#{"pipes-y" if i >= 5 else "pipes"})"/>'
                    f'{text(wx0 + 38 + bw + 6, y + 10, [(f"{v / n * 100:.0f}%", "d")], "xs")}</g>')

    night = sum(hours.get(i, 0) for i in (22, 23, 0, 1, 2, 3, 4)) / n
    morning = sum(hours.get(i, 0) for i in range(5, 11)) / n
    office = sum(hours.get(i, 0) for i in range(9, 18)) / n
    evening = sum(hours.get(i, 0) for i in range(18, 22)) / n
    weekend = (wdays.get(5, 0) + wdays.get(6, 0)) / n
    kind, why = max([
        ("NIGHT OWL", night, "22:00-05:00"), ("EARLY BIRD", morning * 1.3, "05:00-11:00"),
        ("9-TO-5 HACKER", office * 0.75, "09:00-18:00"), ("EVENING SHIPPER", evening * 1.4, "18:00-22:00"),
    ], key=lambda k: k[1])[0::2]
    share = {"NIGHT OWL": night, "EARLY BIRD": morning, "9-TO-5 HACKER": office, "EVENING SHIPPER": evening}[kind]
    busiest = names[max(range(7), key=lambda i: wdays.get(i, 0))]
    total_commits = ctx["gh"]["commits_year"]
    yy = cy0 + ch + 66
    diag = [
        [(">> chronotype : ", "d"), (kind, "y b glow"), (f"   ({share * 100:.0f}% of commits land between {why})", "")],
        [(">> peak hour  : ", "d"), (f"{peak:02d}:00", "l b"), ("   busiest day: ", "d"), (busiest, "l b"), ("   weekend commits: ", "d"),
         (f"{weekend * 100:.0f}%", "l b")],
        [(">> coffee.svc : ", "d"), (f"~{total_commits // 7} cups", "l b"), ("  (est. 1 cup / 7 commits, peer review pending)", "d")],
    ]
    for i, segs in enumerate(diag):
        body.append(text(PAD, yy + i * LH, segs, "sm", delay=t0 + 1.1 + i * 0.15))
    h = int(yy + 2 * LH + 40)
    return doc(h, "".join(body) + tmux_bar(h, 5, now), title="commit chronotype")


def panel_pulse(ctx) -> str:
    g, now = ctx["gh"], ctx["now"]
    cal = g["calendar"][-365:]
    vals = [c for _, c in cal]
    h = 300
    body = []
    tl, end = typed(PAD, 30, PROMPT, "cat /dev/commits | oscilloscope --window=365d --ch1", begin=0.2, cps=0.03, cursor=False)
    body.append(tl)
    t0 = end + 0.2
    gx, gy, gw, gh = PAD, 50, W - 2 * PAD, 170
    grid = [f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" fill="{BG2}" stroke="{DIM}"/>']
    for i in range(1, 20):
        x = gx + gw * i / 20
        grid.append(f'<line x1="{x:.1f}" y1="{gy}" x2="{x:.1f}" y2="{gy + gh}" stroke="{DIM}" stroke-opacity="{.7 if i % 5 == 0 else .3}"/>')
    for i in range(1, 8):
        y = gy + gh * i / 8
        grid.append(f'<line x1="{gx}" y1="{y:.1f}" x2="{gx + gw}" y2="{y:.1f}" stroke="{DIM}" stroke-opacity="{.7 if i == 4 else .3}"/>')
    body.append("".join(grid))
    mx = max(vals) or 1
    n = len(vals)
    xof = lambda i: gx + 4 + (gw - 8) * i / max(1, n - 1)
    yof = lambda v: gy + gh - 8 - (gh - 30) * math.sqrt(max(v, 0) / mx)
    bars = "".join(f'<line x1="{xof(i):.1f}" y1="{gy + gh - 8}" x2="{xof(i):.1f}" y2="{yof(v):.1f}"/>' for i, v in enumerate(vals) if v)
    body.append(f'<g stroke="{GREEN}" stroke-opacity=".3" stroke-width="1.4" class="a" style="animation-delay:{t0:.2f}s">{bars}</g>')
    ma = [sum(vals[max(0, i - 6):i + 1]) / len(vals[max(0, i - 6):i + 1]) for i in range(n)]
    pts = [(xof(i), yof(v)) for i, v in enumerate(ma)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    area = d + f" L{pts[-1][0]:.1f},{gy + gh - 8} L{pts[0][0]:.1f},{gy + gh - 8} Z"
    body.append(f'<path d="{area}" fill="{GREEN}" fill-opacity=".07" class="a" style="animation-delay:{t0 + 2.2:.2f}s"/>')
    body.append(f'<path d="{d}" fill="none" stroke="{GREEN}" stroke-width="2" stroke-linejoin="round" filter="url(#bigglow)" '
                f'stroke-dasharray="{length:.0f}" stroke-dashoffset="{length:.0f}">'
                f'<animate attributeName="stroke-dashoffset" from="{length:.0f}" to="0" begin="{t0:.2f}s" dur="2.4s" fill="freeze"/></path>')
    body.append(f'<circle r="3.5" fill="{WHITE}" filter="url(#bigglow)" opacity="0"><set attributeName="opacity" to="1" begin="{t0 + 2.4:.2f}s"/>'
                f'<animateMotion dur="9s" begin="{t0 + 2.4:.2f}s" repeatCount="indefinite" path="{d}"/></circle>')
    # month ticks
    for i, (ds, _) in enumerate(cal):
        if ds.endswith("-01"):
            x = pts[i][0]
            body.append(text(x, gy + gh + 14, [(dt.date.fromisoformat(ds).strftime("%b").lower(), "d")], "xs", anchor="middle"))
    # readouts
    cur, best = ctx["streak"]
    imax = max(range(n), key=lambda i: vals[i])
    avg = sum(vals) / n if n else 0
    body.append(text(gx + 8, gy + 16, [("CH1 ", "y b"), ("7d avg  ", ""), ("CH2 ", "d b"), ("daily  ", "d"), ("√-scale  ", "d"), ("365d", "c")], "xs", delay=t0))
    body.append(text(gx + gw - 8, gy + 16, [("Vmax ", "d"), (f"{mx}", "l b"), ("  Vavg ", "d"), (f"{avg:.2f}", "l b"), ("  Trig ", "d"), ("auto", "g")], "xs", anchor="end", delay=t0))
    sy = gy + gh + 40
    stats = [("total ", "d"), (f"{sum(vals)}", "l b"), ("   current streak ", "d"), (f"{cur}d", "g b"), ("   best streak ", "d"), (f"{best}d", "l b"),
             ("   spike ", "d"), (f"{vals[imax]} on {cal[imax][0]}", "y b"), ("   flatline days ", "d"), (f"{sum(1 for v in vals if v == 0)}", "r")]
    body.append(text(PAD, sy, stats, "sm", delay=t0 + 2.5))
    return doc(h, "".join(body) + tmux_bar(h, 6, now), title="contribution oscilloscope")


def panel_matrix(ctx) -> str:
    h = 170
    rnd = random.Random(42)
    glyphs = "01アイウエオカキクケコサシスセソタチツテトナニヌネノ$#{}<>=+*λ"
    cols = []
    for i in range(W // 16):
        x = 8 + i * 16
        n = rnd.randint(6, 14)
        chars = "".join(rnd.choice(glyphs) for _ in range(n))
        dur = rnd.uniform(3.5, 9)
        delay = -rnd.uniform(0, dur)
        sp = []
        for j, ch in enumerate(chars):
            op = 0.15 + 0.85 * (j / (n - 1)) if n > 1 else 1
            cls = "w" if j == n - 1 else "g"
            sp.append(f'<tspan x="{x}" dy="16" class="{cls}" fill-opacity="{op:.2f}">{esc(ch)}</tspan>')
        cols.append(f'<text class="rain sm" style="animation-duration:{dur:.2f}s;animation-delay:{delay:.2f}s" y="-{n * 16}">{"".join(sp)}</text>')
    css = f".rain{{animation-name:fall;animation-timing-function:linear;animation-iteration-count:infinite}}@keyframes fall{{from{{transform:translateY(0)}}to{{transform:translateY({h + 260}px)}}}}"
    msg_w = 520
    box = (f'<rect x="{(W - msg_w) / 2}" y="{h / 2 - 34}" width="{msg_w}" height="62" rx="6" fill="{BG}" stroke="{GREEN}" stroke-opacity=".6"/>'
           f'{text(W / 2, h / 2 - 6, [("while ", "m"), ("True", "y"), (": ", "w"), ("ship_something_new", "g b"), ("()", "w")], "lg glow", anchor="middle")}'
           f'{text(W / 2, h / 2 + 16, [("# talk is cheap. show me the code.  -- linus", "d")], "sm", anchor="middle")}')
    return doc(h, "".join(cols) + box, css, title="while True: ship_something_new()")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--demo", action="store_true", help="offline sample data, no network")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    hist_path = out / "data" / "uptime.json"
    now = dt.datetime.now(TZ)
    nowts = int(now.timestamp())

    if args.demo:
        gh, times, weather = demo_github(), demo_commit_times(), {"temp": 16, "hum": 77, "desc": "partly cloudy", "wind": 12}
        history = demo_history(nowts)
        probes = {n: history[n][-1] for n, _, _ in SITES}
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("GH_TOKEN is required (or pass --demo)", file=sys.stderr)
            return 2
        print("» github graphql")
        gh = fetch_github(token)
        print("» commit timestamps")
        times = fetch_commit_times(token)
        print("» weather")
        weather = fetch_weather()
        print("» probes")
        probes = {n: probe(u) for n, u, _ in SITES}
        try:
            history = json.loads(hist_path.read_text()) if hist_path.exists() else {}
        except json.JSONDecodeError:
            history = {}
        cutoff = nowts - HISTORY_DAYS * 86400
        for n, _, _ in SITES:
            history[n] = [r for r in history.get(n, []) if r["t"] >= cutoff] + [probes[n]]
        for n, p in probes.items():
            print(f"   {n:<18} {p['s']:>3}  {p['ms']}ms")

    hist_path.write_text(json.dumps(history, separators=(",", ":")))
    ctx = {"gh": gh, "commit_times": times, "weather": weather, "probes": probes, "history": history,
           "now": now, "streak": streaks(gh["calendar"])}
    panels = {
        "boot.svg": panel_boot, "neofetch.svg": panel_neofetch, "shoplux.svg": panel_shoplux, "systemctl.svg": panel_systemctl,
        "htop.svg": panel_htop, "chrono.svg": panel_chrono, "pulse.svg": panel_pulse, "matrix.svg": panel_matrix,
    }
    for fn, f in panels.items():
        (out / fn).write_text(f(ctx), encoding="utf-8")
        print(f"   wrote {fn}")
    (out / "data" / "snapshot.json").write_text(json.dumps({
        "generated_at": now.isoformat(), "repos": gh["repo_count"], "commits_year": gh["commits_year"],
        "streak": ctx["streak"], "probes": probes, "weather": weather}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
