// Tiny dependency-free web server for the toy-shop fixture app.
// Flakiness is intentional and randomized per request:
//   - the submit button id rotates between a well-known id and a random one
//     (a stable data-testid="submit" is always present)
//   - a banner flips to "Ready" after a random client-side delay (no network)
//   - /api/items responds after a random delay (network race)
const http = require('http');
const crypto = require('crypto');

const PORT = process.env.PORT || 4173;

function page() {
  const submitId =
    Math.random() < 0.5 ? 'btn-1f9c' : 'btn-' + crypto.randomBytes(2).toString('hex');
  const bannerDelay = 200 + Math.floor(Math.random() * 2800); // 200..3000 ms
  return `<!doctype html>
<html>
<head><meta charset="utf-8"><title>Toy Shop</title></head>
<body>
  <h1>Toy Shop</h1>
  <button id="${submitId}" data-testid="submit">Submit order</button>
  <div id="submit-result"></div>
  <div id="delayed-banner" data-testid="banner">Loading…</div>
  <button id="load-items" data-testid="load-items">Load items</button>
  <div id="item-count" data-testid="item-count">–</div>
  <script>
    document.querySelector('[data-testid="submit"]').addEventListener('click', () => {
      document.getElementById('submit-result').textContent = 'submitted';
    });
    setTimeout(() => {
      document.getElementById('delayed-banner').textContent = 'Ready';
    }, ${bannerDelay});
    document.getElementById('load-items').addEventListener('click', async () => {
      const res = await fetch('/api/items');
      const items = await res.json();
      document.getElementById('item-count').textContent = items.length + ' items';
    });
  </script>
</body>
</html>`;
}

const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/items')) {
    const delay = Math.floor(Math.random() * 2500); // 0..2500 ms
    setTimeout(() => {
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify(['truck', 'ball', 'kite']));
    }, delay);
    return;
  }
  res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store' });
  res.end(page());
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`toy-shop listening on http://127.0.0.1:${PORT}`);
});
