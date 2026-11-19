// Phase 4 front end, with Phase 5 accounts. Deliberately plain: no build
// step, no framework, because the interesting parts of this project are
// the watchers, and nothing here needs a toolchain to maintain.

let kind = "video";
let isOwner = false;
let signedIn = false;
let jobsTimer = null;
// Bumped on every sign-in and sign-out. A response fetched with one
// user's token must never be rendered for the next user, so each loader
// notes this before its request and drops the answer if it changed.
let session = 0;

const $ = (sel) => document.querySelector(sel);
const banner = $("#pushBanner");

// ---------------------------------------------------------------- sign-in
//
// Every /api route except /api/health needs a Firebase ID token, so
// nothing below may fetch("/api/...") directly - it goes through api().

class AuthError extends Error {}

const authConfigured = typeof firebaseConfig !== "undefined" && !!firebaseConfig.apiKey;
if (authConfigured && !firebase.apps.length) firebase.initializeApp(firebaseConfig);

// Set just before a forced sign-out, so the sign-in screen can say why
// (the sign-out itself arrives later, through onAuthStateChanged).
let signOutReason = "";

const INVITE_ONLY = "this app is invite-only for now";

function detailOf(data, fallback) {
  return data && typeof data.detail === "string" ? data.detail : fallback;
}

async function api(path, options = {}) {
  const user = firebase.auth().currentUser;
  if (!user) throw new AuthError("sign in first");
  // getIdToken() refreshes an expired token by itself; ID tokens only
  // last an hour, so asking each time is the only safe way.
  const token = await user.getIdToken();
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(path, { ...options, headers });

  if (res.status === 401) {
    signOutReason = "Your sign-in has expired. Please sign in again.";
    signOutQuietly();
    throw new AuthError("sign in first");
  }
  if (res.status === 403) {
    // 403 is also how a non-owner is refused a face/name watch, and that
    // must stay a normal error message. Only the invite-only refusal
    // means "this account is not allowed in at all".
    const data = await res.clone().json().catch(() => null);
    if (detailOf(data, "") === INVITE_ONLY) {
      signOutReason = INVITE_ONLY.charAt(0).toUpperCase() + INVITE_ONLY.slice(1) + ".";
      signOutQuietly();
      throw new AuthError(INVITE_ONLY);
    }
  }
  return res;
}

function signOutQuietly() {
  // A failed sign-out must not become an unhandled rejection.
  return firebase.auth().signOut().catch(() => {});
}

function showSignInScreen(message) {
  $("#app").classList.add("hide");
  $("#signedOut").classList.remove("hide");
  const box = $("#signInError");
  box.textContent = message || "";
  box.classList.toggle("hide", !message);
}

async function signIn() {
  if (!authConfigured) {
    showSignInScreen("Sign-in isn't configured on the server yet (no Firebase web config).");
    return;
  }
  showSignInScreen("");
  const provider = new firebase.auth.GoogleAuthProvider();
  // A popup is often blocked, and an installed home-screen app can't
  // reliably open one, so those cases use a full-page redirect instead.
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  try {
    if (standalone) {
      await firebase.auth().signInWithRedirect(provider);
    } else {
      await firebase.auth().signInWithPopup(provider);
    }
  } catch (err) {
    if (err.code === "auth/popup-blocked") {
      try {
        await firebase.auth().signInWithRedirect(provider);
      } catch (err2) {
        showSignInScreen("Couldn't sign in: " + err2.message);
      }
      return;
    }
    // Closing the popup is the user changing their mind, not an error.
    if (["auth/popup-closed-by-user", "auth/cancelled-popup-request"].includes(err.code)) return;
    showSignInScreen("Couldn't sign in: " + err.message);
  }
}

$("#signInBtn").addEventListener("click", signIn);
$("#signOutBtn").addEventListener("click", signOutQuietly);

