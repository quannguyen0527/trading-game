// Browser client: connects over a WebSocket and redraws everything from each state message.
// The server is the source of truth; this file only renders what it sends.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const fmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (cents) => (cents < 0 ? "-$" : "$") + fmt.format(Math.abs(cents) / 100);
const signedMoney = (cents) => (cents > 0 ? "+" : "") + money(cents);
const pct = (x) => (x > 0 ? "+" : "") + x.toFixed(2) + "%";
const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const isBot = (name) => name.startsWith("[bot]");
const trend = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

const STARTING_PRICE = 100_00;

let ws;
let state = null;        // last {public, you} from the server
let roundEndsAt = 0;     // local time the round ends, from the server's seconds_left
let lastShownPrice = null;
let newsSeen = null;
let coachRound = 0;       // round the coach panel currently shows // how many headlines were already on screen; null before the first render

// ---------- connection ----------

$("join").onsubmit = (e) => {
  e.preventDefault();
  const room = $("room").value.trim();
  const name = $("name").value.trim();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(room)}?name=${encodeURIComponent(name)}`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "error") toast(msg.message);
    if (msg.type === "state") render(msg);
    if (msg.type === "coach") showCoach(msg);
  };
  ws.onclose = () => {
    if (state) $("banner").textContent = "Disconnected from the server. Refresh the page to rejoin with the same name.";
  };
};

const send = (msg) => ws && ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(msg));

$("start").onclick = () => send({ type: "start" });

$("coach-ask").onclick = () => {
  send({ type: "coach" });
  $("coach-ask").hidden = true;
  setCoachText("Reviewing your trades", "thinking");
};

document.querySelectorAll("#order [data-side]").forEach((btn) => {
  btn.onclick = () => {
    const price = $("price").value;
    send({
      type: "order",
      side: btn.dataset.side,
      qty: parseInt($("qty").value, 10),
      price: price === "" ? null : Math.round(parseFloat(price) * 100),
    });
  };
});
$("order").onsubmit = (e) => e.preventDefault();

// Clicks on table cells are handled here, so re-rendering the tables doesn't drop handlers.
$("book").onclick = (e) => {
  const row = e.target.closest("tr[data-price]");
  if (row) $("price").value = (row.dataset.price / 100).toFixed(2);
};
$("mine").onclick = (e) => {
  const btn = e.target.closest("button[data-id]");
  if (btn) send({ type: "cancel", order_id: Number(btn.dataset.id) });
};

// ---------- rendering ----------

function render(msg) {
  state = msg;
  const { public: pub, you } = msg;
  $("join-screen").hidden = true;
  $("game").hidden = false;
  $("room-name").textContent = `Room: ${pub.room}`;

  renderRound(pub, you);
  renderCoach(pub);
  renderQuote(pub);
  renderPosition(you);
  renderBook(pub.book);
  renderNews(pub.news);
  renderLeaders(pub.leaderboard, you.name);
  renderOpenOrders(you.open_orders);
  renderTrades(pub.trades);
  drawChart();
}

function renderRound(pub, you) {
  const running = pub.phase === "running";
  roundEndsAt = running ? Date.now() + pub.seconds_left * 1000 : 0;
  tickClock();
  $("start").hidden = running;
  $("start").textContent = pub.phase === "finished" ? "Play again" : "Start round";
  $("order").classList.toggle("closed", !running);
  $("fair-key").hidden = pub.fair_history === null;

  const banner = $("banner");
  banner.classList.toggle("result", pub.phase === "finished");
  if (pub.phase === "waiting") {
    banner.textContent = "Press Start round when everyone has joined. Headlines move a hidden fair value, and shares are settled at that value when the clock hits zero.";
  } else if (running) {
    banner.textContent = "";
  } else {
    const rank = pub.leaderboard.findIndex((r) => r.name === you.name) + 1;
    banner.innerHTML =
      `<b>Round over.</b> Fair value was <b>${money(pub.fair_value)}</b>; every share was settled at that price. ` +
      `You finished <b>#${rank} of ${pub.leaderboard.length}</b> with ${money(you.net_worth)} ` +
      `(<span class="${trend(you.pnl)}">${signedMoney(you.pnl)}</span>).`;
  }
}

function renderQuote(pub) {
  const el = $("last");
  el.textContent = money(pub.last_price);
  if (lastShownPrice !== null && pub.last_price !== lastShownPrice) {
    // Flash green or red, then fade back (the fade is a CSS transition).
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(pub.last_price > lastShownPrice ? "flash-up" : "flash-down");
    setTimeout(() => el.classList.remove("flash-up", "flash-down"), 50);
  }
  lastShownPrice = pub.last_price;

  const change = ((pub.last_price - STARTING_PRICE) / STARTING_PRICE) * 100;
  $("change").textContent = pct(change);
  $("change").className = "change " + trend(change);
}

