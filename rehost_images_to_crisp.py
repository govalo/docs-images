#!/usr/bin/env python3
"""
rehost_images_to_crisp.py
=========================

Move your Crisp helpdesk article images off Mantle's dying CDN and onto GitHub.

It reads every article straight from Crisp, finds the images still pointing at
cdn.heymantle.com, downloads each one into THIS repo's images/ folder, and
rewrites the article in Crisp to point at this repo's public raw URL instead.

Designed to run from a GitHub Actions workflow in a PUBLIC repo (so the raw URLs
load for everyone). The docs repo can stay private; only the screenshots live
here. Run it BEFORE Mantle shuts down, since it pulls from cdn.heymantle.com.

Env (provided by the workflow)
------------------------------
    CRISP_WEBSITE_ID, CRISP_TOKEN_ID, CRISP_TOKEN_KEY   (Crisp website token)
    PUBLIC_RAW_BASE   e.g. https://raw.githubusercontent.com/govalo/help-images/main

Usage
-----
    python rehost_images_to_crisp.py --dry-run        # report, change nothing
    python rehost_images_to_crisp.py --only 1         # do one article as a test
    python rehost_images_to_crisp.py                  # do everything
"""

import argparse
import base64
import hashlib
import os
import re
import sys
import time
import urllib.parse
import pathlib

API = "https://api.crisp.chat/v1"
PAUSE = 0.5
HOST = "cdn.heymantle.com"          # only these images get rehosted

# capture (prefix)(url)(suffix) so we can swap just the URL
IMG_MD = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")
IMG_HTML = re.compile(r'(<img[^>]+src=["\'])([^"\']+)(["\'])')


class Crisp:
    def __init__(self, wid, tid, tkey, tier="website"):
        import requests
        self.wid = wid
        self.s = requests.Session()
        basic = base64.b64encode(f"{tid}:{tkey}".encode()).decode()
        self.s.headers.update({
            "Authorization": f"Basic {basic}",
            "X-Crisp-Tier": tier,
            "Content-Type": "application/json",
        })

    def _call(self, method, path, **kw):
        for attempt in range(8):
            r = self.s.request(method, API + path, **kw)
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else min(60, 2 ** attempt)
                except ValueError:
                    wait = min(60, 2 ** attempt)
                print(f"    rate-limited by Crisp, waiting {wait:.0f}s...", flush=True)
                time.sleep(wait)
                continue
            if not r.ok:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
            time.sleep(PAUSE)
            body = r.json() if r.text else {}
            return body.get("data", body)
        raise RuntimeError(f"gave up after retries (rate-limited): {method} {path}")

    def article_ids(self, locale):
        ids, page = [], 1
        while True:
            rows = self._call(
                "GET", f"/website/{self.wid}/helpdesk/locale/{locale}/articles/{page}")
            if not rows:
                break
            for a in rows:
                aid = a.get("article_id") or a.get("id")
                if aid:
                    ids.append(aid)
            page += 1
        return ids

    def get_article(self, locale, aid):
        return self._call(
            "GET", f"/website/{self.wid}/helpdesk/locale/{locale}/article/{aid}")

    def update_content(self, locale, aid, content):
        return self._call(
            "PATCH", f"/website/{self.wid}/helpdesk/locale/{locale}/article/{aid}",
            json={"content": content})


def local_name(url):
    """Deterministic, collision-free filename from the full URL."""
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        ext = ".png"
    return "images/" + hashlib.sha1(url.encode()).hexdigest()[:16] + ext


def download(url, session, repo_root):
    rel = local_name(url)
    dest = repo_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return rel
    r = session.get(url, timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return rel


def rehost(content, session, repo_root, public_base, do_download):
    """Return (new_content, num_images). Downloads + rewrites Mantle image URLs."""
    count = [0]

    def repl(m):
        pre, url, post = m.group(1), m.group(2), m.group(3)
        if HOST not in url:
            return m.group(0)
        count[0] += 1
        if not do_download:
            return m.group(0)            # dry run: report only
        try:
            rel = download(url, session, repo_root)
        except Exception as e:
            print(f"    image download FAILED, leaving as-is: {url} ({e})")
            return m.group(0)
        return pre + public_base.rstrip("/") + "/" + rel + post

    content = IMG_MD.sub(repl, content)
    content = IMG_HTML.sub(repl, content)
    return content, count[0]


def main():
    ap = argparse.ArgumentParser(description="Rehost Mantle images into this repo and update Crisp")
    ap.add_argument("--locale", default="en")
    ap.add_argument("--only", type=int, default=0, help="Max articles to update (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Report only; download nothing, change nothing")
    args = ap.parse_args()

    wid = os.environ.get("CRISP_WEBSITE_ID")
    tid = os.environ.get("CRISP_TOKEN_ID")
    tkey = os.environ.get("CRISP_TOKEN_KEY")
    public_base = os.environ.get("PUBLIC_RAW_BASE", "").strip()
    if not (wid and tid and tkey):
        sys.exit("Missing CRISP_WEBSITE_ID / CRISP_TOKEN_ID / CRISP_TOKEN_KEY.")
    if not args.dry_run and not public_base:
        sys.exit("Missing PUBLIC_RAW_BASE (the repo's raw URL base).")

    import requests
    session = requests.Session()
    repo_root = pathlib.Path(".")
    crisp = Crisp(wid, tid, tkey)

    print(f"Reading articles (locale: {args.locale})...")
    ids = crisp.article_ids(args.locale)
    print(f"{len(ids)} articles found.\n")

    updated = images = scanned_with_images = 0
    for aid in ids:
        art = crisp.get_article(args.locale, aid)
        content = art.get("content") or ""
        title = art.get("title") or aid
        if HOST not in content:
            continue
        new_content, n = rehost(content, session, repo_root, public_base,
                                do_download=not args.dry_run)
        if n:
            scanned_with_images += 1
            images += n
        if args.dry_run:
            print(f"  would update: {title}  ({n} image(s))")
            continue
        if new_content != content:
            crisp.update_content(args.locale, aid, new_content)
            updated += 1
            print(f"  updated: {title}  ({n} image(s))")
        if args.only and updated >= args.only:
            print(f"Reached --only {args.only}.")
            break

    print()
    if args.dry_run:
        print(f"Dry run. {scanned_with_images} articles contain {images} Mantle "
              f"image(s) that would be rehosted. Nothing was changed.")
    else:
        print(f"Done. {updated} articles updated, {images} images rehosted into images/.")
        print("The workflow will now commit the images/ folder to this repo.")


if __name__ == "__main__":
    main()
