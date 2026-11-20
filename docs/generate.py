#!/usr/bin/env python3
"""L'ouverture : publie chaque jour un article sur l'actualité mondiale.

1. récupère les titres du jour dans des flux RSS publics ;
2. demande à l'API Gemini (niveau gratuit) de rédiger un article original ;
3. reconstruit le site statique dans le dossier docs/.
Aucune installation nécessaire : uniquement la bibliothèque standard de Python.
"""
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

SITE_NAME = "L'ouverture"
TAGLINE = "L'actualité du monde, un article par jour."

# Sources d'actualité (flux RSS). Un flux qui ne répond pas est simplement ignoré.
FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("France 24", "https://www.france24.com/fr/rss"),
    ("Le Monde", "https://www.lemonde.fr/international/rss_full.xml"),
    ("ONU Info", "https://news.un.org/feed/subscribe/fr/news/all/rss.xml"),
]
ITEMS_PER_FEED = 8

# Les noms de modèles changent souvent : on peut les remplacer sans toucher au code
# en définissant la variable GEMINI_MODEL dans GitHub.
MODELS = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-flash-latest",
                      "gemini-flash-lite-latest") if m]

DATA = Path("data")
DOCS = Path("docs")
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; LouvertureBot/1.0)"}


# ---------------------------------------------------------------- actualités
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def collect_items():
    items = []
    for name, url in FEEDS:
        try:
            root = ET.fromstring(http_get(url))
        except Exception as e:  # flux indisponible : on passe au suivant
            print(f"[flux ignoré] {name} : {e}")
            continue
        count = 0
        for node in root.iter("item"):
            title = clean(node.findtext("title"), 200)
            link = (node.findtext("link") or "").strip()
            if title and link.startswith("http"):
                items.append({"source": name, "title": title, "link": link,
                              "summary": clean(node.findtext("description"), 400)})
                count += 1
            if count >= ITEMS_PER_FEED:
                break
    return items


# -------------------------------------------------------------------- Gemini
def build_prompt(items, today_fr):
    lines = "\n".join(f"[{i}] ({it['source']}) {it['title']} : {it['summary']}"
                      for i, it in enumerate(items))
    return f"""Tu es rédacteur en chef du média en ligne « {SITE_NAME} ». Nous sommes le {today_fr}.
Voici l'actualité mondiale du jour, chaque élément étant numéroté :

{lines}

Rédige UN article original, en français, de 500 à 700 mots, sur le sujet mondial le plus important du jour, en recoupant plusieurs sources quand c'est possible.
Règles strictes :
- Utilise uniquement les informations ci-dessus. N'invente aucun fait, chiffre, date ni citation.
- Reformule avec tes propres mots, ne recopie pas les phrases des sources.
- Ton neutre, clair et factuel. Distingue ce qui est confirmé de ce qui ne l'est pas.
- Si les éléments sont insuffisants, dis-le honnêtement plutôt que de combler les vides.

Réponds uniquement avec un objet JSON de cette forme :
{{"title": "titre sobre et précis", "description": "résumé en une phrase, 160 caractères maximum", "paragraphs": ["paragraphe 1", "paragraphe 2", "..."], "sources": [numéros des éléments utilisés]}}"""


def call_gemini(prompt, key):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4,
                             "responseMimeType": "application/json"},
    }).encode("utf-8")
    last = "aucune réponse"
    for model in MODELS:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                return json.loads(text)
            except urllib.error.HTTPError as e:
                last = f"{model} : HTTP {e.code} {e.read()[:200]!r}"
                print("[gemini]", last)
                if e.code in (429, 500, 503):
                    time.sleep(20 * (attempt + 1))
                    continue
                break  # modèle inconnu, clé refusée... on essaie le modèle suivant
            except Exception as e:
                last = f"{model} : {e}"
                print("[gemini]", last)
                time.sleep(5)
    sys.exit(f"Échec de la génération : {last}")


def validate(article, items):
    if not isinstance(article, dict):
        raise ValueError("réponse inattendue")
    title = str(article.get("title", "")).strip()
    paragraphs = [str(p).strip() for p in article.get("paragraphs", []) if str(p).strip()]
    if not title or len(paragraphs) < 3:
        raise ValueError("article incomplet")
    chosen = []
    for i in article.get("sources", []):
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items) and i not in chosen:
            chosen.append(i)
    if not chosen:
        raise ValueError("aucune source citée")
    return {
        "title": title,
        "description": str(article.get("description", "")).strip()[:160],
        "paragraphs": paragraphs,
        "sources": [{"name": items[i]["source"], "title": items[i]["title"],
                     "link": items[i]["link"]} for i in chosen],
    }