async function startSession(user) {
  // Ask the server who this is before showing anything: it decides
  // which modes exist, and it is also where "not on the list" and
  // "server can't check sign-ins" surface.
  let me;
  try {
    const res = await api("/api/me");
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      signOutReason = detailOf(data, "Couldn't sign in.");
      signOutQuietly();
      return;
    }
    me = data;
  } catch (err) {
    if (err instanceof AuthError) return; // api() already signed out
    signOutReason = "Couldn't reach the server: " + err.message;
    signOutQuietly();
    return;
  }
  // They may have signed out while /api/me was in flight.
  const current = firebase.auth().currentUser;
  if (!current || current.uid !== user.uid) return;

  signedIn = true;
  signOutReason = "";
  isOwner = !!me.is_owner;
  $("#whoAmI").textContent = me.email;
  $("#signedOut").classList.add("hide");
  $("#app").classList.remove("hide");
  applyRole();
  setUpNotifications();
  loadPeople();
  loadJobs();
  clearInterval(jobsTimer); // a quick sign-out/in must not double the polling
  jobsTimer = setInterval(loadJobs, 4000);
}

// Nothing from the last user may stay in the page for the next one.
function endSession() {
  signedIn = false;
  isOwner = false;
  clearInterval(jobsTimer);
  jobsTimer = null;
  if (unsubscribeMessages) {
    unsubscribeMessages();
    unsubscribeMessages = null;
  }
  revokeThumbs();
  people = [];
  newPhotos = [];
  liveMatches = [];
  matchesLoaded = false;
  $("#jobs").textContent = "Nothing yet.";
  $("#peopleList").textContent = "Nobody saved yet.";
  $("#pPhotoList").innerHTML = "";
  $("#personForm").reset();
  $("#personForm").classList.add("hide");
  $("#jobForm").reset();
  $("#photoCheck").classList.add("hide");
  $("#photoCheck").innerHTML = "";
  $("#whoPicker").innerHTML = "";
  $("#whoAmI").textContent = "";
  $("#whoNote").textContent = "";
  banner.classList.add("hide");
}

if (authConfigured) {
  // A redirect sign-in returns here with the result (or error) waiting.
  // The first onAuthStateChanged(null) redraws the sign-in screen, so
  // the error is also parked in signOutReason or it would be wiped.
  firebase.auth().getRedirectResult().catch((err) => {
    signOutReason = "Couldn't sign in: " + err.message;
    showSignInScreen(signOutReason);
  });
  firebase.auth().onAuthStateChanged((user) => {
    session++;
    if (user) {
      startSession(user);
    } else {
      endSession();
      showSignInScreen(signOutReason);
      signOutReason = "";
    }
  });
} else {
  showSignInScreen("Sign-in isn't configured on the server yet (no Firebase web config).");
}

// ---------------------------------------------------------------- modes

function showModeFields() {
  document.querySelectorAll("[data-for]").forEach((el) => {
    el.classList.toggle("hide", !el.dataset.for.split(" ").includes(kind));
  });
  // "Also listen for a name" only makes sense while watching a face.
  $("#alsoListen").classList.toggle("hide", kind !== "video");
  updateWho();
}

function setKind(next) {
  kind = next;
  document.querySelectorAll(".modes button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.kind === kind))
  );
  refreshWho();
  showModeFields();
  if (kind === "sports") loadMatches();
}

document.querySelectorAll(".modes button").forEach((btn) => {
  btn.addEventListener("click", () => setKind(btn.dataset.kind));
});

// Face and name watches run on the owner's YouTube session, so for
// anyone else they don't exist: hidden, not just disabled. The server
// refuses them too - hiding the buttons is not the protection.
function applyRole() {
  document.querySelectorAll(".modes button").forEach((b) => {
    if (b.dataset.kind !== "sports") b.classList.toggle("hide", !isOwner);
  });
  setKind(isOwner ? "video" : "sports");
}

