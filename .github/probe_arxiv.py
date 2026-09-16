"""arXiv 406: hangi COKLU KATEGORI sorgu bicimi calisiyor?  (gecici dosya)"""
import time, urllib.parse, urllib.request, urllib.error

UA = "arxiv-radar/1.0 (+https://github.com/haydarsahin0/Jev)"
HDRS = {"User-Agent": UA, "Accept": "application/atom+xml,text/xml;q=0.9,*/*;q=0.8"}
BASE = "https://export.arxiv.org/api/query"


def run(name, qs):
    target = BASE + "?" + qs
    req = urllib.request.Request(target, headers=dict(HDRS))
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
            n = body.count("<entry>")
            print("  OK   %-46s %s  entry=%d" % (name, r.status, n))
            return True
    except urllib.error.HTTPError as e:
        print("  FAIL %-46s HTTP %s %s" % (name, e.code, e.reason))
    except Exception as e:
        print("  FAIL %-46s %s: %s" % (name, type(e).__name__, e))
    finally:
        time.sleep(3)          # arXiv'e nazik ol
    return False


SORT = "&start=0&max_results=5&sortBy=submittedDate&sortOrder=descending"

print("=== A. OR'un kendisi mi sorun? (siralamali) ===")
run("tek kategori", "search_query=cat:cs.AI" + SORT)
run("iki kategori, + ile OR", "search_query=cat:cs.AI+OR+cat:cs.CL" + SORT)
run("iki kategori, %20 ile OR", "search_query=cat:cs.AI%20OR%20cat:cs.CL" + SORT)
run("iki kategori, parantezli", "search_query=%28cat:cs.AI+OR+cat:cs.CL%29" + SORT)
run("dort kategori, + ile OR",
    "search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG+OR+cat:cs.SD" + SORT)

print("=== B. Siralama olmadan ===")
run("iki kategori, siralamasiz", "search_query=cat:cs.AI+OR+cat:cs.CL&start=0&max_results=5")
run("dort kategori, siralamasiz",
    "search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG+OR+cat:cs.SD&start=0&max_results=5")

print("=== C. Tek kategori + siralama + buyuk max_results ===")
run("tek kategori, max_results=100", "search_query=cat:cs.AI&start=0&max_results=100"
    "&sortBy=submittedDate&sortOrder=descending")
run("tek kategori, max_results=300", "search_query=cat:cs.AI&start=0&max_results=300"
    "&sortBy=submittedDate&sortOrder=descending")

print("=== D. Alternatif alan sozdizimi ===")
run("cat:cs.* joker", "search_query=cat:cs.A*" + SORT)
run("terms yerine abs", "search_query=abs:transformer" + SORT)

print("=== E. Her kategori ayri istek (yedek plan) ===")
ok = 0
for c in ["cs.AI", "cs.CL", "cs.LG", "cs.SD"]:
    if run("ayri istek " + c, "search_query=cat:" + c + "&start=0&max_results=100"
           "&sortBy=submittedDate&sortOrder=descending"):
        ok += 1
print("  -> ayri isteklerle basarili kategori: %d/4" % ok)
