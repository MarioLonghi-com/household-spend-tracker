/**
 * Snap: photograph a receipt, compress it here, send it to the inbox.
 *
 * Compressing on the phone is not optional -- a 4 MB photo over cellular is
 * slow enough that people stop bothering, which is the actual failure mode of
 * every receipt feature. But the obvious way to do it breaks two guarantees
 * the whole design rests on, and both breaks are silent:
 *
 *   1. `canvas.toBlob()` produces a file with no metadata whatsoever. Draw a
 *      photo to a canvas and read it back and the GPS is simply never carried
 *      across -- so the location would be dead on the one page where it is
 *      most likely to exist.
 *
 *   2. The SHA-256 would be of the wrong bytes. The dedupe key is taken over
 *      the *original*, before any re-encode, because an encoder that changes
 *      by one byte between releases otherwise makes every stored receipt stop
 *      matching itself. If the phone re-encodes, the server never sees the
 *      original, and the same photo sent from here and from the desktop
 *      becomes two different receipts.
 *
 * Both are fixed by sending three things instead of one: the compressed JPEG,
 * the hash of the untouched File, and the original's APP1 block. The server
 * then behaves exactly as if it had received the original -- no second code
 * path, and a client that sends neither extra field still works.
 */

/** Matches the server's display copy, so the second pass has nothing to undo. */
const MAX_EDGE = 2000;

/**
 * JPEG, and not WebP, which is the surprising half.
 *
 * WebP is smaller on the wire and is obviously right until you measure the
 * double pass. Phone JPEG q0.90 -> server AVIF q60 reaches the same
 * text-region error as uploading the untouched 3.4 MB original, at 290 KB.
 * WebP arrives having already thrown the glyph detail away and the server
 * cannot put it back -- so the phone that saves the most bandwidth produces
 * the permanently worst receipt, and nothing on screen would ever say so.
 */
const QUALITY = 0.9;
const TYPE = "image/jpeg";

/**
 * How many uploads are in flight at once.
 *
 * Two, and not the desktop's three: here the *encode* is on the phone's main
 * thread and is the slow part, so a third in flight buys nothing and makes
 * the page stop responding to the camera button -- which is the one control
 * that has to stay live while a queue drains.
 *
 * Selecting several at once is the ordinary case, not an edge: a long receipt
 * needs two or three shots, and a wallet emptied on a Sunday is a dozen.
 */
const AT_ONCE = 2;

const DB_NAME = "snap";
const STORE = "pending";
const HOUSEHOLD_KEY = "snap.household";
const GPS_KEY = "snap.gps";
//: Written by the app's Your account panel, read here. The same device and the
//: same origin, so the same store -- this page has no setting of its own,
//: because a person who asked for dark meant the whole app. Keep in step with
//: `APPEARANCE_KEY` in `client/src/lib/appearance.ts`; there is a test.
const APPEARANCE_KEY = "spendtracker.appearance";

const els = {
  house: document.getElementById("house"),
  caret: document.getElementById("caret"),
  switcher: document.getElementById("switcher"),
  shoot: document.getElementById("shoot"),
  file: document.getElementById("file"),
  queue: document.getElementById("queue"),
  summary: document.getElementById("summary"),
  who: document.getElementById("who"),
  where: document.getElementById("where"),
  several: document.getElementById("several"),
  files: document.getElementById("files"),
  landed: document.getElementById("landed"),
  gps: document.getElementById("gps"),
  gpsLabel: document.getElementById("gpslabel"),
  gpsWhy: document.getElementById("gpswhy"),
};

let households = [];
let me = null;
//: Receipts that reached the database, newest first. Kept rather than only
//: counted, because a note can only be written once there is a row to hang it
//: on -- see `landedRender`.
const landed = [];
let chosen = null;
let sent = 0;
let running = 0;
const items = new Map();

// --------------------------------------------------------------------------
// Which household
// --------------------------------------------------------------------------

/**
 * Remembered per device, and shown loudly.
 *
 * A receipt sent to the wrong ledger is exactly the failure the colour
 * palettes exist to prevent. `localStorage` and not a cookie or a column: it
 * is a per-device convenience that can come back empty -- a private tab,
 * cleared site data -- so every read is in a try and the page renders
 * correctly without it. Nothing in the ledger depends on it; the household is
 * sent explicitly on every upload.
 */