// Nobody knows a Cricbuzz match id, so offer the live matches by name
// and keep the id out of sight.
let matchesLoaded = false;
let liveMatches = [];
$("#matchPicker").addEventListener("change", () => {
  const chosen = liveMatches.find((m) => m.match_id === $("#matchPicker").value);
  $("#matchNote").textContent = chosen ? chosen.status || "" : "";
});

async function loadMatches() {
  if (matchesLoaded) return;
  const picker = $("#matchPicker");
  const note = $("#matchNote");
  try {
    const res = await api("/api/cricket/matches");
    const data = await res.json();
    if (!res.ok) throw new Error(detailOf(data, "couldn't load matches"));
    liveMatches = data.matches;
    if (!liveMatches.length) {
      picker.innerHTML = '<option value="">No matches are live right now</option>';
      note.textContent = "Cricket jobs need a match that's actually in progress.";
      return;
    }
    picker.innerHTML = liveMatches
      .map((m) => `<option value="${escapeHtml(m.match_id)}">${escapeHtml(m.teams)}</option>`)
      .join("");
    note.textContent = liveMatches[0].status || "";
    matchesLoaded = true;
  } catch (err) {
    if (err instanceof AuthError) return;
    picker.innerHTML = '<option value="">Couldn\'t load matches</option>';
    note.textContent = err.message;
  }
}

// ---------------------------------------------------------------- push

function showBanner(message) {
  $("#pushMessage").textContent = message;
  banner.classList.remove("hide");
}

let unsubscribeMessages = null;

// Only after sign-in: registering a device needs to know whose it is.
async function setUpNotifications() {
  if (!authConfigured) {
    showBanner(
      "Notifications aren't configured on the server yet. Jobs still run; " +
        "you just won't be alerted on this phone."
    );
    return;
  }
  if (!("serviceWorker" in navigator) || !("Notification" in window)) {
    showBanner(
      "This browser can't receive push notifications. On iPhone, use Share -> " +
        "Add to Home Screen first, then open it from the home screen."
    );
    return;
  }
  try {
    const registration = await navigator.serviceWorker.register("/firebase-messaging-sw.js");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      showBanner(
        `Notifications are ${permission}. Tap the lock icon next to the address bar -> ` +
          "Permissions -> Notifications -> Allow, then tap Enable notifications below."
      );
      return;
    }
    const messaging = firebase.messaging();
    const token = await messaging.getToken({
      vapidKey: window.VAPID_KEY,
      serviceWorkerRegistration: registration,
    });
    if (!token) throw new Error("Firebase returned no token");
    const body = new FormData();
    body.append("token", token);
    const res = await api("/api/devices", { method: "POST", body });
    if (!res.ok) throw new Error(detailOf(await res.json().catch(() => null), "server refused the device"));
    banner.classList.add("hide");

    // A notification arriving while the page is open doesn't show by
    // itself, so show it rather than letting the alert vanish.
    //
    // It MUST go through the service worker registration: on Android
    // Chrome `new Notification(...)` is an illegal constructor and
    // throws, so a notification arriving while the app was on screen
    // silently never appeared.
    //
    // Replace any earlier handler: signing out and back in would
    // otherwise stack one per sign-in and show every alert twice.
    if (unsubscribeMessages) unsubscribeMessages();
    unsubscribeMessages = messaging.onMessage((payload) => {
      const n = payload.notification || {};
      const link = (payload.data && payload.data.link) || "/";
      registration.showNotification(n.title || "Don't Miss The Moment", {
        body: n.body || "",
        icon: "/icon.png",
        data: { link },
      });
      loadJobs();
    });
  } catch (err) {
    if (err instanceof AuthError) return;
    // Show the real error. Registration failing silently is what made
    // "no phone has registered" impossible to act on.
    showBanner("Couldn't set up notifications: " + (err && err.message ? err.message : err));
  }
}

$("#enableBtn").addEventListener("click", () => {
  $("#pushMessage").textContent = "Trying again...";
  setUpNotifications();
});

// ---------------------------------------------------------------- photo check

