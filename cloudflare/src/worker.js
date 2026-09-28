// Club Watcher, Cloudflare edition: checks the Fixr pages where the house's
// nights are posted every minute, around the clock, and sends phone alerts.
// Alert wording matches club_watcher.py exactly, so the laptop, GitHub and
// this copy can tell when one of the others has already sent an alert.

// every: check this page every N minutes. The free plan allows ~10ms of CPU a
// run and each request costs ~1ms, so the pages where the house's nights never
// appear are checked less often.
const SOURCES = [
  { club: "Timepiece", url: "https://fixr.co/organiser/timepiece", every: 1, pick: (pp) => pp.data.data },
  { club: "Timepiece", url: "https://fixr.co/venue/2783", every: 1, pick: (pp) => pp.venue.events },
  { club: "Fever", url: "https://fixr.co/venue/28528", every: 1, pick: (pp) => pp.venue.events },
  { club: "Cavern", url: "https://fixr.co/venue/1107", every: 1, pick: (pp) => pp.venue.events },
  { club: "Fever", url: "https://fixr.co/venue/2379", every: 5, pick: (pp) => pp.venue.events },
];

// Ticket-by-ticket details cost a request per event, so they're fetched every
// minute only for house nights and brand-new events; other events every
// DETAIL_EVERY minutes, or sooner if the page summary shows a change.
const DETAIL_EVERY = 5;

// Keep in sync with MAIN_NIGHTS in club_watcher.py
const MAIN_NIGHTS = [
  ["Fever", "Mon", ["LOGIC"], "LOGIC"],
  ["Cavern", "Tue", ["CAVERN TUESDAY"], "Cavern Tuesday"],
  ["Timepiece", "Wed", ["LEGENDS", "AU WEDNESDAY"], "TP Wednesday"],
  ["Timepiece", "Fri", ["SKETCH"], "SKETCH"],
  ["Timepiece", "Sat", ["SATURDAY"], "TP Saturday"],
];

const CATCH_UP_AFTER_HOURS = 6;
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36";

// ------------------------------------------------------------------ helpers --

async function fetchText(url, accept = "text/html") {
  const r = await fetch(url, {
    headers: { "User-Agent": UA, Accept: accept, "Accept-Language": "en-GB" },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  return r.text();
}

function nextData(page) {
  const m = page.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("page layout changed (no __NEXT_DATA__)");
  return JSON.parse(m[1]).props.pageProps;
}

// UK time, formatted exactly like Python's "%a %d %b %H:%M" (so "Sep", not "Sept")
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function ukParts(unixSecs) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", year: "numeric", month: "numeric", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(unixSecs * 1000));
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
  const day = DAYS[new Date(Date.UTC(+p.year, +p.month - 1, +p.day)).getUTCDay()];
  return { day, when: `${day} ${p.day} ${MONTHS[+p.month - 1]} ${p.hour}:${p.minute}` };
}

function mainNight(club, ev) {
  const name = ev.name.toUpperCase();
  for (const [nClub, day, words, label] of MAIN_NIGHTS) {
    if (nClub === club && (ev.day === day || words.some((w) => name.includes(w)))) return label;
  }
  return null;
}

// ---------------------------------------------------------------- scraping --

async function eventTickets(id) {
  const d = JSON.parse(
    await fetchText(`https://api.fixr.co/api/v2/app/event/${id}`, "application/json"));
  const tickets = {};
  for (const t of d.tickets || []) {
    const status = t.expired ? "off_sale" : t.sold_out ? "sold_out"
      : t.not_yet_valid ? "not_yet" : "available";
    const name = t.name + (t.promo_code_required ? " (rep code needed)" : "");
    tickets[String(t.id)] = { name, status };
  }
  return tickets;
}

async function scrapeAll(log, minute, known) {
  const events = {}; // "fixr:<id>" -> event (first source to list it names the club)
  for (const src of SOURCES) {
    if (minute % src.every) continue;
    try {
      const raw = src.pick(nextData(await fetchText(src.url)));
      for (const e of raw) {
        const key = `fixr:${e.id}`;
        if (events[key]) continue;
        const { day, when } = ukParts(e.openTime);
        const ev = {
          club: src.club, name: e.name, when, day, openTime: e.openTime,
          url: `https://fixr.co/event/${e.routingPart}`,
          sig: `${e.soldOut}|${e.groupIsSoldOut}|${e.cheapestTicket?.id}|${e.cheapestTicket?.price}`,
        };
        const prev = known[key];
        // Each event gets its turn in a different minute, spreading the load
        const fresh = !prev || mainNight(ev.club, ev) ||
          (minute + e.id) % DETAIL_EVERY === 0 || prev.sig !== ev.sig;
        ev.tickets = fresh ? await eventTickets(e.id) : prev.tickets;
        events[key] = ev;
      }
    } catch (ex) {
      log(`couldn't check ${src.club} (${src.url}): ${ex.message}`);
    }
  }
  return events;
}

// ----------------------------------------------------------- notifications --

async function alreadySent(topic, title, message) {
  try {
    const history = await fetchText(`https://ntfy.sh/${topic}/json?poll=1&since=12h`,
      "application/json");
    return history.split("\n").filter(Boolean).map((l) => JSON.parse(l))
      .some((m) => m.title === title && m.message === message);
  } catch {
    return false;
  }
}

async function notify(env, log, title, message, url, main) {
  title = title.replace(/[^\x00-\x7F]/g, "");
  log(`ALERT ${main ? "[MAIN NIGHT] " : ""}${title} - ${message}`);
  if (!env.NTFY_TOPIC) return;
  if (await alreadySent(env.NTFY_TOPIC, title, message)) {
    log("  (already sent by another watcher)");
    return;
  }
  const headers = {
    Title: title, Priority: main ? "urgent" : "default",
    Tags: main ? "star,tickets" : "tickets",
  };
  if (url) {
    headers.Click = url;
    headers.Actions = `view, Get tickets, ${url}`;
  }
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, { method: "POST", headers, body: message });
}

