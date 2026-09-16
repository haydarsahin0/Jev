"""arXiv 406 teshisi: hangi istek varyanti calisiyor?  (gecici dosya)"""
import urllib.parse, urllib.request, urllib.error

SEARCH = " OR ".join("cat:" + c for c in ["cs.AI", "cs.CL", "cs.LG", "cs.SD"])
UA_APP = "arxiv-radar/1.0 (+https://github.com/haydarsahin0/Jev)"
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
ACCEPT_XML = "application/atom+xml,text/xml;q=0.9,*/*;q=0.8"


def url(host, encoded=True, simple=False):
    q = "cat:cs.AI" if simple else SEARCH
    params = {"search_query": q, "start": 0, "max_results": 3,
              "sortBy": "submittedDate", "sortOrder": "descending"}
    if encoded:
        qs = urllib.parse.urlencode(params)
    else:
        qs = "&".join("%s=%s" % (k, str(v).replace(" ", "+")) for k, v in params.items())
    return "%s/api/query?%s" % (host, qs)


def probe(name, target, headers):
    req = urllib.request.Request(target, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read(400).decode("utf-8", "replace")
            print("  OK   %-42s %s  |  %s" % (name, r.status, body[:70].replace("\n", " ")))
            return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(200).decode("utf-8", "replace").replace("\n", " ")[:90]
        except Exception:
            pass
        print("  FAIL %-42s HTTP %s %s  |  %s" % (name, e.code, e.reason, detail))
    except Exception as e:
        print("  FAIL %-42s %s: %s" % (name, type(e).__name__, e))
    return False


HTTPS = "https://export.arxiv.org"
HTTP = "http://export.arxiv.org"

print("=== 1. Baslik varyantlari (https, urlencode) ===")
probe("basliksiz (urllib varsayilani)", url(HTTPS), {})
probe("sadece UA (uygulama)", url(HTTPS), {"User-Agent": UA_APP})
probe("sadece Accept", url(HTTPS), {"Accept": ACCEPT_XML})
probe("UA + Accept", url(HTTPS), {"User-Agent": UA_APP, "Accept": ACCEPT_XML})
probe("UA + Accept: */*", url(HTTPS), {"User-Agent": UA_APP, "Accept": "*/*"})
probe("tarayici UA + Accept", url(HTTPS), {"User-Agent": UA_BROWSER, "Accept": ACCEPT_XML})
probe("tarayici UA tek basina", url(HTTPS), {"User-Agent": UA_BROWSER})

print("=== 2. Accept-Encoding ===")
probe("UA + Accept + AE:identity", url(HTTPS),
      {"User-Agent": UA_APP, "Accept": ACCEPT_XML, "Accept-Encoding": "identity"})
probe("UA + Accept + AE:gzip", url(HTTPS),
      {"User-Agent": UA_APP, "Accept": ACCEPT_XML, "Accept-Encoding": "gzip, deflate"})

print("=== 3. http vs https ===")
probe("http + UA + Accept", url(HTTP), {"User-Agent": UA_APP, "Accept": ACCEPT_XML})

print("=== 4. Sorgu kodlamasi ===")
probe("kodlanmamis (bosluk -> +)", url(HTTPS, encoded=False),
      {"User-Agent": UA_APP, "Accept": ACCEPT_XML})
probe("tek kategori (basit sorgu)", url(HTTPS, simple=True),
      {"User-Agent": UA_APP, "Accept": ACCEPT_XML})
probe("tek kategori, basliksiz", url(HTTPS, simple=True), {})

print("=== 5. Eski RSS ucu (karsilastirma) ===")
probe("export.arxiv.org/rss/cs.AI", "https://export.arxiv.org/rss/cs.AI",
      {"User-Agent": UA_APP, "Accept": ACCEPT_XML})
