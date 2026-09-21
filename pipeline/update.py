#!/usr/bin/env python3
"""
Shearer Day Sheet. One file: fetch, classify, build.

  WPCOM_STATS_KEY=...  python3 update.py run

Optional ANTHROPIC_API_KEY makes Claude label the headlines; without it a rules
classifier is used. State accumulates in state.json beside this file, and is
built from the last 60 days on the first run.
"""
import json, os, re, sys, html, math, statistics, collections
import datetime as dt, urllib.request, urllib.parse

KEY = os.environ.get("WPCOM_STATS_KEY", "")
BLOG_ID = "55140081"
AUTHOR_ID = 3430                # Michael Shearer
SETTLE = 3                      # views counted over publish day + 2
RESETTLE = 6                    # re-pull views for the most recent N days
WINDOW = 60                     # rolling days shown on the page
HERE = os.path.dirname(os.path.abspath(__file__)) or "."
P = lambda f: os.path.join(HERE, f)


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "celts-desk/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == tries - 1:
                raise
            import time; time.sleep(3)


def postviews(day):
    if not KEY:
        raise SystemExit("WPCOM_STATS_KEY not set")
    u = "https://stats.wordpress.com/csv.php?" + urllib.parse.urlencode(
        {"api_key": KEY, "blog_id": BLOG_ID, "table": "postviews",
         "days": 1, "end": day, "limit": 500, "format": "json"})
    d = get(u)
    return {p["post_id"]: p["views"] for p in (d[0].get("postviews", []) if d else [])}


def posts(after, before, author=None):
    base = f"https://public-api.wordpress.com/rest/v1.1/sites/{BLOG_ID}/posts/"
    q = {"number": 100, "after": after, "before": before,
         "fields": "ID,title,date,URL,author"}
    if author:
        q["author"] = author
    out, url = [], base + "?" + urllib.parse.urlencode(q)
    while url:
        d = get(url)
        got = d.get("posts", [])
        out.extend(got)
        nxt = d.get("meta", {}).get("next_page")
        url = base + "?" + urllib.parse.urlencode({**q, "page_handle": nxt}) \
            if (nxt and len(got) == 100) else None
    return out


def load():
    try:
        return json.load(open(P("state.json")))
    except FileNotFoundError:
        return {"rows": [], "site_by_day": {}}


# --------------------------------------------------------------- fetch
def fetch():
    st = load()
    rows = {r["id"]: r for r in st["rows"]}
    today = dt.date.today()
    last_day = today - dt.timedelta(days=1)      # yesterday
    last_settled = last_day
    have = sorted(st["site_by_day"].keys())
    first = dt.date.fromisoformat(have[-1]) + dt.timedelta(days=1) if have \
        else last_settled - dt.timedelta(days=WINDOW)
    # re-settle recent days too
    first = min(first, last_settled - dt.timedelta(days=RESETTLE))

    if first > last_settled:
        print("nothing new to fetch")
        open(P("unclassified.tsv"), "w").write("")
        return

    need = set()
    d = first
    while d <= last_settled:
        for i in range(SETTLE):
            day = d + dt.timedelta(days=i)
            if day <= last_day:
                need.add(day.isoformat())
        d += dt.timedelta(days=1)
    views = {}
    for day in sorted(need):
        views[day] = postviews(day)
    print(f"pulled postviews for {len(views)} days", file=sys.stderr)

    def settled(pid, pub):
        return sum(views.get((pub + dt.timedelta(days=i)).isoformat(), {}).get(pid, 0)
                   for i in range(SETTLE))

    d = first
    while d <= last_settled:
        a = f"{d.isoformat()}T00:00:00+01:00"
        b = f"{(d + dt.timedelta(days=1)).isoformat()}T00:00:00+01:00"
        everyone = posts(a, b)
        sv = [settled(p["ID"], d) for p in everyone]
        st["site_by_day"][d.isoformat()] = {
            "posts": len(everyone),
            "median": statistics.median(sv) if sv else 0,
        }
        for p in everyone:
            if p.get("author", {}).get("ID") != AUTHOR_ID:
                continue
            r = rows.get(p["ID"], {})
            rows[p["ID"]] = {
                "id": p["ID"], "day": d.isoformat(), "date": p["date"],
                "title": html.unescape(p["title"]), "url": p["URL"],
                "views": settled(p["ID"], d), "type": r.get("type", ""),
            }
        print(f"  {d} michael={sum(1 for x in rows.values() if x['day']==d.isoformat())}"
              f" site={len(everyone)}", file=sys.stderr)
        d += dt.timedelta(days=1)

    st["rows"] = sorted(rows.values(), key=lambda r: r["id"])
    json.dump(st, open(P("state.json"), "w"), indent=1)

    todo = [r for r in st["rows"] if not r["type"]]
    if todo:
        assign(todo)
        json.dump(st, open(P("state.json"), "w"), indent=1)
    print(f"state: {len(st['rows'])} rows | classified this run: {len(todo)}")