function remembered() {
  try {
    return window.localStorage.getItem(HOUSEHOLD_KEY);
  } catch {
    return null;
  }
}

function remember(id) {
  try {
    window.localStorage.setItem(HOUSEHOLD_KEY, id);
  } catch {
    /* a private tab, and the page works without it */
  }
}

/** What the person chose in the app: "light", "dark", or nothing for system. */
function appearance() {
  try {
    const found = window.localStorage.getItem(APPEARANCE_KEY);
    return found === "light" || found === "dark" ? found : "system";
  } catch {
    return "system";
  }
}

/**
 * Put the choice on <html> so the stylesheet above can act on it.
 *
 * Called before the first fetch, for the same reason the app calls it before
 * its first render: after it would mean a frame of the scheme somebody turned
 * off.
 */
function paintScheme() {
  const choice = appearance();
  if (choice === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", choice);
}

/** Which scheme is on screen, once the choice is taken into account. */
function schemeNow() {
  const choice = appearance();
  if (choice !== "system") return choice;
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

// The same lock as `client/src/lib/theme.ts`, which a test holds this to. The
// server validates the accent as hex before storing it; this is the second
// lock, so a server bug, an old row or a tampered response is never pasted
// into the header's style (#92). Anything else leaves the header as it is.
const HEX = /^#[0-9a-fA-F]{6}$/;

function accentOf(house) {
  const scheme = house.colours && house.colours[schemeNow()];
  const accent = scheme ? scheme.accent : null;
  return typeof accent === "string" && HEX.test(accent) ? accent : null;
}

function paint() {
  // Who, then where. Two households in one browser look identical otherwise,
  // and so do two accounts -- and that is how a receipt lands in the wrong
  // ledger under the wrong name.
  els.who.textContent = me ? me.display_name : "";
  els.where.textContent = chosen.name;
  // Like the app's own tabs (#189); the static <title> stands until here.
  document.title = `Spend Tracker - Snap a Receipt - ${chosen.name}`;
  const accent = accentOf(chosen);
  if (accent) document.querySelector("header").style.background = accent;

  // A control with one option is furniture, so somebody in one household never
  // sees a switcher at all -- and no caret promising one.
  if (households.length < 2) {
    els.house.removeAttribute("aria-haspopup");
    els.caret.hidden = true;
    return;
  }
  els.house.setAttribute("aria-haspopup", "true");
  els.caret.hidden = false;
  els.house.onclick = () => {
    els.switcher.hidden = !els.switcher.hidden;
  };
  els.switcher.replaceChildren(
    ...households.map((one) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = one.name;
      button.onclick = () => {
        chosen = one;
        remember(one.id);
        els.switcher.hidden = true;
        paint();
      };
      return button;
    }),
  );
}

async function start() {
  // Before anything on the network: the toggle has to say which state it is
  // in even on a page that could not reach the server, because it is a
  // statement about what the next photo will record.
  gpsOn = gpsRemembered();
  paintGps();
  paintScheme();

  // The header's accent is read in JavaScript rather than by CSS, so unlike
  // every other colour on this page it does not follow the system on its own.
  // Without this it kept whichever scheme the page was opened in until a
  // reload -- which at a till in the evening is the wrong one.
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (chosen) paint();
    });
  } catch {
    /* an old browser; the accent simply stays as it was */
  }

  let list;
  try {
    list = await fetch("/api/households", { credentials: "same-origin" });
  } catch {
    els.summary.textContent = "No connection. Photograph it anyway and it will send later.";
    return;
  }
  if (list.status === 401) {
    window.location.replace("/?next=%2Fsnap");
    return;
  }
  // Best effort: the page works without a name, and failing to render at all
  // because the greeting could not be fetched would be the wrong trade at a
  // till.
  try {
    const who = await fetch("/api/me", { credentials: "same-origin" });
    if (who.ok) me = await who.json();
  } catch {
    me = null;
  }

  households = await list.json();
  if (households.length === 0) {
    els.summary.textContent = "This account is not in a household yet.";
    els.shoot.disabled = true;
    return;
  }

  // `?h=` pins one, for anyone who would rather install a home-screen icon per
  // household. It is a URL parameter and not a mode: it sets the same stored
  // value a tap does.
  const pinned = new URLSearchParams(window.location.search).get("h");
  if (pinned) remember(pinned);
  const wanted = pinned || remembered();
  chosen = households.find((one) => one.id === wanted) || households[0];
  remember(chosen.id);
  paint();

  await resume();
}

