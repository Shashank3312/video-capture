// Phase 4 front end. Deliberately plain: no build step, no framework,
// because the interesting parts of this project are the watchers, and
// a single-user MVP does not need a toolchain to maintain.

let kind = "video";

const $ = (sel) => document.querySelector(sel);
const banner = $("#pushBanner");

function showModeFields() {
  document.querySelectorAll("[data-for]").forEach((el) => {
    el.classList.toggle("hide", !el.dataset.for.split(" ").includes(kind));
  });
  // "Also listen for a name" only makes sense while watching a face.
  $("#alsoListen").classList.toggle("hide", kind !== "video");
}

document.querySelectorAll(".modes button").forEach((btn) => {
  btn.addEventListener("click", () => {
    kind = btn.dataset.kind;
    document.querySelectorAll(".modes button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b === btn))
    );
    showModeFields();
    if (kind === "sports") loadMatches();
  });
});

// Nobody knows a Cricbuzz match id, so offer the live matches by name
// and keep the id out of sight.
let matchesLoaded = false;
async function loadMatches() {
  if (matchesLoaded) return;
  const picker = $("#matchPicker");
  const note = $("#matchNote");
  try {
    const res = await fetch("/api/cricket/matches");
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "couldn't load matches");
    if (!data.matches.length) {
      picker.innerHTML = '<option value="">No matches are live right now</option>';
      note.textContent = "Cricket jobs need a match that's actually in progress.";
      return;
    }
    picker.innerHTML = data.matches
      .map((m) => `<option value="${m.match_id}">${escapeHtml(m.teams)}</option>`)
      .join("");
    note.textContent = data.matches[0].status || "";
    picker.addEventListener("change", () => {
      const chosen = data.matches.find((m) => m.match_id === picker.value);
      note.textContent = chosen ? chosen.status : "";
    });
    matchesLoaded = true;
  } catch (err) {
    picker.innerHTML = '<option value="">Couldn\'t load matches</option>';
    note.textContent = err.message;
  }
}

// ---------------------------------------------------------------- push

function showBanner(message) {
  $("#pushMessage").textContent = message;
  banner.classList.remove("hide");
}

async function setUpNotifications() {
  if (typeof firebaseConfig === "undefined" || !firebaseConfig.apiKey) {
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
    if (!firebase.apps.length) firebase.initializeApp(firebaseConfig);
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
    await fetch("/api/devices", { method: "POST", body });
    banner.classList.add("hide");

    // A notification arriving while the page is open doesn't show by
    // itself, so show it rather than letting the alert vanish.
    //
    // It MUST go through the service worker registration: on Android
    // Chrome `new Notification(...)` is an illegal constructor and
    // throws, so a notification arriving while the app was on screen
    // silently never appeared.
    messaging.onMessage((payload) => {
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
    // Show the real error. Registration failing silently is what made
    // "no phone has registered" impossible to act on.
    showBanner("Couldn't set up notifications: " + (err && err.message ? err.message : err));
  }
}

$("#enableBtn").addEventListener("click", () => {
  $("#pushMessage").textContent = "Trying again...";
  setUpNotifications();
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

  if (kind === "sports") {
    carry("match_id");
    carry("player");
  } else {
    carry("url");
    carry("max_minutes");
    carry("alert_mode");
    if (kind === "audio") carry("listen_for");
    else carry("listen_for_video", "listen_for");
    const photo = form.get("photo");
    if (photo && photo.size) body.append("photo", photo);
  }

  const btn = $("#startBtn");
  btn.disabled = true;
  btn.textContent = "Starting...";
  try {
    const res = await fetch("/api/jobs", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "couldn't start it");
    loadJobs();
  } catch (err) {
    alert(err.message);
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
  const res = await fetch("/api/jobs");
  const jobs = await res.json();
  const box = $("#jobs");
  if (!jobs.length) {
    box.textContent = "Nothing yet.";
    return;
  }
  const done = (s) => ["expired", "failed", "matched"].includes(s);
  box.innerHTML = jobs
    .map(
      (job) => `
      <div class="job">
        <div class="status ${job.status}">${label(job)}</div>
        <div>${escapeHtml(describe(job))}</div>
        ${job.progress ? `<div class="muted">${escapeHtml(job.progress)}</div>` : ""}
        ${done(job.status)
          ? `<button class="ghost" style="width:auto;margin-top:8px;padding:6px 12px;font-size:13px"
                     data-restart="${job.id}">Watch again</button>`
          : `<button class="ghost" style="width:auto;margin-top:8px;padding:6px 12px;font-size:13px"
                     data-cancel="${job.id}">Stop</button>`}
      </div>`
    )
    .join("");

  box.querySelectorAll("[data-restart]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`/api/jobs/${btn.dataset.restart}/restart`, { method: "POST" });
      loadJobs();
    })
  );
  box.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`/api/jobs/${btn.dataset.cancel}/cancel`, { method: "POST" });
      loadJobs();
    })
  );
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
  const res = await fetch("/api/test-notification", { method: "POST" });
  const data = await res.json();
  if (data.ok) {
    alert(`Sent to ${data.sent} device(s). Check your phone.`);
    return;
  }
  // Point at the fix rather than just restating the problem.
  alert(
    data.error +
      "\n\nScroll to the top of this page and tap 'Enable notifications' to register this phone."
  );
});

showModeFields();
setUpNotifications();
loadJobs();
setInterval(loadJobs, 4000);
