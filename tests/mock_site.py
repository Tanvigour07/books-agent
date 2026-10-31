"""Local mock of books.toscrape.com (same markup) for offline testing.
Serves 3 listing pages x 20 books under /catalogue/. Run: python tests/mock_site.py 8765
"""
import random
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

random.seed(7)
CATS = ["Poetry", "Fiction", "Mystery", "Travel", "History", "Humor"]
WORDS = ["Light", "Attic", "Velvet", "Storm", "Garden", "Silent", "Orbit", "Ledger"]
RATINGS = ["One", "Two", "Three", "Four", "Five"]
BOOKS = []
for i in range(60):
    BOOKS.append(dict(
        slug=f"book-{i}_{i + 1}", title=f"{random.choice(WORDS)} {random.choice(WORDS)} {i}",
        price=round(random.uniform(10, 60), 2), rating=random.choice(RATINGS),
        cat=random.choice(CATS), stock=random.randint(1, 22)))


def listing(n):
    items = "".join(
        f'<article class="product_pod"><h3><a href="{b["slug"]}/index.html">{b["title"]}</a></h3></article>'
        for b in BOOKS[(n - 1) * 20: n * 20])
    return f"<html><body><ol>{items}</ol></body></html>"


def product(b):
    return f"""<html><body>
<ul class="breadcrumb"><li><a href="/">Home</a></li><li><a href="/c">Books</a></li>
<li><a href="/c/x">{b['cat']}</a></li><li class="active">{b['title']}</li></ul>
<article class="product_page"><h1>{b['title']}</h1>
<p class="price_color">£{b['price']}</p>
<p class="availability"><i class="icon-ok"></i> In stock ({b['stock']} available)</p>
<p class="star-rating {b['rating']}"></p></article></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        body = None
        if p.startswith("/catalogue/page-"):
            n = int(p.split("page-")[1].split(".")[0])
            body = listing(n) if 1 <= n <= 3 else None
        else:
            for b in BOOKS:
                if p == f"/catalogue/{b['slug']}/index.html":
                    body = product(b)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
