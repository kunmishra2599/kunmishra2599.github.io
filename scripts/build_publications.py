#!/usr/bin/env python3
"""
Build publications.html from publications.json, and keep publications.json in
sync with the ORCID record.

    python3 scripts/build_publications.py            # sync from ORCID, then render
    python3 scripts/build_publications.py --render   # render only, no network

How it works
------------
publications.json is the single source of truth for the page. Each item is one
publication with a type (paper / preprint / abstract / thesis), authors, venue,
date and optional links.

`sync` asks the ORCID public API for every work on the profile. Any DOI that is
not yet in publications.json is looked up on Crossref (title, authors, venue,
date) and PubMed (PMID), and appended. Existing items are never overwritten, so
hand edits like equal-contribution markers, notes and hidden flags survive.

`render` writes publications.html: a year-grouped timeline plus schema.org
JSON-LD. Standard library only, no third-party packages.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "publications.json"
OUT = ROOT / "publications.html"
SITE = "https://kunalmishra.me"
UA = "kunalmishra.me publications builder (mailto:kunmishra2599@gmail.com)"

TYPE_LABEL = {
    "paper": "Paper",
    "preprint": "Preprint",
    "abstract": "Conference abstract",
    "thesis": "Thesis",
}


# ----------------------------------------------------------------------------
# Network helpers
# ----------------------------------------------------------------------------
def get_json(url: str, accept: str = "application/json") -> dict | None:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"  warn: {url} -> {e}", file=sys.stderr)
        return None


def orcid_dois(orcid: str) -> list[tuple[str, str]]:
    """Return [(doi, orcid_type)] for every work on the ORCID record."""
    data = get_json(f"https://pub.orcid.org/v3.0/{orcid}/works")
    if not data:
        return []
    out = []
    for group in data.get("group", []):
        summary = group["work-summary"][0]
        for ext in group.get("external-ids", {}).get("external-id", []):
            if ext["external-id-type"] == "doi":
                out.append((ext["external-id-value"].lower(), summary.get("type", "")))
                break
    return out


def initials(given: str) -> str:
    parts = re.split(r"[\s.\-]+", given.strip())
    return "".join(p[0].upper() for p in parts if p)


def crossref_item(doi: str, me: dict) -> dict | None:
    m = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    if not m:
        return None
    m = m["message"]
    authors = []
    for a in m.get("author", []):
        fam, giv = a.get("family", ""), a.get("given", "")
        if not fam:
            continue
        entry = {"name": f"{fam} {initials(giv)}".strip()}
        if fam.lower() == me["family"].lower() and initials(giv).startswith(me["initial"]):
            entry["me"] = True
        authors.append(entry)

    parts = (m.get("published-print") or m.get("published-online") or m.get("posted")
             or m.get("issued") or {}).get("date-parts", [[None]])[0]
    datestr = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p is not None)

    ctype = m.get("type", "")
    if ctype == "posted-content" or m.get("subtype") == "preprint":
        ptype = "preprint"
    elif ctype in ("dissertation",):
        ptype = "thesis"
    else:
        ptype = "paper"

    venue = (m.get("container-title") or [None])[0]
    if ptype == "preprint" and not venue:
        venue = (m.get("institution") or [{}])[0].get("name") or (m.get("group-title") or "bioRxiv")

    item = {
        "doi": doi,
        "type": ptype,
        "title": html.unescape(re.sub(r"<[^>]+>", "", (m.get("title") or [""])[0])).strip(),
        "authors": authors,
        "venue": venue or "",
        "date": datestr,
    }
    for k_src, k_dst in (("volume", "volume"), ("issue", "issue"), ("page", "pages")):
        if m.get(k_src):
            item[k_dst] = m[k_src]
    return item


def pubmed_id(doi: str) -> str | None:
    d = get_json(
        "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?tool=kunalmishra.me&email=kunmishra2599@gmail.com&format=json&ids={urllib.parse.quote(doi)}"
    )
    if not d:
        return None
    for rec in d.get("records", []):
        if rec.get("pmid"):
            return rec["pmid"]
    return None


# ----------------------------------------------------------------------------
# Sync
# ----------------------------------------------------------------------------
def sync(data: dict) -> bool:
    known = {i["doi"].lower() for i in data["items"] if i.get("doi")}
    added = 0
    for doi, _otype in orcid_dois(data["orcid"]):
        if doi in known:
            continue
        print(f"  new DOI on ORCID: {doi}")
        item = crossref_item(doi, data["me"])
        if not item:
            continue
        if item["type"] == "paper":
            pmid = pubmed_id(doi)
            if pmid:
                item["pmid"] = pmid
        item["auto_added"] = date.today().isoformat()
        data["items"].append(item)
        known.add(doi)
        added += 1
    if added:
        data["items"].sort(key=lambda i: str(i.get("date", "")), reverse=True)
    print(f"  sync: {added} new item(s)")
    return added > 0


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------
def esc(s: str) -> str:
    return html.escape(s, quote=True)


def year_of(item: dict) -> str:
    return str(item.get("date", ""))[:4]


def author_html(a: dict) -> str:
    name = esc(a["name"])
    if a.get("equal"):
        name += '<sup class="eq" title="Equal contribution">&#8225;</sup>'
    return f'<span class="me">{name}</span>' if a.get("me") else name


def venue_html(item: dict) -> str:
    v = esc(item.get("venue", ""))
    t = item["type"]
    if t in ("paper", "abstract"):
        bits = v
        if year_of(item):
            bits += f" {year_of(item)}"
        vol = item.get("volume")
        if vol:
            bits += f";{esc(vol)}"
            if item.get("issue"):
                bits += f"({esc(item['issue'])})"
            if item.get("pages"):
                bits += f":{esc(item['pages'])}"
        if item.get("note"):
            bits += f". {esc(item['note'])}"
        return bits
    if t == "preprint":
        bits = f"{v} {year_of(item)}".strip()
        if item.get("note"):
            bits += f". {esc(item['note'])}"
        return bits
    return v


def links_html(item: dict) -> str:
    links = []
    if item.get("doi"):
        label = "bioRxiv" if item["type"] == "preprint" and "biorxiv" in item.get("venue", "").lower() else "DOI"
        links.append((label, f"https://doi.org/{item['doi']}"))
    if item.get("pmid"):
        links.append(("PubMed", f"https://pubmed.ncbi.nlm.nih.gov/{item['pmid']}/"))
    for l in item.get("links", []):
        links.append((l["label"], l["url"]))
    if not links:
        return ""
    a = "\n".join(
        f'            <a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(l)}</a>'
        for l, u in links
    )
    return f'          <p class="pub-links">\n{a}\n          </p>\n'


def item_html(item: dict) -> str:
    authors = ", ".join(author_html(a) for a in item["authors"])
    return (
        '        <li class="pub">\n'
        f'          <span class="pub-tag pub-tag--{item["type"]}">{TYPE_LABEL[item["type"]]}</span>\n'
        f'          <p class="pub-title">{item["title"]}</p>\n'
        f'          <p class="pub-authors">{authors}</p>\n'
        f'          <p class="pub-venue">{venue_html(item)}</p>\n'
        f"{links_html(item)}"
        "        </li>\n"
    )


def timeline_html(items: list[dict]) -> str:
    years: dict[str, list[dict]] = {}
    for it in items:
        years.setdefault(year_of(it), []).append(it)
    rows = []
    for y in sorted(years, reverse=True):
        entries = "".join(item_html(i) for i in years[y])
        rows.append(
            '      <li class="pub-year-row">\n'
            f'        <h2 class="pub-year">{esc(y)}</h2>\n'
            '        <ul class="pub-entries">\n'
            f"{entries}"
            "        </ul>\n"
            "      </li>\n"
        )
    return "".join(rows)


def jsonld(items: list[dict]) -> str:
    person = {"@id": f"{SITE}/#person"}
    elems = []
    for pos, it in enumerate(items, 1):
        plain_title = re.sub(r"<[^>]+>", "", it["title"])
        if it["type"] == "thesis":
            obj = {
                "@type": "Thesis",
                "name": plain_title,
                "datePublished": it.get("date", ""),
                "inSupportOf": it.get("degree", ""),
                "publisher": {"@type": "CollegeOrUniversity", "name": it.get("institution", "")},
                "author": [person],
            }
            if it.get("institution_url"):
                obj["publisher"]["url"] = it["institution_url"]
        else:
            obj = {
                "@type": "ScholarlyArticle",
                "name": plain_title,
                "headline": plain_title,
                "datePublished": it.get("date", ""),
                "author": [person],
            }
            if it["type"] == "preprint":
                obj["publication"] = it.get("venue", "")
                obj["creativeWorkStatus"] = "Preprint"
            else:
                obj["isPartOf"] = {"@type": "Periodical", "name": it.get("venue", "")}
                if it["type"] == "abstract":
                    obj["creativeWorkStatus"] = "Conference abstract"
            for k in ("volume", "issue"):
                if it.get(k):
                    obj[k + "Number"] = it[k]
            if it.get("pages"):
                obj["pagination"] = it["pages"]
            if it.get("doi"):
                obj["url"] = f"https://doi.org/{it['doi']}"
                obj["identifier"] = {"@type": "PropertyValue", "propertyID": "DOI", "value": it["doi"]}
            if it.get("about"):
                obj["about"] = it["about"]
        elems.append({"@type": "ListItem", "position": pos, "item": obj})

    doc = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "@id": f"{SITE}/publications.html#publications",
        "url": f"{SITE}/publications.html",
        "name": "Publications",
        "isPartOf": {"@id": f"{SITE}/#website"},
        "about": person,
        "dateModified": date.today().isoformat(),
        "mainEntity": {
            "@type": "ItemList",
            "itemListOrder": "https://schema.org/ItemListOrderDescending",
            "numberOfItems": len(elems),
            "itemListElement": elems,
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />

  <!-- GENERATED FILE. Do not edit by hand: edit publications.json and run
       python3 scripts/build_publications.py
       A GitHub Action runs the same script weekly to pull new works from ORCID. -->

  <meta name="description" content="Peer-reviewed papers, preprints, conference abstracts and theses by Kunal Mishra, Ph.D., on systems genetics, macrophage biology, single-cell transcriptomics, and chronic kidney disease." />
  <title>Publications | Kunal Mishra, Ph.D.</title>
  <link rel="canonical" href="__SITE__/publications.html" />
  <meta name="robots" content="index, follow, max-image-preview:large" />
  <meta name="author" content="Kunal Mishra" />
  <meta name="theme-color" content="#ffffff" />

  <link rel="icon" type="image/svg+xml" href="favicon.svg" />
  <link rel="alternate icon" href="favicon.ico" />

  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="Kunal Mishra" />
  <meta property="og:title" content="Publications | Kunal Mishra, Ph.D." />
  <meta property="og:description" content="Papers, preprints, abstracts and theses on systems genetics, macrophage biology, and chronic kidney disease." />
  <meta property="og:url" content="__SITE__/publications.html" />
  <meta property="og:image" content="__SITE__/profile-1200.jpeg" />

  <link rel="stylesheet" href="styles.css" />

  <style>
    /* Scoped to this page. Prefixed so nothing collides with styles.css. */
    .pub-header { margin-bottom: 1.5rem; }
    .pub-back { display: inline-block; margin-bottom: 1.25rem; font-size: 0.9rem; }
    .pub-header .bio { max-width: 68ch; }
    .pub-legend { margin: 0 0 0.5rem; font-size: 0.85rem; color: var(--muted); }

    /* Timeline: year on the left, entries on the right */
    .pub-timeline { list-style: none; margin: 0; padding: 0; }
    .pub-year-row {
      display: grid;
      grid-template-columns: 6rem 1fr;
      column-gap: 2.5rem;
      padding: 1.75rem 0;
      border-top: 1px solid rgba(0, 0, 0, 0.1);
    }
    .pub-year-row:last-child { border-bottom: 1px solid rgba(0, 0, 0, 0.1); }
    .pub-year {
      margin: 0;
      font-size: 1.5rem;
      font-weight: 700;
      line-height: 1.15;
      letter-spacing: -0.02em;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .pub-entries { list-style: none; margin: 0; padding: 0; min-width: 0; }
    .pub { margin: 0 0 1.75rem; max-width: 68ch; }
    .pub:last-child { margin-bottom: 0; }

    /* Type badge */
    .pub-tag {
      display: inline-block;
      margin-bottom: 0.45rem;
      padding: 0.1rem 0.55rem;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.07em;
      text-transform: uppercase;
      line-height: 1.6;
    }
    .pub-tag--paper { background: var(--text); color: #ffffff; }
    .pub-tag--preprint { background: transparent; color: var(--text); border-color: var(--text); }
    .pub-tag--abstract { background: #d3dae3; color: var(--text); }
    .pub-tag--thesis { background: #e4dccf; color: var(--text); }

    .pub-title { margin: 0 0 0.35rem; font-size: 1.02rem; font-weight: 600; line-height: 1.4; }
    .pub-authors { margin: 0 0 0.3rem; font-size: 0.92rem; line-height: 1.55; opacity: 0.85; }
    .pub-authors .me { font-weight: 700; opacity: 1; }
    .pub-authors .eq { font-size: 0.7em; line-height: 0; margin-left: 1px; }
    .pub-venue { margin: 0 0 0.45rem; font-size: 0.92rem; font-style: italic; opacity: 0.75; }
    .pub-links { margin: 0; font-size: 0.86rem; }
    .pub-links a + a { margin-left: 0.85rem; }

    @media (max-width: 640px) {
      .pub-year-row { grid-template-columns: 1fr; row-gap: 0.75rem; padding: 1.25rem 0; }
      .pub-year { font-size: 1.25rem; }
      .pub-title { font-size: 0.98rem; }
    }
  </style>

  <script type="application/ld+json">
__JSONLD__
  </script>
</head>

<body>
  <main class="page">
    <header class="pub-header">
      <a class="pub-back" href="/">&larr; Kunal Mishra</a>
      <h1>Publications</h1>
      <p class="bio">
        Papers, preprints, conference abstracts and theses on systems genetics, macrophage
        biology, and chronic kidney disease. This list is kept in sync with my
        <a href="https://orcid.org/__ORCID__" target="_blank" rel="noopener noreferrer">ORCID</a>
        record. Citation counts and the full record live on
        <a href="https://scholar.google.com/citations?user=75vx5LkAAAAJ&amp;hl=en" target="_blank" rel="noopener noreferrer">Google Scholar</a>.
      </p>
__LEGEND__    </header>

    <ol class="pub-timeline">
__TIMELINE__    </ol>

    <footer class="site-footer">
      <p>Made with <span class="heart" aria-label="love">&#10084;&#65039;</span> by Kunal Mishra, &copy; __YEAR__</p>
    </footer>
  </main>
</body>
</html>
"""


def render(data: dict) -> str:
    items = [i for i in data["items"] if not i.get("hidden")]
    items.sort(key=lambda i: str(i.get("date", "")), reverse=True)
    has_equal = any(a.get("equal") for i in items for a in i["authors"])
    legend = (
        '      <p class="pub-legend"><sup>&#8225;</sup> equal contribution</p>\n' if has_equal else ""
    )
    return (
        TEMPLATE.replace("__SITE__", SITE)
        .replace("__ORCID__", data["orcid"])
        .replace("__JSONLD__", jsonld(items))
        .replace("__LEGEND__", legend)
        .replace("__TIMELINE__", timeline_html(items))
        .replace("__YEAR__", str(date.today().year))
    )


def main(argv: list[str]) -> int:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if "--render" not in argv:
        print("Syncing with ORCID...")
        if sync(data):
            DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"  wrote {DATA.name}")
    out = render(data)
    if OUT.exists() and OUT.read_text(encoding="utf-8") == out:
        print(f"{OUT.name} unchanged")
    else:
        OUT.write_text(out, encoding="utf-8")
        print(f"wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