// ------------------------------------------------------------- change check --

async function compare(env, log, old, now, quiet) {
  for (const [key, ev] of Object.entries(now)) {
    const prev = old[key];
    const label = mainNight(ev.club, ev);
    const main = label !== null;
    const who = label ? `${label} (${ev.club} ${ev.day})` : ev.club;
    const tickets = Object.values(ev.tickets);

    if (!prev) {
      if (quiet) continue;
      const onSale = tickets.filter((t) => t.status === "available");
      await notify(env, log, `${who}: new event!`,
        `${ev.name} (${ev.when})` + (onSale.length
          ? ` - ${onSale.length} ticket type(s) on sale NOW`
          : " - not on sale yet, you'll get another alert when it is"),
        ev.url, main);
      continue;
    }

    const live = [], back = [];
    for (const [tid, t] of Object.entries(ev.tickets)) {
      const before = prev.tickets[tid]?.status;
      if (t.status === before || t.status !== "available" || quiet) continue;
      (before === "sold_out" ? back : live).push(t.name);
    }
    for (const [names, what] of [[live, "live"], [back, "back"]]) {
      if (names.length) {
        await notify(env, log, `${who}: tickets ${what}!`,
          `${ev.name} (${ev.when}) - ${names.join("; ")}`, ev.url, main);
      }
    }
  }
}

async function run(env, scheduledTime) {
  const lines = [];
  const log = (m) => { lines.push(m); console.log(m); };

  const saved = JSON.parse((await env.STATE.get("state")) || "null");
  const minute = Math.floor(scheduledTime / 60000);
  const events = await scrapeAll(log, minute, saved?.events || {});
  if (!Object.keys(events).length) return lines; // every source failed; try next minute

  const nowSecs = Date.now() / 1000;
  if (!saved) {
    log(`started watching: ${Object.keys(events).length} upcoming event(s)`);
  } else {
    const stale = nowSecs - saved.lastCheck > CATCH_UP_AFTER_HOURS * 3600;
    if (stale) log("last check was a long time ago - catching up quietly");
    await compare(env, log, saved.events, events, stale);
  }

  // Keep events that vanished briefly (so a relist isn't "new"), until a day after they start
  const merged = { ...(saved?.events || {}) };
  for (const [k, v] of Object.entries(merged)) {
    if (!events[k] && v.openTime < nowSecs - 86400) delete merged[k];
  }
  Object.assign(merged, events);

  // KV allows 1,000 writes a day on the free plan, so only save when something
  // changed, or every 3 hours so the catch-up check knows we're still running
  const changed = JSON.stringify(merged) !== JSON.stringify(saved?.events);
  if (changed || !saved || nowSecs - saved.lastCheck > 3 * 3600) {
    await env.STATE.put("state", JSON.stringify({ events: merged, lastCheck: nowSecs }));
  }
  return lines;
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(run(env, controller.scheduledTime));
  },
};
