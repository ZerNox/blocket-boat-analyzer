// Bookmarklet: run on a Facebook Marketplace page you are viewing. It saves what is on screen —
// the listing cards of a search page, or the full text of one listing — as a JSON file for
// `boats import-marketplace`. Nothing is fetched: it only reads the page you already have open.
(() => {
  const now = new Date().toISOString();
  const idOf = (href) => (href.match(/\/marketplace\/item\/(\d+)/) || [])[1];
  const lines = (el) => el.innerText.split("\n").map((s) => s.trim()).filter(Boolean);
  const isPrice = (s) => /^(?:gratis|free|\d[\d\s .,]*\s*(?:kr|sek|:-)?)$/i.test(s) && /\d|gratis|free/i.test(s);
  const items = [];
  const itemId = idOf(location.pathname);
  if (itemId) {
    // One listing: keep the whole main column; the importer finds title, price and description.
    const main = document.querySelector('[role="main"]') || document.body;
    const text = main.innerText.slice(0, 20000);
    const ls = lines(main);
    const title = (document.querySelector("h1") || {}).innerText || ls[0];
    items.push({ id: itemId, url: `https://www.facebook.com/marketplace/item/${itemId}/`, title, price: ls.find(isPrice) || null, text, saved: now });
  } else {
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
      const id = idOf(a.getAttribute("href"));
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const ls = lines(a);
      const prices = ls.filter(isPrice);
      const rest = ls.filter((s) => !isPrice(s));
      items.push({ id, url: `https://www.facebook.com/marketplace/item/${id}/`, price: prices[0] || null, title: rest[0] || "", location: rest[1] || "",
                   image: (a.querySelector("img") || {}).src || null, query: new URLSearchParams(location.search).get("query"), saved: now });
    }
  }
  if (!items.length) { alert("Hittade inga Marketplace-annonser på sidan. Scrolla så att annonserna laddas och försök igen."); return; }
  const blob = new Blob([JSON.stringify({ source: "facebook-marketplace", page: location.href, saved: now, items }, null, 1)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `marketplace-${itemId || "sok"}-${now.slice(0, 19).replace(/[:T]/g, "-")}.json`;
  document.body.appendChild(link); link.click(); link.remove();
  alert(`Sparade ${items.length} annons${items.length > 1 ? "er" : ""}.`);
})();
