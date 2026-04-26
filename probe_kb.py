import requests
from bs4 import BeautifulSoup
from auth_manager import get_stored_cookies, get_username

username = get_username()
data = get_stored_cookies(username)
cookies_list = data.get("cookies", [])

jar = requests.cookies.RequestsCookieJar()
for c in cookies_list:
    jar.set(c["name"], c["value"], domain=c.get("domain", ".kb.netapp.com"), path=c.get("path", "/"))

session = requests.Session()
session.cookies = jar
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": "https://kb.netapp.com/",
})

url = "https://kb.netapp.com/on-prem/ontap/Perf/Perf-KBs/How_to_troubleshoot_FlexGroup_performance_issues"
resp = session.get(url, timeout=30)
soup = BeautifulSoup(resp.text, "html.parser")

# Find the container with the most FlexGroup mentions
best = None
best_count = 0
for tag in soup.find_all(["div", "section", "article"]):
    cnt = tag.get_text().count("FlexGroup")
    if cnt > best_count:
        best_count = cnt
        best = tag

if best:
    print("Best container: <%s class=%s id=%s>" % (best.name, best.get("class"), best.get("id")))
    print("FlexGroup count:", best_count)
    print("Text preview:")
    print(best.get_text(separator="\n", strip=True)[:5000])