// Show the face that was actually extracted, before it's used. A photo
// in sunglasses matched strangers better than the real person, and
// nothing in the app gave the user any way to notice. Used by the job
// form's photo AND by every photo added to a saved person, so a saved
// person can't be built from photos nobody looked at.
async function checkPhoto(file) {
  const body = new FormData();
  body.append("photo", file);
  try {
    const res = await api("/api/check-photo", { method: "POST", body });
    const d = await res.json();
    if (!res.ok) throw new Error(detailOf(d, "the server couldn't check it"));
    if (!d.ok) {
      return {
        ok: false,
        html: `<div class="facecheck"><div class="warn-text">${escapeHtml(d.error)}</div></div>`,
      };
    }
    const notes = [];
    if (d.faces > 1) notes.push(`${d.faces} faces found - the biggest one will be used`);
    if (d.small) notes.push("this face is small, which makes matching unreliable");
    return {
      ok: true,
      html: `
        <div class="facecheck">
          <img src="data:image/jpeg;base64,${d.crop}" alt="detected face">
          <div>
            <div>This is the face I'll look for (${d.size}px).</div>
            <div class="muted">Can you see the eyes clearly? If not, matching will be poor.</div>
            ${notes.map((n) => `<div class="warn-text">${escapeHtml(n)}</div>`).join("")}
          </div>
        </div>`,
    };
  } catch (err) {
    if (err instanceof AuthError) throw err;
    return {
      ok: false,
      html: `<div class="muted">Couldn't check the photo: ${escapeHtml(err.message)}</div>`,
    };
  }
}

const photoInput = $("#photoInput");
photoInput.addEventListener("change", async () => {
  const box = $("#photoCheck");
  const file = photoInput.files && photoInput.files[0];
  if (!file) {
    box.classList.add("hide");
    return;
  }
  box.classList.remove("hide");
  box.innerHTML = `<div class="muted">Checking the photo...</div>`;
  try {
    box.innerHTML = (await checkPhoto(file)).html;
  } catch (err) {
    box.classList.add("hide");
  }
});

// ---------------------------------------------------------------- saved people

let people = [];
let newPhotos = []; // photos picked in the "add a person" form: {file, ok, html}
let thumbUrls = [];

function revokeThumbs() {
  thumbUrls.splice(0).forEach((u) => URL.revokeObjectURL(u));
}

async function loadPeople() {
  const mine = session;
  try {
    const res = await api("/api/people");
    const data = await res.json();
    if (!res.ok) throw new Error(detailOf(data, "couldn't load people"));
    if (!signedIn || mine !== session) return;
    people = data;
    renderPeople();
    refreshWho();
  } catch (err) {
    if (err instanceof AuthError || mine !== session) return;
    $("#peopleList").textContent = "Couldn't load saved people: " + err.message;
  }
}