// --------------------------------------------------------------------------
// Where the phone is, when -- and only when -- somebody asks for it
// --------------------------------------------------------------------------

/**
 * The location toggle.
 *
 * Off until pressed. Nothing asks the browser for a position while it is off,
 * and the paragraph under the button says what pressing it will record before
 * it records anything. The coordinate goes to this household's own server on
 * the upload it was taken for, and nowhere else: there is no map tile, no
 * geocoder and no third party anywhere in this path.
 *
 * The choice is remembered per device, like the household and for the same
 * reason -- somebody emptying a wallet at a till should not re-arm it per
 * photo -- and the button says which state it is in every time the page
 * loads. Any refusal turns it back off and forgets it, so a "yes" that the
 * browser no longer honours cannot sit there looking armed.
 *
 * What lands in the database is in `app/services/receipts.py`: the same GPS
 * columns EXIF fills, only where the photograph itself carried none, stamped
 * with where the coordinate came from.
 */
//: Both texts have to name the case that surprises people: this records where
//: the phone is *now*, and it does it for a picture added from the library
//: just as much as for one taken through the shutter -- so a receipt
//: photographed yesterday in another town and uploaded at the kitchen table
//: gets the kitchen table. Saying "each photo" and leaving the reader to
//: work that out is the sort of accuracy that is technically true and
//: practically a surprise.
const OFF_TEXT =
  "Off. A photo records only what the camera wrote. Switch this on and every " +
  "picture you send — taken here, or added from your library — also records " +
  "where this phone is now, asked of the browser when you switch it on and " +
  "sent to this household's own server and nowhere else.";
const ON_TEXT =
  "On. Every picture you send records where this phone is now, to about a " +
  "street — including one taken earlier and added from your library, which " +
  "gets this place rather than the place it was taken. It is used only where " +
  "the picture carries no location of its own.";

//: A fix from the last two minutes is the same till. Asking again per photo
//: costs a second of GPS warm-up for four receipts on the same counter.
const FIX_MAX_AGE_MS = 2 * 60 * 1000;

let gpsOn = false;
let fix = null;

function gpsRemembered() {
  try {
    return window.localStorage.getItem(GPS_KEY) === "on";
  } catch {
    return false;
  }
}

function rememberGps(on) {
  try {
    if (on) window.localStorage.setItem(GPS_KEY, "on");
    else window.localStorage.removeItem(GPS_KEY);
  } catch {
    /* a private tab, and the page works without it */
  }
}

/**
 * Why the browser will not even be asked, when that is already decided.
 *
 * **This is the bug the toggle had.** Geolocation is one of the powerful
 * features a browser only offers on a *secure context* -- HTTPS, or localhost.
 * Reached the way this page actually gets reached on a phone,
 * `http://192.168.1.50:8848`, `navigator.geolocation` is still there and
 * `getCurrentPosition` still exists, so nothing looks wrong: the call is made,
 * no permission prompt appears, and the failure comes back as code 1, which is
 * the same code a person tapping "Don't allow" produces. The page then said
 * the app was probably still sending a blocking `Permissions-Policy` -- which
 * by then it was not -- so the one thing on screen pointed away from the cause
 * and the toggle read as broken rather than as unavailable.
 *
 * So it is checked *first*, before the call, and named. `window.isSecureContext`
 * is exactly the browser's own answer to the question, which is better than
 * inferring it from the scheme: `http://localhost` is secure and
 * `https://` behind a certificate error is not, and neither is something to
 * re-derive here.
 *
 * Returns null when there is nothing in the way and the browser should be asked.
 */
function blocked() {
  if (!navigator.geolocation) {
    return "This browser has no location at all. Nothing was recorded.";
  }
  if (!window.isSecureContext) {
    return (
      "This page is not on a secure connection, so the browser will not offer " +
      "location at all — it never asks, whatever this app sends. Open Snap " +
      "over https (the tailnet address), and the toggle will work. Photographs " +
      "still carry their own location where the camera wrote one."
    );
  }
  return null;
}