function renderPosition(you) {
  $("networth").textContent = money(you.net_worth);
  $("pnl").textContent = signedMoney(you.pnl);
  $("pnl").className = "change " + trend(you.pnl);
  $("cash").textContent = money(you.cash);
  $("shares").textContent = you.shares;
  $("power").textContent = money(you.buying_power);
  $("sellable").textContent = you.sellable_shares;
}

function renderBook(book) {
  const maxQty = Math.max(1, ...book.bids.map((l) => l.qty), ...book.asks.map((l) => l.qty));
  const row = (l, cls) =>
    `<tr class="level ${cls}" data-price="${l.price}" style="--depth:${(l.qty / maxQty) * 100}%">` +
    `<td>${money(l.price)}</td><td>${l.qty}</td></tr>`;

  const bestBid = book.bids[0], bestAsk = book.asks[0];
  const spread = bestBid && bestAsk ? `Spread ${money(bestAsk.price - bestBid.price)}`
    : state.public.phase !== "running" ? "Market closed"
    : bestBid ? "No sellers right now"
    : bestAsk ? "No buyers right now"
    : "Empty book";

  $("book").innerHTML =
    "<tr><th>Price</th><th>Qty</th></tr>" +
    book.asks.slice().reverse().map((l) => row(l, "ask")).join("") +
    `<tr class="spread"><td colspan="2">${spread}</td></tr>` +
    book.bids.map((l) => row(l, "bid")).join("");
}

function renderNews(news) {
  if (newsSeen === null) newsSeen = news.length; // don't flash old headlines on page load
  if (news.length < newsSeen) newsSeen = 0;      // a new round started
  $("news").innerHTML = news.length === 0
    ? `<li class="muted">No news yet. Headlines arrive every 15–30 seconds.</li>`
    : news.map((n, i) => {
        const impact = n.change_pct === null ? ""
          : `<span class="impact ${trend(n.change_pct)}">${n.change_pct > 0 ? "+" : ""}${n.change_pct}%</span>`;
        return `<li class="${i >= newsSeen ? "fresh" : ""}"><span class="time">${mmss(n.second)}</span>${esc(n.headline)}${impact}</li>`;
      }).reverse().join("");
  newsSeen = news.length;
}

function renderLeaders(rows, me) {
  const start = 10_000_00 + 100 * STARTING_PRICE;
  $("leaders").innerHTML = rows.map((r, i) => {
    const change = ((r.net_worth - start) / start) * 100;
    const name = isBot(r.name)
      ? `${esc(r.name.replace("[bot] ", ""))}<span class="bot-tag">BOT</span>`
      : esc(r.name);
    return `<tr class="${r.name === me ? "me" : ""}"><td class="rank">${i + 1}</td><td>${name}</td>` +
      `<td>${money(r.net_worth)}</td><td class="${trend(change)}">${pct(change)}</td></tr>`;
  }).join("");
}

function renderOpenOrders(orders) {
  $("mine").innerHTML = orders.length === 0
    ? `<tr><td class="empty">No open orders.</td></tr>`
    : orders.map((o) =>
        `<tr><td class="${o.side === "buy" ? "up" : "down"}">${o.side.toUpperCase()}</td>` +
        `<td>${o.remaining} @ ${money(o.price)}</td>` +
        `<td><button class="link" data-id="${o.id}">Cancel</button></td></tr>`).join("");
}

function renderTrades(trades) {
  const label = (n) => esc(isBot(n) ? n.replace("[bot] ", "🤖 ") : n);
  $("trades").innerHTML = trades.length === 0
    ? `<tr><td class="empty">No trades yet.</td></tr>`
    : trades.slice().reverse().map((t) =>
        `<tr><td>${label(t.buyer)} <span class="muted">bought from</span> ${label(t.seller)}</td>` +
        `<td>${t.qty}</td><td>${money(t.price)}</td></tr>`).join("");
}

// ---------- AI coach ----------

function renderCoach(pub) {
  $("coach").hidden = !(pub.coach_enabled && pub.phase === "finished");
  if (pub.round_number !== coachRound) {
    // A different round: clear the previous review.
    coachRound = pub.round_number;
    $("coach-ask").hidden = false;
    $("coach-text").hidden = true;
  }
}

function showCoach(msg) {
  if (msg.round !== coachRound) return; // a late reply about an earlier round
  if (msg.error) {
    setCoachText(msg.error.charAt(0).toUpperCase() + msg.error.slice(1) + ".", "error");
    $("coach-ask").hidden = false;
  } else {
    setCoachText(msg.text, "");
  }
}

function setCoachText(text, cls) {
  const el = $("coach-text");
  el.textContent = text; // plain text only: the reply is never treated as HTML
  el.className = "coach-text " + cls;
  el.hidden = false;
}

// ---------- clock ----------