function renderPeople() {
  const box = $("#peopleList");
  // <img> can't send the Authorization header, so thumbnails are fetched
  // through api() as blobs. Release the previous ones: a re-render
  // otherwise leaks every photo it ever showed.
  revokeThumbs();
  if (!people.length) {
    box.textContent = "Nobody saved yet.";
    return;
  }
  box.innerHTML = people
    .map((p) => {
      const n = p.photos.length;
      return `
      <div class="person" data-person="${escapeHtml(p.id)}">
        <div class="row">
          <div><b>${escapeHtml(p.name)}</b>
            <span class="muted">(${n} photo${n === 1 ? "" : "s"})</span></div>
          <div class="actions">
            <button type="button" class="small" data-act="watch">Watch</button>
            <button type="button" class="small ghost" data-act="edit">Edit</button>
            <button type="button" class="small ghost" data-act="delete">Delete</button>
          </div>
        </div>
        <form data-editform class="hide">
          <label>Name</label>
          <input name="name" value="${escapeHtml(p.name)}" autocomplete="off">
          <label>Name or phrase to listen for</label>
          <input name="listen_for" placeholder="blank = same as the name"
                 value="${escapeHtml(p.listen_for === p.name ? "" : p.listen_for)}" autocomplete="off">
          <label>Player name for cricket</label>
          <input name="player_name" placeholder="blank = same as the name"
                 value="${escapeHtml(p.player_name === p.name ? "" : p.player_name)}" autocomplete="off">
          <div style="display:flex;gap:8px">
            <button type="submit">Save changes</button>
            <button type="button" class="ghost" data-act="canceledit">Cancel</button>
          </div>
        </form>
        <div class="thumbs">
          ${p.photos
            .map(
              (ph) => `
            <span class="thumb">
              <img data-photo="${escapeHtml(ph.id)}" alt="reference photo">
              <button type="button" data-act="rmphoto" data-photo-id="${escapeHtml(ph.id)}"
                      title="Remove this photo">&times;</button>
            </span>`
            )
            .join("")}
        </div>
        <label style="margin-top:10px">Add a photo</label>
        <input type="file" accept="image/*" data-act="addphoto">
        <div data-addbox></div>
      </div>`;
    })
    .join("");

  box.querySelectorAll("img[data-photo]").forEach(async (img) => {
    const pid = img.closest("[data-person]").dataset.person;
    try {
      const res = await api(`/api/people/${pid}/photos/${img.dataset.photo}`);
      if (!res.ok) return;
      const url = URL.createObjectURL(await res.blob());
      if (!img.isConnected) {
        URL.revokeObjectURL(url); // re-rendered while it downloaded
        return;
      }
      thumbUrls.push(url);
      img.src = url;
    } catch (err) {
      /* a missing thumbnail is cosmetic */
    }
  });
}

function personById(id) {
  return people.find((p) => p.id === id);
}

$("#peopleList").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest("[data-person]");
  const person = personById(card.dataset.person);
  if (!person) return;
  const act = btn.dataset.act;

  if (act === "watch") {
    watchPerson(person);
  } else if (act === "edit") {
    card.querySelector("[data-editform]").classList.toggle("hide");
  } else if (act === "canceledit") {
    card.querySelector("[data-editform]").classList.add("hide");
  } else if (act === "delete") {
    if (!confirm(`Delete ${person.name}? Their photos are deleted too.`)) return;
    btn.disabled = true;
    await removeThing(`/api/people/${person.id}`, "delete the person");
  } else if (act === "rmphoto") {
    if (!confirm("Remove this photo?")) return;
    btn.disabled = true;
    await removeThing(`/api/people/${person.id}/photos/${btn.dataset.photoId}`, "remove the photo");
  } else if (act === "savephoto") {
    const file = card.pendingPhoto;
    if (!file) return;
    btn.disabled = true;
    const body = new FormData();
    body.append("photo", file);
    try {
      const res = await api(`/api/people/${person.id}/photos`, { method: "POST", body });
      if (!res.ok) throw new Error(detailOf(await res.json().catch(() => null), "couldn't add the photo"));
    } catch (err) {
      if (!(err instanceof AuthError)) alert(err.message);
      btn.disabled = false;
      return;
    }
    loadPeople();
  } else if (act === "cancelphoto") {
    card.pendingPhoto = null;
    card.querySelector("[data-addbox]").innerHTML = "";
    card.querySelector("input[data-act=addphoto]").value = "";
  }
});

// Editing matters most for the player name: Cricbuzz matching depends
// on it being spelled right, and the alternative was deleting the
// person and every photo to fix a typo. Blank phrase/player is sent as
// blank, which the server reads as "follow the name again".
$("#peopleList").addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-editform]");
  if (!form) return;
  event.preventDefault();
  const person = personById(form.closest("[data-person]").dataset.person);
  if (!person) return;
  const name = form.elements.name.value.trim();
  if (!name) {
    alert("A person needs a name.");
    return;
  }
  const body = new FormData();
  body.append("name", name);
  body.append("listen_for", form.elements.listen_for.value.trim());
  body.append("player_name", form.elements.player_name.value.trim());
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const res = await api(`/api/people/${person.id}`, { method: "PATCH", body });
    if (!res.ok) throw new Error(detailOf(await res.json().catch(() => null), "couldn't save the changes"));
  } catch (err) {
    if (!(err instanceof AuthError)) alert(err.message);
    btn.disabled = false;
    return;
  }
  loadPeople();
});