function paintGps(why) {
  const cannot = blocked();
  els.gps.setAttribute("aria-pressed", gpsOn ? "true" : "false");
  // Disabled rather than left tappable-and-failing: a control that cannot do
  // its job should say so before it is pressed, not after. The sentence under
  // it is where the reason goes, and it is `aria-describedby` on the button.
  els.gps.disabled = cannot !== null;
  els.gpsLabel.textContent = cannot ? "Location unavailable" : gpsOn ? "Location on" : "Location off";
  els.gpsWhy.textContent = why || cannot || (gpsOn ? ON_TEXT : OFF_TEXT);
}

function position() {
  return new Promise((resolve, reject) => {
    const cannot = blocked();
    if (cannot) {
      reject(new Error(cannot));
      return;
    }
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      timeout: 10000,
      maximumAge: 60000,
    });
  });
}

async function locate() {
  if (fix && Date.now() - fix.at < FIX_MAX_AGE_MS) return fix;
  const spot = await position();
  fix = {
    lat: spot.coords.latitude,
    lon: spot.coords.longitude,
    // Metres, and the same column `GPSHPositioningError` fills. A fix off cell
    // towers is several hundred of them, which is exactly the case the app
    // draws as "approximate" rather than as a doorway.
    accuracy: spot.coords.accuracy,
    at: Date.now(),
  };
  return fix;
}

/**
 * Why it did not work, without inventing a reason.
 *
 * Everything this *can* work out in advance is worked out by `blocked()` and
 * never reaches here. What is left is code 1 from a browser that was genuinely
 * asked and genuinely refused -- the person tapped "Don't allow", or the site
 * is denied location in the browser's own settings from a previous visit.
 *
 * It used to offer "this app is still sending a Permissions-Policy that
 * switches geolocation off" as the other half of that sentence. `/snap` has
 * sent `geolocation=(self)` since the toggle was built, so that half was
 * simply wrong, and it was the half people read.
 */
function refusal(problem) {
  // A fix asked for at the shutter can hit the wall `blocked()` describes --
  // the toggle was switched on at an https address and the page is now being
  // used at a plain-http one, which `localStorage` remembers straight through.
  // Its sentence wins: "could not get a fix just now" would be a third wrong
  // reason in a row.
  const cannot = blocked();
  if (cannot) return cannot;
  return problem && problem.code === 1
    ? "This browser refused. Check that this site is allowed to use your " +
        "location in the browser's own settings for it. Nothing was recorded."
    : "This phone could not get a fix just now. Nothing was recorded.";
}

els.gps.onclick = async () => {
  // Belt and braces: `paintGps` disables the button when `blocked()` has an
  // answer, and a disabled button fires nothing. This is the second half, for
  // a page whose state moved between the paint and the tap.
  if (blocked()) {
    paintGps();
    return;
  }
  if (gpsOn) {
    gpsOn = false;
    fix = null;
    rememberGps(false);
    paintGps();
    return;
  }
  // Asked here, at the tap, which is the moment the permission prompt belongs
  // to -- not at page load, and not silently behind the shutter.
  paintGps("Asking this browser where it is…");
  try {
    await locate();
  } catch (problem) {
    gpsOn = false;
    fix = null;
    rememberGps(false);
    paintGps(refusal(problem));
    return;
  }
  gpsOn = true;
  rememberGps(true);
  paintGps();
};

// --------------------------------------------------------------------------
// The queue, which survives the page
// --------------------------------------------------------------------------

/**
 * Photos go into IndexedDB before the first upload attempt and are removed on
 * a 201, so a tab the OS kills mid-upload -- routine on a phone -- loses
 * nothing. This is the only piece of state on the page and it exists because
 * the alternative is losing a receipt that no longer exists on paper.
 */
function open() {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function withStore(mode, work) {
  let db;
  try {
    db = await open();
  } catch {
    return null; // Private browsing, or storage refused. Uploads still work.
  }
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, mode);
    const result = work(transaction.objectStore(STORE));
    transaction.oncomplete = () => resolve(result && result.result);
    transaction.onerror = () => reject(transaction.error);
  });
}

const park = (record) => withStore("readwrite", (store) => store.put(record));
const unpark = (id) => withStore("readwrite", (store) => store.delete(id));
const parked = () => withStore("readonly", (store) => store.getAll());

/**
 * Whether a parked photo is this person's to send.
 *
 * The queue outlives the session as well as the page, and a phone or a family
 * tablet can change hands in between. Resuming everything uploaded A's photo
 * under B's session -- into A's household, or, when B is not a member, into a
 * 404 retried on every visit with the bytes and any location kept (#198). So
 * a photo carries who took it and only they send it. One parked before the
 * stamp existed has no owner; it goes only to a household this person is in,
 * rather than being stranded.
 */