# ------------------------------------------------------- classification
def assign(rows):
    """Label rows in place. Uses Claude when an API key is present, rules otherwise."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        try:
            labels = claude_labels([r["title"] for r in rows], key)
            if len(labels) == len(rows):
                for r, t in zip(rows, labels):
                    r["type"] = t if t in LABEL else "other"
                print(f"  classified {len(rows)} headlines with Claude", file=sys.stderr)
                return
            print("  Claude returned wrong count, falling back to rules", file=sys.stderr)
        except Exception as ex:
            print(f"  Claude classify failed ({ex}), falling back to rules", file=sys.stderr)
    for r in rows:
        r["type"] = classify(r["title"])
    print(f"  classified {len(rows)} headlines with rules", file=sys.stderr)


def pick_model(key):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models?limit=100",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ids = [m["id"] for m in json.loads(r.read().decode())["data"]]
    for want in ("haiku", "sonnet"):
        hit = [i for i in ids if want in i]
        if hit:
            return sorted(hit)[-1]
    return ids[0]


def claude_labels(titles, key):
    types = ", ".join(LABEL)
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "These are headlines from CeltsAreHere.com, a Celtic FC news site.\n"
        "Classify each into exactly one story type, judging what the article IS "
        "rather than merely who is quoted in it.\n\n"
        f"Types: {types}\n\n"
        "manager_presser is routine Celtic manager press-conference talk (mood, tactics, "
        "respect for opponents). manager_newsline is the manager saying something with real "
        "news in it (a transfer plan, a contract, a named injury, a board conversation, an "
        "exit, or a genuinely combative line). pundit_reaction covers pundits and ex-players "
        "reacting (Sutton, Boyd, Lennon, Jordan, Stewart, English and the like). "
        "rival_voice is a rival club's manager or player talking about Celtic.\n\n"
        f"Headlines:\n{numbered}\n\n"
        "Reply with a JSON array of the type strings only, in order, same length as the "
        "list. No prose, no markdown fences.")
    body = json.dumps({"model": pick_model(key), "max_tokens": 4000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        txt = json.loads(r.read().decode())["content"][0]["text"].strip()
    txt = txt[txt.index("["): txt.rindex("]") + 1]
    return json.loads(txt)


# --------------------------------------------------------------- build
LABEL = {
    "transfer_in_motion": "Live transfer business", "transfer_link": "Transfer links",
    "transfer_confirmed": "Confirmed deals", "exit_news": "Exit stories",
    "injury_news": "Injury news", "officiating": "Refereeing and VAR",
    "board_backroom": "Board and backroom", "club_official": "Club announcements",
    "fan_reaction": "Fan reaction", "player_moment": "Spotted moments",
    "manager_presser": "Manager presser lines", "manager_newsline": "Manager news lines",
    "player_quotes": "Player quotes", "player_future": "Player on own future",
    "pundit_reaction": "Pundit reaction", "rival_voice": "Rival voices",
    "journalist_report": "Journalist claims", "preview": "Previews",
    "starting_xi": "Team news and XI", "match_report": "Match reports",
    "former_player": "Former players", "youth_women": "Youth, loans, women",
    "international": "Internationals", "kit_commercial": "Kit and commercial",
    "stats_data": "Stats and data", "other": "Other",
}
lab = lambda t: LABEL.get(t, (t or "other").replace("_", " ").title())
low = lambda t: " ".join(w if w in ("XI", "VAR") else w.lower() for w in lab(t).split())
e = lambda s: html.escape(str(s), quote=True)
n = lambda x: f"{int(round(x)):,}"
SPAN = 2.0
pos = lambda m: 50 + (max(-SPAN, min(SPAN, math.log2(m if m > 0 else .01))) / SPAN) * 50


def bar(m):
    p = pos(m); lo, hi = min(p, 50.0), max(p, 50.0)
    return f"left:{lo:.2f}%;width:{max(hi-lo,.6):.2f}%", ("over" if m >= 1 else "under")


def build():
    st = load()
    for r in st["rows"]:
        r["type"] = r["type"] or "other"

    alldays = sorted({r["day"] for r in st["rows"]})
    ROWS = [r for r in st["rows"] if r["day"] in set(alldays[-WINDOW:])]
    SITE = st["site_by_day"]
    med = statistics.median(r["views"] for r in ROWS)
    today = dt.date.today()

    # --- yesterday (or the most recent day he actually filed) ---
    filed = sorted({r["day"] for r in ROWS})
    yday = (today - dt.timedelta(days=1)).isoformat()
    day = yday if yday in filed else filed[-1]
    drows = sorted([r for r in ROWS if r["day"] == day], key=lambda r: -r["views"])
    dsite = SITE.get(day, {})
    dmed = statistics.median(r["views"] for r in drows) if drows else 0
    dmax = max([r["views"] for r in drows] or [1])
    stale = "" if day == yday else (
        '<span class="flag">His last filing day, not yesterday. '
        'He did not publish yesterday.</span>')
    young = (dt.date.today() - dt.date.fromisoformat(day)).days < 3
    note = ("Counted since publication, so these still climb for another day or two."
            if young else "Settled figures.")

    def drow(r, i):
        side = "over" if r["views"] >= med else "under"
        return (f'<tr><td class="rk mono">{i}</td>'
                f'<td class="ti"><a href="{e(r["url"])}" target="_blank" rel="noopener">'
                f'{e(r["title"])}</a><span class="chip">{e(lab(r["type"]))}</span></td>'
                f'<td class="bc"><span class="minibar {side}" '
                f'style="width:{max(r["views"]/dmax*100,2):.1f}%"></span></td>'
                f'<td class="mono num">{n(r["views"])}</td>'
                f'<td class="mono num {side}">{r["views"]/med:.1f}x</td></tr>')

    # --- recent days strip ---
    recent = []
    for d in reversed(filed[-15:-1] if len(filed) > 1 else []):
        rs = [r for r in ROWS if r["day"] == d]
        b = max(rs, key=lambda r: r["views"])
        m = statistics.median(r["views"] for r in rs)
        recent.append(
            f'<tr><td class="dt">{dt.date.fromisoformat(d).strftime("%a %-d %b")}</td>'
            f'<td class="mono num">{len(rs)}</td>'
            f'<td class="mono num {"over" if m>=med else "under"}">{n(m)}</td>'
            f'<td class="ti"><a href="{e(b["url"])}" target="_blank" rel="noopener">'
            f'{e(b["title"])}</a></td>'
            f'<td class="mono num">{n(b["views"])}</td></tr>')

    # --- type table ---
    by = collections.defaultdict(list)
    for r in ROWS:
        by[r["type"]].append(r)
    ts = sorted(({"t": t, "n": len(rs), "m": statistics.median(x["views"] for x in rs)}
                 for t, rs in by.items() if len(rs) >= 4), key=lambda x: -x["m"])
    trows = ""
    for x in ts:
        s_, side = bar(x["m"] / med)
        tip = e("%d articles, median %s views" % (x["n"], n(x["m"])))
        trows += (f'<div class="row"><div class="rlab">{e(lab(x["t"]))}</div>'
                  f'<div class="track" data-tip="{tip}">'
                  f'<span class="tick t1"></span>'
                  f'<i class="bar {side}" style="{s_}"></i></div>'
                  f'<div class="rnum mono {side}">{x["m"]/med:.1f}x</div>'
                  f'<div class="rmed mono">{n(x["m"])}</div></div>')
    ticks = "".join(f'<span class="tk" style="left:{pos(v):.2f}%">{l}</span>'
                    for v, l in [(.25, "0.25x"), (.5, "0.5x"), (1, "median"), (2, "2x"), (4, "4x")])

    best = drows[0] if drows else None
    tpl = TEMPLATE
    out = tpl.format(
        daylong=dt.date.fromisoformat(day).strftime("%A %-d %B"),
        stale=stale, note=note,
        nfiled=len(drows),
        best_views=n(best["views"]) if best else "0",
        best_type=lab(best["type"]) if best else "",
        daymed=n(dmed), daymed_side="over" if dmed >= med else "under",
        med=n(med), nwindow=len(ROWS), ndays=len(filed),
        sitemed=n(dsite.get("median", 0)), siteposts=dsite.get("posts", "?"),
        dayrows="".join(drow(r, i) for i, r in enumerate(drows, 1)),
        recentrows="".join(recent), ticks=ticks, typerows=trows,
        generated=dt.datetime.now().strftime("%-d %B %Y, %H:%M"))
    open(P("desk.html"), "w").write(out)
    print(f"built desk.html for {day}: {len(drows)} articles, running median {n(med)}")



# ------------------------------------------------- rules fallback
PUNDITS = r"(chris sutton|sutton|kris boyd|boyd|simon jordan|neil lennon|lennon|michael stewart|tom english|charlie nicholas|john hartson|hartson|ally mccoist|mccoist|barry ferguson|alan brazil|frank mcavennie|mcavennie|kenny miller|peter martin|pat nevin|scott brown|stiliyan petrov|andy walker|steven thompson|josh meekings|kevin thomson|stephen craigan|willie miller)"
MANAGERS = r"(martin o.neill|o.neill|brendan rodgers|rodgers)"
JOURNOS = r"(journalist|journo|reporter|david friel|stephen mcgowan|mcgowan|anthony haggerty|ronnie charters|keith jackson|gary keown|alison mcconnell)"
SAY = r"(says?|said|claims?|reveals?|admits?|insists?|explains?|confirms?|believes?|tells?|urges?|warns?|hits out|slams?|blasts?|brands?|rejects?|rages?|responds?|addresses|delivers?|makes? clear|opens up|speaks?|questions?|calls? for|takes? aim|defends?|backs?|praises?|challenges?|dismisses?|shuts? down|fires?|flags?|drops? hint|signals?|predicts?|revisits?|takes? blame)"

RULES = [
    # most specific first
    ("starting_xi",        r"starting (xi|11)|predicted (xi|11|line.?up)|line.?up|team news|team named|xi vs|xi to face"),
    ("kit_commercial",     r"\bkit\b|new strip|shirt (leak|launch|deal)|adidas|sponsor|merchandis|retro top"),
    ("youth_women",        r"\bb team\b|academy|celtic women|youth|loan (spell|move|return)|development squad"),
    ("international",      r"international (duty|break|call)|scotland squad|called up (by|for)|japan squad|republic of ireland squad|national team"),
    ("officiating",        r"\bvar\b|referee|\bref\b|whistle|officials?|willie collum|disallow|penalty (decision|claim)|red card decision|sfa (panel|compliance)"),
    ("injury_news",        r"injur|fitness|scan|sidelined|protective boot|substituted|knock|out for|return date|ruled out|doubt for|limped|recovery|absence|setback|miss(es|ed)? training|miss(es|ed)? (the )?(match|game)|\bblow\b"),
    ("transfer_confirmed", r"^confirmed:|\bcompletes? (a )?(move|switch|transfer)\b|\bsigns? for\b|joins? celtic|joins? .*(on|for) (a )?(season|loan|deal|free)|officially (signs|joins|completes)|unveiled|deal done|seals? (a )?(loan )?(move|switch|transfer|deal)|summer signing\b"),
    ("transfer_in_motion", r"\bbid\b|\bbids\b|\boffer\b|talks?\b|\bagree|medical|transfer fee|hijack|move (collapses|dead|off|unlikely|nearing)|price|deadline day|swoop|approach|timeline|collapses|\bdead\b|progress|weigh up|hold talks|nearing|complete"),
    ("transfer_link",      r"linked|\btargets?\b|interest|eyeing|\beyes?\b|race for|out of .*race|wanted by|radar|shortlist|offered to|scouted|tracking|alerted to|transfer boost|transfer update|\bmoves?\b|\btransfers?\b|\bsigning\b|wants? to sign"),
    ("exit_news",          r"\bexit\b|leaves?\b|leaving|farewell|departure|axed|sacked|released|future (in doubt|key)|set to go|walks away|final (game|appearance)"),
    ("board_backroom",     r"\bboard\b|director|chief executive|dermot desmond|michael nicholson|peter lawwell|ownership|recruitment|manager search|next manager|backroom|appointed as|takes charge"),
    ("club_official",      r"celtic (confirm|announce)|statement|ticket|kick.?off time|fixture|rearranged|sfa|spfl announce|uefa|awarded|squad (list|registration)|season book"),
    ("preview",            r"how to watch|tv (channel|details|info)|live stream|what time|everything you need|preview|build.?up|scouting"),
    ("match_report",       r"full.?time|player ratings|match report|\d+-\d+"),
    ("player_moment",      r"spotted|gesture|caught|camera|celebration|touchline (spat|row)|body language|footage|watch:|reaction to"),
    ("fan_reaction",       r"\bfans?\b|supporters?|celtic support|banner|tifo|atmosphere|green brigade|sing|chant"),
    ("stats_data",         r"\bstats?\b|ranked|table shows|valuation|worth £|data|percentage|record of"),
    ("former_player",      r"former celtic|ex.celtic|celtic legend|celtic hero|old celtic"),
    ("rival_voice",        r"derek mcinnes|mcinnes|philippe clement|\b(rangers|hearts|hibs|aberdeen|dundee|motherwell|kilmarnock|falkirk|st mirren|livingston|lask|psv|braga|utrecht|roma|feyenoord|midtjylland|sturm)\b (boss|manager|star|chief|captain|defender|midfielder|striker|winger|keeper)|rival (boss|manager|fans)"),
]


def classify(title):
    t = title.lower()

    # voice-led pieces are judged on who is speaking, after the hard news tests
    for typ, pat in RULES[:6]:                 # xi, kit, youth, intl, officiating, injury
        if re.search(pat, t):
            return typ

    if re.search(JOURNOS, t) and re.search(SAY, t):
        return "journalist_report"

    is_mgr = bool(re.search(MANAGERS, t))
    is_pundit = bool(re.search(PUNDITS, t)) and not is_mgr

    for typ, pat in RULES[6:]:                 # transfers, exits, board, club, etc.
        if re.search(pat, t):
            # a manager or pundit talking ABOUT a transfer is still a quote piece,
            # unless the headline reports the deal itself
            if typ in ("transfer_in_motion", "transfer_link", "exit_news",
                       "board_backroom") and (is_mgr or is_pundit):
                return "manager_newsline" if is_mgr else "pundit_reaction"
            return typ

    if is_pundit:
        return "pundit_reaction"
    if is_mgr:
        # news-bearing manager lines vs routine presser talk
        if re.search(r"confirm|reveal|exit door|board|january|sign|replace|contract|blame|quit|transfer|window|target|deal|strengthen|position|lifts? lid|names? two|rubbish|criticism|allocation|missed", t):
            return "manager_newsline"
        return "manager_presser"
    if re.search(r"own future|my future|contract talks", t):
        return "player_future"
    if re.search(SAY, t) or re.search(r"[‘’“”\"']", title):
        return "player_quotes"
    return "other"


# ------------------------------------------------- page template
TEMPLATE = r'''<title>Shearer Day Sheet</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{{
  --paper:#EEF0EC; --card:#FFFFFF; --ink:#14181A; --ink2:#4C5559; --ink3:#7C868A;
  --rule:#D8DDD9; --over:#1567AE; --under:#B85A16; --mid:#9AA4A8;
  --ui:'Archivo',system-ui,sans-serif; --serif:'Source Serif 4',Georgia,serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace; --lw:170px;
}}
@media (prefers-color-scheme:dark){{ :root:not([data-theme="light"]){{
  --paper:#0E1215; --card:#161B1E; --ink:#E9EDEB; --ink2:#9BA5A9; --ink3:#6E787C;
  --rule:#242B2F; --over:#2F92C9; --under:#CE7A2C; --mid:#5F696D;
}}}}
:root[data-theme="dark"]{{
  --paper:#0E1215; --card:#161B1E; --ink:#E9EDEB; --ink2:#9BA5A9; --ink3:#6E787C;
  --rule:#242B2F; --over:#2F92C9; --under:#CE7A2C; --mid:#5F696D;
}}
*{{box-sizing:border-box}}
body{{background:var(--paper);color:var(--ink);font-family:var(--ui);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:900px;margin:0 auto;padding-inline:20px;padding-block:28px 60px;
  display:flex;flex-direction:column;gap:28px}}
.mono{{font-family:var(--mono);font-variant-numeric:tabular-nums}}
a{{color:inherit}}
h1,h2{{margin:0;text-wrap:balance}}
.over{{color:var(--over)}} .under{{color:var(--under)}}

.mast{{border-bottom:2px solid var(--ink);padding-bottom:13px}}
.kicker{{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink3);font-weight:600}}
.mast h1{{font-size:clamp(27px,6.4vw,40px);font-weight:700;letter-spacing:-.02em;
  line-height:1.05;margin-top:6px}}
.mast .sub{{margin:9px 0 0;color:var(--ink2);font-family:var(--serif);font-size:15.5px;
  max-width:62ch}}
.flag{{display:block;margin-top:7px;font-family:var(--ui);font-size:12.5px;
  color:var(--under);font-weight:500}}

.strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule)}}
.stat{{background:var(--card);padding:13px 15px}}
.stat .v{{font-family:var(--mono);font-size:25px;font-weight:600;
  letter-spacing:-.02em;line-height:1.1}}
.stat .k{{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink3);font-weight:600;margin-top:5px}}
.stat .s{{font-size:12.5px;color:var(--ink2);margin-top:3px}}

section h2{{font-size:18px;font-weight:700;letter-spacing:-.01em}}
section .lede{{font-family:var(--serif);color:var(--ink2);font-size:15px;
  margin:6px 0 0;max-width:64ch}}
.head{{margin-bottom:14px}}

.tbl{{overflow-x:auto;background:var(--card);border:1px solid var(--rule)}}
table{{border-collapse:collapse;width:100%;min-width:540px}}
th{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);
  text-align:left;font-weight:600;padding:9px 12px;border-bottom:1px solid var(--rule)}}
th.num,td.num{{text-align:right}}
td{{padding:9px 12px;border-bottom:1px solid var(--rule);font-size:13.5px;
  vertical-align:middle}}
tr:last-child td{{border-bottom:0}}
td.rk{{color:var(--ink3);font-size:12px;width:26px;text-align:right;padding-right:0}}
td.ti a{{text-decoration:none;font-weight:500}}
td.ti a:hover{{text-decoration:underline}}
td.dt{{white-space:nowrap;font-size:12.5px;color:var(--ink2);font-weight:500}}
td.num{{font-size:13px;white-space:nowrap}}
td.bc{{width:110px;padding-left:4px;padding-right:4px}}
.minibar{{display:block;height:8px;border-radius:2px;min-width:3px}}
.minibar.over{{background:var(--over)}}
.minibar.under{{background:var(--under)}}
.chip{{display:inline-block;font-size:10.5px;letter-spacing:.04em;margin-left:8px;
  padding:2px 7px;border:1px solid var(--rule);border-radius:99px;
  color:var(--ink2);white-space:nowrap;background:var(--paper);vertical-align:1px}}

.chart{{background:var(--card);border:1px solid var(--rule);padding:15px 15px 9px}}
.scale{{position:relative;height:15px;margin-left:calc(var(--lw) + 8px);
  margin-right:110px;font-size:10.5px;color:var(--ink3);font-family:var(--mono)}}
.scale .tk{{position:absolute;transform:translateX(-50%);white-space:nowrap}}
.row{{display:grid;grid-template-columns:var(--lw) 1fr 46px 56px;
  align-items:center;gap:8px;padding:3px 0}}
.rlab{{font-size:13px;font-weight:500;overflow-wrap:anywhere}}
.track{{position:relative;height:17px;background:var(--paper);border-radius:2px}}
.tick{{position:absolute;top:-1px;bottom:-1px;width:1px;background:var(--mid);opacity:.65}}
.tick.t1{{left:50%}}
.bar{{position:absolute;top:2px;bottom:2px;border-radius:3px;display:block}}
.bar.over{{background:var(--over)}} .bar.under{{background:var(--under)}}
.rnum,.rmed{{font-size:12.5px;text-align:right}}
.rnum{{font-weight:600}} .rmed{{color:var(--ink2)}}
.colhead{{display:grid;grid-template-columns:var(--lw) 1fr 46px 56px;gap:8px;
  font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);
  font-weight:600;padding-bottom:6px;border-bottom:1px solid var(--rule);
  margin-bottom:6px}}
.colhead span:nth-child(n+3){{text-align:right}}

footer{{color:var(--ink3);font-size:12px;border-top:1px solid var(--rule);
  padding-top:13px;line-height:1.6}}

#tip{{position:fixed;z-index:50;background:var(--ink);color:var(--paper);
  font-size:12px;line-height:1.4;padding:7px 10px;border-radius:4px;max-width:280px;
  pointer-events:none;opacity:0;transition:opacity .1s}}
#tip.on{{opacity:1}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
@media (max-width:700px){{
  :root{{--lw:118px}}
  .row,.colhead{{grid-template-columns:var(--lw) 1fr 42px 50px;gap:6px}}
  .scale{{margin-right:98px;margin-left:calc(var(--lw) + 6px)}}
  .scale .tk:first-child,.scale .tk:last-child{{display:none}}
  .rlab{{font-size:12px}}
}}
</style>

<div class="wrap">

<header class="mast">
  <div class="kicker">CeltsAreHere day sheet</div>
  <h1>{daylong}</h1>
  <p class="sub">Michael Shearer. Every article, ranked by how many people read it.
  {note}{stale}</p>
</header>

<div class="strip">
  <div class="stat"><div class="v">{nfiled}</div><div class="k">Filed</div>
    <div class="s">site published {siteposts}</div></div>
  <div class="stat"><div class="v">{best_views}</div><div class="k">Best article</div>
    <div class="s">{best_type}</div></div>
  <div class="stat"><div class="v {daymed_side}">{daymed}</div><div class="k">His median</div>
    <div class="s">site median {sitemed}</div></div>
  <div class="stat"><div class="v">{med}</div><div class="k">His running median</div>
    <div class="s">across {ndays} filing days</div></div>
</div>

<section>
  <div class="tbl"><table>
    <thead><tr><th></th><th>Headline</th><th></th><th class="num">Views</th>
      <th class="num">vs his median</th></tr></thead>
    <tbody>{dayrows}</tbody>
  </table></div>
</section>

<section>
  <div class="head">
    <h2>The fortnight behind it</h2>
    <p class="lede">His median each day, and the piece that carried it.</p>
  </div>
  <div class="tbl"><table>
    <thead><tr><th>Day</th><th class="num">Filed</th><th class="num">Median</th>
      <th>Biggest that day</th><th class="num">Views</th></tr></thead>
    <tbody>{recentrows}</tbody>
  </table></div>
</section>

<section>
  <div class="head">
    <h2>Which kinds of story pay</h2>
    <p class="lede">Every type he has filed, measured against his own median of {med}.
    Right of the line beats it. This builds as the days accumulate.</p>
  </div>
  <div class="chart">
    <div class="scale">{ticks}</div>
    <div class="colhead"><span>Story type</span><span></span><span>vs him</span>
      <span>Median</span></div>
    {typerows}
  </div>
</section>

<footer>
  Jetpack page views, counted from publication. An article gathers roughly 84 per cent
  of its lifetime traffic in its first day, so yesterday's numbers are close to final
  and firm up over the two days after. Running median across {nwindow} articles.<br>
  Refreshed {generated}.
</footer>

</div>

<div id="tip" role="status"></div>
<script>
(function(){{
  var tip=document.getElementById('tip');
  document.addEventListener('mouseover',function(ev){{
    var el=ev.target.closest('[data-tip]');
    if(!el){{tip.classList.remove('on');return;}}
    tip.textContent=el.getAttribute('data-tip');tip.classList.add('on');
  }});
  document.addEventListener('mousemove',function(ev){{
    if(!tip.classList.contains('on'))return;
    var x=ev.clientX+14,y=ev.clientY+16,r=tip.getBoundingClientRect();
    if(x+r.width>window.innerWidth-8)x=ev.clientX-r.width-14;
    if(y+r.height>window.innerHeight-8)y=ev.clientY-r.height-16;
    tip.style.left=x+'px';tip.style.top=y+'px';
  }});
  document.addEventListener('mouseleave',function(){{tip.classList.remove('on');}},true);
}})();
</script>
'''


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        fetch(); build()
    else:
        {"fetch": fetch, "build": build}[cmd]()