async function removeThing(path, what) {
  try {
    const res = await api(path, { method: "DELETE" });
    if (!res.ok) throw new Error(detailOf(await res.json().catch(() => null), `couldn't ${what}`));
  } catch (err) {
    if (!(err instanceof AuthError)) alert(err.message);
    loadPeople();
    return;
  }
  loadPeople();
}

// A photo added to an existing person goes through the same preview as
// everywhere else, and can only be saved if a face was actually found.
$("#peopleList").addEventListener("change", async (event) => {
  const input = event.target.closest("input[data-act=addphoto]");
  if (!input) return;
  const card = input.closest("[data-person]");
  const box = card.querySelector("[data-addbox]");
  const file = input.files && input.files[0];
  card.pendingPhoto = null;
  if (!file) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<div class="muted">Checking the photo...</div>`;
  try {
    const result = await checkPhoto(file);
    if (input.files[0] !== file) return; // they picked another meanwhile
    box.innerHTML =
      result.html +
      `<div style="display:flex;gap:8px">
         ${result.ok ? '<button type="button" class="small" data-act="savephoto">Save photo</button>' : ""}
         <button type="button" class="small ghost" data-act="cancelphoto">Cancel</button>
       </div>`;
    if (result.ok) card.pendingPhoto = file;
  } catch (err) {
    box.innerHTML = "";
  }
});

// "Watch" jumps to the job form with this person chosen. If the current
// mode can't use them (no photos for a face watch), move to one that can.
function watchPerson(person) {
  if (kind === "video" && !person.photos.length) {
    setKind(isOwner ? "audio" : "sports");
  }
  $("#whoPicker").value = person.id;
  updateWho();
  $("#jobForm").scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---- the "add a person" form

$("#addPersonBtn").addEventListener("click", () => {
  $("#personForm").classList.toggle("hide");
});

$("#pCancel").addEventListener("click", () => {
  $("#personForm").reset();
  newPhotos = [];
  renderNewPhotos();
  $("#personForm").classList.add("hide");
});

function renderNewPhotos() {
  const box = $("#pPhotoList");
  box.innerHTML = newPhotos
    .map(
      (p, i) => `
      <div>
        ${p.html}
        <button type="button" class="ghost small" data-rm="${i}">Remove this photo</button>
      </div>`
    )
    .join("");
  box.querySelectorAll("[data-rm]").forEach((b) =>
    b.addEventListener("click", () => {
      newPhotos.splice(Number(b.dataset.rm), 1);
      renderNewPhotos();
    })
  );
}

$("#pPhotos").addEventListener("change", async (event) => {
  const files = Array.from(event.target.files || []);
  event.target.value = ""; // so picking the same file again still fires
  for (const file of files) {
    const entry = { file, ok: false, html: `<div class="muted">Checking ${escapeHtml(file.name)}...</div>` };
    newPhotos.push(entry);
    renderNewPhotos();
    try {
      Object.assign(entry, await checkPhoto(file));
    } catch (err) {
      return; // signed out
    }
    renderNewPhotos();
  }
});

$("#personForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("#pName").value.trim();
  if (!name) {
    alert("Give the person a name.");
    return;
  }
  if (newPhotos.some((p) => !p.ok)) {
    alert("Remove the photos where no face was found (or that are still being checked) first.");
    return;
  }
  const body = new FormData();
  body.append("name", name);
  if ($("#pPhrase").value.trim()) body.append("listen_for", $("#pPhrase").value.trim());
  if ($("#pPlayer").value.trim()) body.append("player_name", $("#pPlayer").value.trim());
  newPhotos.forEach((p) => body.append("photos", p.file));

  const btn = $("#pSave");
  btn.disabled = true;
  try {
    const res = await api("/api/people", { method: "POST", body });
    if (!res.ok) throw new Error(detailOf(await res.json().catch(() => null), "couldn't save the person"));
    $("#personForm").reset();
    newPhotos = [];
    renderNewPhotos();
    $("#personForm").classList.add("hide");
    loadPeople();
  } catch (err) {
    if (!(err instanceof AuthError)) alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

// ---- the "Who?" picker on the job form

// A face watch needs a person with at least one photo; the other modes
// can use anyone saved.
function eligiblePeople() {
  return people.filter((p) => kind !== "video" || p.photos.length);
}

function refreshWho() {
  const picker = $("#whoPicker");
  const keep = picker.value;
  picker.innerHTML =
    `<option value="">Someone new</option>` +
    eligiblePeople()
      .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`)
      .join("");
  picker.value = eligiblePeople().some((p) => p.id === keep) ? keep : "";
  updateWho();
}