export function mine(record, who, theirs) {
  if (record.owner) return Boolean(who) && record.owner === who.id;
  return theirs.some((one) => one.id === record.household);
}

async function resume() {
  const waiting = (await parked()) || [];
  for (const record of waiting) {
    if (!mine(record, me, households)) continue;
    items.set(record.id, { ...record, state: "waiting" });
  }
  render();
  pump();
}

// --------------------------------------------------------------------------
// Getting the bytes ready
// --------------------------------------------------------------------------

async function digest(file) {
  // `crypto.subtle` needs a secure context. The deployment is HTTPS over the
  // tailnet, so this works in production and is absent on a plain-HTTP dev
  // box -- where the fallback is to upload the original bytes uncompressed and
  // let the server hash what it actually received, rather than to send a hash
  // of the wrong thing.
  if (!window.crypto || !window.crypto.subtle) return null;
  const buffer = await file.arrayBuffer();
  const hash = await window.crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(hash))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** The original's APP1 segment, base64, for the server to parse. */
async function exifBlock(file) {
  const head = new Uint8Array(await file.slice(0, 128 * 1024).arrayBuffer());
  if (head[0] !== 0xff || head[1] !== 0xd8) return null; // not a JPEG
  let at = 2;
  while (at + 4 < head.length) {
    if (head[at] !== 0xff) return null;
    const marker = head[at + 1];
    const length = (head[at + 2] << 8) | head[at + 3];
    if (marker === 0xe1) {
      const block = head.slice(at + 4, at + 2 + length);
      let binary = "";
      for (const byte of block) binary += String.fromCharCode(byte);
      return window.btoa(binary);
    }
    if (marker === 0xda) return null; // image data starts; no APP1
    at += 2 + length;
  }
  return null;
}

async function shrink(file) {
  if (!file.type.startsWith("image/")) return null;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return null; // A format this browser will not decode. Send it whole.
  }
  const scale = MAX_EDGE / Math.max(bitmap.width, bitmap.height);
  if (scale >= 1) {
    bitmap.close();
    return null; // Already small enough; the original is the better bytes.
  }

  // Two halving passes before the final draw. A single drawImage from 4032px
  // to 2000px aliases badly on most mobile GPUs, and the aliasing lands on
  // exactly the high-frequency edges that are the text.
  let source = bitmap;
  let width = bitmap.width;
  let height = bitmap.height;
  while (width / 2 > MAX_EDGE && height / 2 > MAX_EDGE) {
    width = Math.round(width / 2);
    height = Math.round(height / 2);
    source = await step(source, width, height);
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * (MAX_EDGE / Math.max(width, height)));
  canvas.height = Math.round(height * (MAX_EDGE / Math.max(width, height)));
  const context = canvas.getContext("2d");
  context.imageSmoothingQuality = "high";
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  if (source.close) source.close();

  return new Promise((resolve) => canvas.toBlob(resolve, TYPE, QUALITY));
}

function step(source, width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.imageSmoothingQuality = "high";
  context.drawImage(source, 0, 0, width, height);
  if (source.close) source.close();
  return createImageBitmap(canvas);
}

// --------------------------------------------------------------------------
// Sending
// --------------------------------------------------------------------------

async function take(file) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const [sha, exif] = await Promise.all([digest(file), exifBlock(file)]);
  const smaller = await shrink(file);

  // Read at capture and parked with the photo, not read at send: a queue that
  // drains twenty minutes later on the way home would otherwise stamp every
  // receipt in it with the wrong place. A photo is never held up or dropped
  // for want of a fix -- the location is the optional part.
  let where = null;
  if (gpsOn) {
    try {
      where = await locate();
    } catch (problem) {
      paintGps(refusal(problem));
    }
  }

  const record = {
    id,
    name: file.name || "photo.jpg",
    blob: smaller || file,
    sha,
    exif,
    // Provenance, so a receipt that went through two encoders says so.
    encoded: Boolean(smaller),
    fix: where && { lat: where.lat, lon: where.lon, accuracy: where.accuracy },
    household: chosen.id,
    owner: me ? me.id : null,
  };
  await park(record);
  items.set(id, { ...record, state: "waiting" });
  render();
  pump();
}

