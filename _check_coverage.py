from auth_manager import get_stored_cookies, get_username
import requests
from bs4 import BeautifulSoup
import kb_client

username = get_username()
data = get_stored_cookies(username)
cookies_list = data.get('cookies', [])
jar = requests.cookies.RequestsCookieJar()
for c in cookies_list:
    jar.set(c['name'], c['value'], domain=c.get('domain', '.kb.netapp.com'), path=c.get('path', '/'))

session = requests.Session()
session.cookies = jar
session.headers.update({'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html,*/*'})

print('%-50s  %5s  %9s' % ('Category Path', 'HTTP', 'Articles'))
print('-' * 70)
grand_total = 0
for path in kb_client.KB_CATEGORY_BROWSE_PATHS:
    url = 'https://kb.netapp.com' + path
    resp = session.get(url, timeout=30)
    soup = BeautifulSoup(resp.text, 'html.parser')
    count = len(set(
        a['href'] for a in soup.find_all('a', href=True)
        if any(m in a.get('href','') for m in kb_client.ARTICLE_PATH_MARKERS)
    ))
    grand_total += count
    status = 'OK' if resp.status_code == 200 else str(resp.status_code)
    print('%-50s  %5s  %9d' % (path, status, count))

print('-' * 70)
print('%-50s  %5s  %9d' % ('GRAND TOTAL', '', grand_total))