function tickClock() {
  const clock = $("clock");
  if (!roundEndsAt) {
    clock.textContent = state && state.public.phase === "finished" ? "0:00" : mmss(state ? state.public.round_seconds : 180);
    clock.classList.remove("urgent");
    return;
  }
  const s = Math.max(0, Math.ceil((roundEndsAt - Date.now()) / 1000));
  clock.textContent = mmss(s);
  clock.classList.toggle("urgent", s <= 10);
}
setInterval(tickClock, 250);

// ---------- toast ----------

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
}

// ---------- price chart (plain canvas, no library) ----------

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function drawChart() {
  const canvas = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  // Draw at the screen's real pixel density so lines stay sharp on retina displays.
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!state) return;

  const pub = state.public;
  const prices = pub.price_history;
  const fair = pub.fair_history || [];
  const colors = { grid: css("--border"), text: css("--muted"), line: css("--accent"), fair: css("--fair"), up: css("--up"), down: css("--down") };
  const pad = { l: 8, r: 62, t: 14, b: 24 };
  const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;

  // Y range: fit every visible value, at least ±2% around the middle so tiny moves don't look huge.
  const values = prices.map((p) => p[1]).concat(fair.map((p) => p[1]));
  if (values.length === 0) values.push(pub.last_price);
  const lo0 = Math.min(...values), hi0 = Math.max(...values);
  const mid = (lo0 + hi0) / 2;
  const half = Math.max((hi0 - lo0) / 2, mid * 0.02) * 1.15;
  const lo = mid - half, hi = mid + half;
  const x = (s) => pad.l + (s / pub.round_seconds) * plotW;
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * plotH;

  ctx.font = "11px system-ui, sans-serif";
  ctx.lineWidth = 1;

  // Horizontal grid lines with price labels on the right.
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    ctx.strokeStyle = colors.grid;
    ctx.beginPath(); ctx.moveTo(pad.l, y(v)); ctx.lineTo(pad.l + plotW, y(v)); ctx.stroke();
    ctx.fillStyle = colors.text;
    ctx.fillText(money(Math.round(v)), pad.l + plotW + 8, y(v));
  }
  // Time labels along the bottom, one per minute.
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let s = 0; s <= pub.round_seconds; s += 60) {
    ctx.textAlign = s === 0 ? "left" : "center"; // keep 0:00 from being cut off at the edge
    ctx.fillText(mmss(s), x(s), pad.t + plotH + 8);
  }

  // A dashed vertical line for each headline, colored once its impact is revealed.
  ctx.setLineDash([3, 4]);
  for (const n of pub.news) {
    ctx.strokeStyle = n.change_pct === null ? colors.text : n.change_pct > 0 ? colors.up : colors.down;
    ctx.globalAlpha = 0.6;
    ctx.beginPath(); ctx.moveTo(x(n.second), pad.t); ctx.lineTo(x(n.second), pad.t + plotH); ctx.stroke();
  }
  ctx.globalAlpha = 1;

  if (prices.length === 0) {
    ctx.setLineDash([]);
    ctx.fillStyle = colors.text;
    ctx.textBaseline = "middle";
    ctx.font = "14px system-ui, sans-serif";
    ctx.fillText("The chart starts when the round does.", pad.l + plotW / 2, pad.t + plotH / 2);
    return;
  }

  // Fair value (revealed after the round): a step line, since it only jumps on news.
  if (fair.length) {
    ctx.strokeStyle = colors.fair;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(x(fair[0][0]), y(fair[0][1]));
    for (let i = 1; i < fair.length; i++) {
      ctx.lineTo(x(fair[i][0]), y(fair[i - 1][1]));
      ctx.lineTo(x(fair[i][0]), y(fair[i][1]));
    }
    ctx.lineTo(x(pub.round_seconds), y(fair[fair.length - 1][1]));
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // Last-trade price: a line with a soft fill underneath.
  const path = new Path2D();
  prices.forEach(([s, v], i) => (i ? path.lineTo(x(s), y(v)) : path.moveTo(x(s), y(v))));
  const last = prices[prices.length - 1];

  const fill = new Path2D(path);
  fill.lineTo(x(last[0]), pad.t + plotH);
  fill.lineTo(x(prices[0][0]), pad.t + plotH);
  fill.closePath();
  const gradient = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  gradient.addColorStop(0, colors.line + "55");
  gradient.addColorStop(1, colors.line + "00");
  ctx.fillStyle = gradient;
  ctx.fill(fill);

  ctx.strokeStyle = colors.line;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke(path);

  // Current price: a dot and a tag on the price axis.
  const ly = y(last[1]);
  ctx.fillStyle = colors.line;
  ctx.beginPath(); ctx.arc(x(last[0]), ly, 3.5, 0, Math.PI * 2); ctx.fill();
  ctx.fillRect(pad.l + plotW + 2, ly - 9, pad.r - 4, 18);
  ctx.fillStyle = "#fff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.font = "600 11px system-ui, sans-serif";
  ctx.fillText(money(last[1]), pad.l + plotW + 6, ly);
}

new ResizeObserver(drawChart).observe($("chart"));