// Choosing a saved person fills in what they cover, so that field is
// shown as read-only text rather than left to be retyped (and the page
// doesn't send it - the server uses the person's own).
function updateWho() {
  const person = personById($("#whoPicker").value);
  const playerInput = $("#jobForm [name=player]");
  const phraseInput = $("#jobForm [name=listen_for]");
  const note = $("#whoNote");

  playerInput.readOnly = !!person;
  phraseInput.readOnly = !!person;
  if (person) {
    playerInput.value = person.player_name;
    phraseInput.value = person.listen_for;
  } else if (playerInput.dataset.filled) {
    playerInput.value = "";
    phraseInput.value = "";
  }
  playerInput.dataset.filled = person ? "1" : "";

  note.textContent = "";
  if (person && kind === "video") {
    note.textContent = `Looking for ${person.name}'s ${person.photos.length} saved photo(s).`;
  }
  // The photo box is for someone new only; a saved person brings theirs.
  if (kind === "video") $("#jobForm [data-for=video]").classList.toggle("hide", !!person);
  $("#saveBox").classList.toggle("hide", !!person);
}

$("#whoPicker").addEventListener("change", updateWho);
$("#saveCheck").addEventListener("change", () => {
  $("#saveName").classList.toggle("hide", !$("#saveCheck").checked);
});

// ---------------------------------------------------------------- jobs

$("#jobForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const body = new FormData();
  body.append("kind", kind);

  const carry = (name, as = name) => {
    const value = form.get(name);
    if (value) body.append(as, value);
  };

  const personId = $("#whoPicker").value;
  if (personId) body.append("person_id", personId);

  if (kind === "sports") {
    carry("match_id");
    if (!personId) carry("player"); // a saved person supplies theirs
  } else {
    carry("url");
    carry("max_minutes");
    carry("alert_mode");
    if (kind === "audio") {
      if (!personId) carry("listen_for");
    } else {
      carry("listen_for_video", "listen_for");
      // A photo is refused for anything but a face watch, and ignored
      // when a saved person is chosen, so only send it when it can count.
      const photo = form.get("photo");
      if (!personId && photo && photo.size) body.append("photo", photo);
    }
  }

  if (!personId && $("#saveCheck").checked) {
    const name = $("#saveName").value.trim();
    if (!name) {
      alert("Give the person a name to save them.");
      return;
    }
    body.append("save_person", "1");
    body.append("person_name", name);
  }

  const btn = $("#startBtn");
  btn.disabled = true;
  btn.textContent = "Starting...";
  try {
    const res = await api("/api/jobs", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(detailOf(data, "couldn't start it"));
    if (body.has("save_person")) {
      $("#saveCheck").checked = false;
      $("#saveName").value = "";
      $("#saveName").classList.add("hide");
      loadPeople();
    }
    loadJobs();
  } catch (err) {
    if (!(err instanceof AuthError)) alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Start watching";
  }
});