# ---------------------------------------------------------------------- site
def fr_date(iso):
    d = date.fromisoformat(iso)
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {MONTHS[d.month - 1]} {d.year}"


def site_base():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        owner = owner.lower()
        if name.lower() == f"{owner}.github.io":
            return f"https://{owner}.github.io"
        return f"https://{owner}.github.io/{name}"
    return ""


MARK = ('<svg class="mark" viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">'
        '<path d="M26 9.5A12 12 0 1 0 26 22.5" fill="none" stroke="currentColor" '
        'stroke-width="3.2" stroke-linecap="round"/></svg>')

PALETTES = [("#0e1f4d", "#2446c9", "#ffd166"), ("#12343b", "#2d6a6f", "#f4a261"),
            ("#2b1740", "#6a3d9a", "#ffb4a2"), ("#14213d", "#3a5a8c", "#fca311"),
            ("#3a1c1c", "#a23b3b", "#f1e3c8")]

FAVICON = ('<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' '
           'viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'7\' fill=\'%232446c9\'/%3E'
           '%3Cpath d=\'M25 10A11 11 0 1 0 25 22\' fill=\'none\' stroke=\'white\' stroke-width=\'3.4\' '
           'stroke-linecap=\'round\'/%3E%3C/svg%3E">')

SHARE = """<button class="share" type="button" id="share">Partager cet article</button>
<script>document.getElementById("share").onclick=function(){var d={title:document.title,url:location.href};if(navigator.share){navigator.share(d)}else if(navigator.clipboard){navigator.clipboard.writeText(d.url);this.textContent="Lien copié"}}</script>"""


def cover_svg(seed, cls="cover"):
    """Illustration abstraite unique, calculée à partir du titre : gratuite et sans droits."""
    n = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    uid = f"{n % 10**8:08d}"
    c1, c2, accent = PALETTES[n % len(PALETTES)]
    n //= 10
    cx = 800 * (0.55 + (n % 30) / 100)
    n //= 30
    cy = 450 * (0.30 + (n % 40) / 100)
    n //= 40
    rot = n % 360
    parts, dot = [], ""
    for k in range(6):
        rad = 36 + k * 38
        t0 = math.radians(rot + k * 31)
        t1 = t0 + math.radians(290)
        x0, y0 = cx + rad * math.cos(t0), cy + rad * math.sin(t0)
        x1, y1 = cx + rad * math.cos(t1), cy + rad * math.sin(t1)
        parts.append(f'<path d="M{x0:.1f} {y0:.1f}A{rad} {rad} 0 1 1 {x1:.1f} {y1:.1f}" fill="none" '
                     f'stroke="#fff" stroke-opacity="{0.9 - k * 0.1:.2f}" stroke-width="{2 + k * 0.6:.1f}" '
                     'stroke-linecap="round"/>')
        if k == 2:  # le point d'accent passe par l'ouverture de l'anneau
            tm = t1 + math.radians(35)
            dot = (f'<circle cx="{cx + rad * math.cos(tm):.1f}" cy="{cy + rad * math.sin(tm):.1f}" '
                   f'r="11" fill="{accent}"/>')
    return (f'<svg class="{cls}" viewBox="0 0 800 450" preserveAspectRatio="xMidYMid slice" aria-hidden="true">'
            f'<defs><linearGradient id="g{uid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/>'
            f'<stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
            f'<rect width="800" height="450" fill="url(#g{uid})"/>{"".join(parts)}{dot}</svg>')