function pump() {
  for (const [id, item] of items) {
    if (running >= AT_ONCE) return;
    if (item.state !== "waiting") continue;
    running += 1;
    void send(id, item);
  }
}

async function send(id, item) {
  item.state = "sending";
  render();
  try {
    const form = new FormData();
    form.append("file", item.blob, item.name);
    if (item.sha) form.append("original_sha256", item.sha);
    if (item.exif) form.append("exif", item.exif);
    if (item.encoded) form.append("client_encoded", "true");
    if (item.fix) {
      form.append("device_lat", String(item.fix.lat));
      form.append("device_lon", String(item.fix.lon));
      if (item.fix.accuracy != null) {
        form.append("device_accuracy_m", String(item.fix.accuracy));
      }
    }

    const answer = await fetch(`/api/households/${item.household}/receipts`, {
      method: "POST",
      body: form,
      credentials: "same-origin",
    });
    if (answer.status === 401) {
      window.location.replace("/?next=%2Fsnap");
      return;
    }
    if (!answer.ok) {
      const problem = await answer.json().catch(() => null);
      throw new Error((problem && problem.detail) || `the server said ${answer.status}`);
    }
    const body = await answer.json().catch(() => null);
    await unpark(id);
    items.delete(id);
    sent += 1;
    // Held rather than forgotten. A note can only be written once the row
    // exists, and the moment somebody remembers what the receipt was for is
    // the moment it finishes sending -- not later, at a desk, looking at a
    // thumbnail of a crumpled till slip.
    if (body && body.receipt) {
      land({
        id: body.receipt.id,
        name: item.name,
        note: "",
        saved: "",
        pending: null,
        state: "in",
        // The bytes this page already holds, kept rather than revoked: the
        // thumbnail beside the row and the larger copy under the pointer are
        // both this one URL, so a row that says "in the inbox" costs no
        // further network at a till.
        url: URL.createObjectURL(item.blob),
      });
    }
  } catch (problem) {
    // The photo is never dropped: it stays in the queue and in IndexedDB, and
    // Retry is a button rather than a reload.
    item.state = "failed";
    item.why = problem.message;
  } finally {
    running -= 1;
    render();
    pump();
  }
}

/**
 * What has already reached the database, each with a box for a note.
 *
 * Deliberately after the upload rather than before it. A field on the queue
 * item would be typed into while the bytes are still in flight, and a send
 * that failed would take the words with it -- so the note is offered at the
 * only moment it can be saved, against a row that certainly exists.
 *
 * The note is the receipt's own free text, which is the field the panel in
 * the app shows beside the picture. It is not the transaction's memo: at the
 * till there is no transaction yet, and inventing one to hang a memo on is how
 * the inbox stops meaning "evidence waiting for its row".
 */
const LANDED_STATE = {
  in: "✓ in the inbox",
  saving: "saving the receipt notes…",
  // A confirmation that stays put. The note is the one thing on this page
  // that can silently not have happened, and a message that vanishes after
  // three seconds is a message somebody at a till misses.
  saved: "✓ in the inbox · receipt notes saved",
  failed: "✓ in the inbox · the receipt notes did not save — type them again to retry",
};

/**
 * How many landed receipts stay on the page, each holding its photo's bytes.
 *
 * Every row keeps an object URL of the photo it sent, and an object URL pins
 * its blob in memory until it is revoked. Uncapped, an afternoon of receipts at
 * a phone's resolution was all still resident in a tab that never reloads. The
 * oldest fall off the bottom here and their bytes are let go; they are in the
 * inbox, which is where a note can still be added to them.
 */
export const LANDED_KEPT = 25;

/** Put a receipt at the top of the landed list, freeing whatever falls off. */
export function land(entry) {
  landed.unshift(entry);
  for (const gone of landed.splice(LANDED_KEPT)) URL.revokeObjectURL(gone.url);
  landedRender();
}

/**
 * Show a blob in an <img> for as long as it takes to decode, then let it go.
 *
 * Revoked on error as well as on load: a photo the browser cannot decode (a
 * HEIC on a desktop, a truncated file) never fires `onload`, and its URL used
 * to hold the whole blob for the life of the page -- once per `render()`.
 */