function describe(job) {
  const p = job.params || {};
  if (job.kind === "sports") return `${p.player} - match ${p.match_id}`;
  const what = job.kind === "audio" ? `listening for "${p.listen_for}"` : "watching for a face";
  return `${job.event_title || p.url || ""} - ${what}`;
}

async function loadJobs() {
  if (!signedIn) return;
  const mine = session;
  let jobs;
  try {
    const res = await api("/api/jobs");
    if (!res.ok) return;
    jobs = await res.json();
  } catch (err) {
    return; // signed out, or a blip: the next poll tries again
  }
  if (!signedIn || mine !== session) return;
  const box = $("#jobs");
  if (!jobs.length) {
    box.textContent = "Nothing yet.";
    return;
  }
  const done = (s) => ["expired", "failed", "matched"].includes(s);
  const smallBtn = `style="width:auto;margin-top:8px;padding:6px 12px;font-size:13px"`;
  box.innerHTML = jobs
    .map(
      (job) => `
      <div class="job">
        <div class="status ${job.status}">${label(job)}</div>
        <div>${escapeHtml(describe(job))}</div>
        ${job.progress ? `<div class="muted">${escapeHtml(job.progress)}</div>` : ""}
        ${alertTimes(job)}
        ${
          !done(job.status)
            ? `<button class="ghost" ${smallBtn} data-cancel="${job.id}">Stop</button>`
            : job.restartable
              ? `<button class="ghost" ${smallBtn} data-restart="${job.id}">Watch again</button>`
              : `<div class="muted" style="margin-top:8px">save this person to watch them again</div>`
        }
      </div>`
    )
    .join("");

  box.querySelectorAll("[data-restart]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await jobAction(`/api/jobs/${btn.dataset.restart}/restart`);
    })
  );
  box.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await jobAction(`/api/jobs/${btn.dataset.cancel}/cancel`);
    })
  );
}

async function jobAction(path) {
  try {
    const res = await api(path, { method: "POST" });
    if (!res.ok) alert(detailOf(await res.json().catch(() => null), "that didn't work"));
  } catch (err) {
    if (!(err instanceof AuthError)) alert(err.message);
  }
  loadJobs();
}

// When they were on is the whole point of a finished watch, so show
// every hit with its time rather than only that there was one.
function alertTimes(job) {
  const log = job.alerts_log || [];
  if (!log.length) return "";
  const rows = log
    .slice(-6)
    .map((a) => {
      const what = a.kind === "name" ? `heard "${a.detail}"` : "on screen";
      // Position into the stream is the useful one - it still means
      // something tomorrow, and it links straight to the moment.
      const when = a.position ? `${a.position} in` : a.at || "?";
      const label = `<b>${escapeHtml(when)}</b> ${escapeHtml(what)}`;
      return `<div class="hit">${
        a.seek_url ? `<a href="${escapeHtml(a.seek_url)}" target="_blank">${label}</a>` : label
      }</div>`;
    })
    .join("");
  const more = log.length > 6 ? `<div class="muted">+${log.length - 6} earlier</div>` : "";
  return `<div class="hits">${rows}${more}</div>`;
}

// "expired" is the one people misread, so say what it means rather
// than showing a bare status word.
function label(job) {
  if (job.status === "expired") return "timed out - never appeared";
  if (job.status === "matched") return "found them";
  if (job.status === "failed") return job.outcome === "cancelled" ? "stopped" : "failed";
  return job.status;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

$("#testBtn").addEventListener("click", async () => {
  let data;
  try {
    const res = await api("/api/test-notification", { method: "POST" });
    data = await res.json();
  } catch (err) {
    if (!(err instanceof AuthError)) alert("Couldn't send it: " + err.message);
    return;
  }
  if (data.ok) {
    alert(`Sent to ${data.sent} device(s). Check your phone.`);
    return;
  }
  // Point at the fix rather than just restating the problem.
  alert(
    (data.error || detailOf(data, "that didn't work")) +
      "\n\nScroll to the top of this page and tap 'Enable notifications' to register this phone."
  );
});

showModeFields();