CSS = """:root{--bg:#f3f6f8;--ink:#101b2d;--muted:#5b6678;--accent:#2446c9;--line:#d5dce4}
@media (prefers-color-scheme:dark){:root{--bg:#0e1624;--ink:#e6ebf2;--muted:#98a3b5;--accent:#8fa8ff;--line:#26324a}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:19px/1.75 "Iowan Old Style",Charter,Georgia,serif}
.wrap{max-width:38rem;margin:0 auto;padding:0 1.25rem}
a{color:var(--accent);text-underline-offset:.18em}
a:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:2px}
.masthead,h1,h2,.date,.archive li{font-family:"Avenir Next","Segoe UI",system-ui,sans-serif}
.masthead{padding:2.2rem 1.25rem 1.6rem}
.masthead a.brand{display:inline-flex;align-items:center;gap:.65rem;color:var(--ink);text-decoration:none;font-size:2rem;font-weight:750;letter-spacing:-.02em}
.mark{color:var(--accent);flex:none}
.masthead p{margin:.3rem 0 0;color:var(--muted);font-size:.95rem}
h1{font-size:2.15rem;line-height:1.16;letter-spacing:-.025em;margin:.3rem 0 1rem}
h1 a{color:var(--ink);text-decoration:none}
h1 a:hover{text-decoration:underline}
h2{font-size:1.15rem;margin:2.6rem 0 .8rem}
.date{font-size:.9rem;line-height:1.4;color:var(--muted);margin:0}
.lede{font-size:1.15rem;color:var(--muted);margin:0 0 1.4rem}
article p{margin:0 0 1.2rem}
.sources ul{padding-left:1.2rem;margin:0;font-size:.95rem}
.sources li{margin-bottom:.4rem}
.note{font-size:.88rem;line-height:1.55;color:var(--muted);border-left:3px solid var(--accent);padding-left:.9rem;margin-top:2rem}
.archive ul{list-style:none;margin:0;padding:0}
.archive li{padding:.85rem 0;border-top:1px solid var(--line)}
.archive li a{display:block;color:var(--ink);font-weight:600;text-decoration:none;line-height:1.35}
.archive li a:hover{color:var(--accent)}
.empty{color:var(--muted)}
footer{padding:3rem 1.25rem 2.5rem;color:var(--muted);font:.85rem/1.4 "Avenir Next","Segoe UI",system-ui,sans-serif}
.masthead{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
.masthead .nav{margin-top:.75rem;font-size:.95rem;color:var(--muted);text-decoration:none}
.masthead .nav:hover{color:var(--accent)}
.cover{display:block;width:100%;aspect-ratio:16/9;border-radius:6px;margin:0 0 1.4rem}
.lead .cover{margin-bottom:1.1rem}
.archive li{display:flex;gap:1rem;align-items:center}
.thumb{flex:none;width:88px;height:88px;border-radius:4px}
.archive .date{display:block}
.share{font:.95rem "Avenir Next","Segoe UI",system-ui,sans-serif;color:var(--accent);background:none;border:1px solid var(--accent);border-radius:999px;padding:.5rem 1.1rem;margin-top:1.4rem;cursor:pointer}
.share:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def layout(title, description, body, url, root, kind):
    home = root or "./"
    year = datetime.now(timezone.utc).year
    canonical = f'<link rel="canonical" href="{esc(url)}">' if url else ""
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
{canonical}
<meta property="og:type" content="{kind}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<link rel="stylesheet" href="{root}style.css">
{FAVICON}
</head>
<body>
<header class="wrap masthead">
<div>
<a class="brand" href="{home}">{MARK}{esc(SITE_NAME)}</a>
<p>{esc(TAGLINE)}</p>
</div>
<a class="nav" href="{root}a-propos.html">À propos</a>
</header>
<main class="wrap">
{body}
</main>
<footer class="wrap">© {year} {esc(SITE_NAME)}</footer>
</body>
</html>
"""


def article_page(a, base):
    paras = "\n".join(f"<p>{esc(p)}</p>" for p in a["paragraphs"])
    srcs = "\n".join(f'<li><a href="{esc(s["link"])}" rel="noopener">{esc(s["title"])}</a> '
                     f'({esc(s["name"])})</li>' for s in a["sources"])
    body = f"""<article>
{cover_svg(a["date"] + a["title"])}
<p class="date">{fr_date(a["date"])}</p>
<h1>{esc(a["title"])}</h1>
<p class="lede">{esc(a["description"])}</p>
{paras}
<section class="sources">
<h2>Sources</h2>
<ul>
{srcs}
</ul>
</section>
<p class="note">Cet article a été rédigé avec l'aide d'une intelligence artificielle à partir des sources ci-dessus. Pour vérifier un fait, reportez-vous à ces sources.</p>
{SHARE}
</article>"""
    url = f'{base}/articles/{a["date"]}.html' if base else ""
    return layout(f'{a["title"]} | {SITE_NAME}', a["description"], body, url, "../", "article")