export function showOnce(img, blob) {
  const url = URL.createObjectURL(blob);
  const free = () => URL.revokeObjectURL(url);
  img.onload = free;
  img.onerror = free;
  img.src = url;
}

function landedRender() {
  els.landed.replaceChildren(
    ...landed.map((one) => {
      const row = document.createElement("li");
      row.className = "landed";

      // The picture, on the left, with a larger copy under the pointer. Both
      // are the blob this page still holds; nothing is fetched back.
      const shot = document.createElement("div");
      shot.className = "shot";
      const small = document.createElement("img");
      small.src = one.url;
      small.alt = "";
      const peek = document.createElement("img");
      peek.className = "peek";
      peek.src = one.url;
      peek.alt = "";
      shot.append(small, peek);

      const middle = document.createElement("div");
      middle.className = "grow";

      const name = document.createElement("div");
      name.className = "name";
      name.textContent = one.name;

      const state = document.createElement("div");
      state.className = "state";
      state.textContent = LANDED_STATE[one.state] || LANDED_STATE.in;

      const note = document.createElement("input");
      note.className = "note-field";
      note.type = "text";
      note.placeholder = "What was it for? (optional)";
      note.value = one.note;
      note.setAttribute("aria-label", `Receipt notes for ${one.name}`);
      // On the way out of the field, like everywhere else in this app that
      // saves without a button.
      note.onchange = () => saveNote(one, note.value);
      note.onblur = () => saveNote(one, note.value);

      middle.append(name, state, note);
      row.append(shot, middle);
      return row;
    }),
  );
}

async function saveNote(entry, text) {
  const clean = text.trim();
  // What is in the box, always -- a failed save must not take the words back
  // out of it. What was *stored* is `saved`, and the two being separate is
  // what lets typing the same thing again be a retry.
  entry.note = clean;
  if (clean === entry.saved || clean === entry.pending) return;
  entry.pending = clean;
  entry.state = "saving";
  landedRender();
  try {
    const answer = await fetch(`/api/receipts/${entry.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(clean ? { note: clean } : { clear_note: true }),
    });
    // Checked, and it was not: a 4xx read exactly like a save, so a note
    // longer than the column or a session that had expired since the photo
    // went up said "✓" and was gone. The receipt is safely stored either way
    // -- only the note failed -- and saying so beats a silent nothing.
    if (!answer.ok) throw new Error(`the server said ${answer.status}`);
  } catch {
    entry.pending = null;
    entry.state = "failed";
    landedRender();
    return;
  }
  // Said out loud, and it stays said. Writing the note is the one thing here
  // that people do not watch finish.
  entry.pending = null;
  entry.saved = clean;
  entry.state = clean ? "saved" : "in";
  landedRender();
}

function render() {
  els.summary.textContent = sent
    ? `✓ ${sent} sent to the inbox`
    : "";
  els.queue.replaceChildren(
    ...[...items.entries()].map(([id, item]) => {
      const row = document.createElement("li");
      if (item.state === "failed") row.className = "failed";

      const preview = document.createElement("img");
      preview.alt = "";
      showOnce(preview, item.blob);

      const middle = document.createElement("div");
      middle.className = "grow";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = item.name;
      const state = document.createElement("div");
      state.className = "state";
      state.textContent =
        item.state === "sending"
          ? "Sending…"
          : item.state === "failed"
            ? item.why
            : "Waiting";
      middle.append(name, state);
      row.append(preview, middle);

      if (item.state === "failed") {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "retry";
        retry.textContent = "Retry";
        retry.onclick = () => {
          item.state = "waiting";
          render();
          pump();
        };
        row.append(retry);
      }
      return row;
    }),
  );
}

// --------------------------------------------------------------------------

async function queueAll(input) {
  const files = [...input.files];
  input.value = "";
  // Queued one after another rather than all at once: `take` hashes and
  // re-encodes, both on the main thread, and firing a dozen in parallel would
  // freeze the page before any of them reached the network. The uploads
  // themselves overlap -- that is what `pump` is for.
  for (const file of files) await take(file);
}

els.shoot.onclick = () => els.file.click();
els.file.onchange = () => queueAll(els.file);

// The same path, from the picker rather than the shutter. Both end in `take`,
// so a photo chosen from the library is hashed, re-encoded and queued exactly
// like one taken here -- there is no second code path to keep in step.
els.several.onclick = () => els.files.click();
els.files.onchange = () => queueAll(els.files);

void start();