def index_page(articles, base):
    if not articles:
        body = '<p class="empty">Le premier article arrive bientôt.</p>'
    else:
        last = articles[0]
        body = f"""<section class="lead">
<a href="articles/{last["date"]}.html" aria-label="Lire l'article">{cover_svg(last["date"] + last["title"])}</a>
<p class="date">{fr_date(last["date"])}</p>
<h1><a href="articles/{last["date"]}.html">{esc(last["title"])}</a></h1>
<p class="lede">{esc(last["description"])}</p>
<p><a href="articles/{last["date"]}.html">Lire l'article</a></p>
</section>"""
        if len(articles) > 1:
            rows = "\n".join(
                f'<li>{cover_svg(a["date"] + a["title"], "thumb")}<div><span class="date">{fr_date(a["date"])}</span>'
                f'<a href="articles/{a["date"]}.html">{esc(a["title"])}</a></div></li>'
                for a in articles[1:])
            body += f'\n<section class="archive">\n<h2>Articles précédents</h2>\n<ul>\n{rows}\n</ul>\n</section>'
    url = f"{base}/" if base else ""
    return layout(f"{SITE_NAME} | {TAGLINE}", TAGLINE, body, url, "", "website")


def about_page(base):
    names = ", ".join(n for n, _ in FEEDS)
    body = f"""<article>
<h1>À propos</h1>
<p>{esc(SITE_NAME)} propose chaque jour un article sur le sujet qui domine l'actualité mondiale, à lire en quelques minutes.</p>
<h2>Comment sont faits les articles</h2>
<p>Chaque matin, un programme rassemble les titres publiés par des médias internationaux ({esc(names)}). Une intelligence artificielle rédige ensuite un article original à partir de ces informations, sans en recopier les phrases. Chaque article indique ses sources, avec un lien vers l'original.</p>
<h2>Ce qu'il faut savoir</h2>
<p>Les articles sont rédigés avec l'aide d'une intelligence artificielle et peuvent contenir des erreurs. Pour vérifier un fait, reportez-vous aux sources citées en bas de chaque article.</p>
</article>"""
    url = f"{base}/a-propos.html" if base else ""
    return layout(f"À propos | {SITE_NAME}", f"Comment {SITE_NAME} fabrique ses articles.", body, url, "", "website")


def build_site():
    articles = [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(DATA.glob("*.json"), reverse=True)]
    base = site_base()
    (DOCS / "articles").mkdir(parents=True, exist_ok=True)
    (DOCS / "style.css").write_text(CSS, encoding="utf-8")
    (DOCS / "index.html").write_text(index_page(articles, base), encoding="utf-8")
    (DOCS / "a-propos.html").write_text(about_page(base), encoding="utf-8")
    for a in articles:
        (DOCS / "articles" / f'{a["date"]}.html').write_text(article_page(a, base), encoding="utf-8")
    if base:  # plan du site pour Google
        urls = [f"<url><loc>{base}/</loc></url>", f"<url><loc>{base}/a-propos.html</loc></url>"] + [
            f'<url><loc>{base}/articles/{a["date"]}.html</loc><lastmod>{a["date"]}</lastmod></url>'
            for a in articles]
        (DOCS / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n", encoding="utf-8")


# ---------------------------------------------------------------------- main
def main():
    today = datetime.now(timezone.utc).date()
    DATA.mkdir(exist_ok=True)
    target = DATA / f"{today.isoformat()}.json"
    if target.exists() and os.environ.get("FORCE") != "1":
        print("L'article du jour existe déjà.")
    else:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            sys.exit("Clé GEMINI_API_KEY manquante : ajoutez-la dans les secrets du dépôt.")
        items = collect_items()
        if len(items) < 5:
            sys.exit("Trop peu d'actualités récupérées. Nouvel essai demain.")
        prompt = build_prompt(items, fr_date(today.isoformat()))
        article = None
        for _ in range(2):
            try:
                article = validate(call_gemini(prompt, key), items)
                break
            except ValueError as e:
                print("[article refusé]", e)
        if article is None:
            sys.exit("Aucun article valide généré aujourd'hui.")
        article["date"] = today.isoformat()
        target.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Article créé :", article["title"])
    build_site()


if __name__ == "__main__":
    main()
