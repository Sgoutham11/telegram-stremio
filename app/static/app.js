const $ = (id) => document.getElementById(id);
let connectionId = null;
let pollTimer = null;
let selectedRemote = null;

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Request failed");
  return body;
}

function notice(message = "") { $("notice").textContent = message; }

const storageResult = new URLSearchParams(window.location.search).get("storage");
if (storageResult === "failed") {
  notice("Google Drive authorization was not completed or Drive access could not be verified.");
} else if (storageResult === "duplicate") {
  notice("That Google account is already connected. Choose a different account.");
} else if (storageResult === "limit") {
  notice("You have reached the Google Drive connection limit.");
} else if (storageResult === "identity") {
  notice("Google did not return a verified account email.");
} else if (storageResult === "legacy") {
  notice("Disconnect and reconnect the existing Google Drive once before adding another.");
} else if (storageResult === "connected") {
  notice("Google Drive connected.");
}

async function refresh() {
  try {
    const status = await api("api/status");
    $("telegram-status").textContent = status.telegram.connected
      ? `Connected as: ${status.telegram.displayName}` : "Not connected";
    $("telegram-connect").hidden = status.telegram.connected;
    $("telegram-disconnect-actions").hidden = !status.telegram.connected;
    selectedRemote = status.storage.remote;
    $("storage-status").textContent =
      `${status.storage.count} of ${status.storage.limit} Google Drives connected`;
    const list = $("storage-connections");
    list.replaceChildren();
    for (const connection of status.storage.connections) {
      const item = document.createElement("li");
      item.textContent =
        `${connection.remote} — ${connection.email || "unknown account"}`
        + (connection.selected ? " (selected)" : "");
      list.appendChild(item);
    }
    $("storage-connect").hidden = !status.storage.canAdd;
    $("storage-disconnect").hidden = !selectedRemote;
    $("active-panel").hidden = !status.active;
  } catch (error) {
    notice(error.message);
  }
}

function applyLogin(login) {
  connectionId = login.connectionId;
  $("qr-panel").hidden = login.status !== "WAITING_FOR_SCAN";
  $("two-factor").hidden = login.status !== "TWO_FACTOR_REQUIRED";
  if (login.qrImage) $("qr-image").src = login.qrImage;
  if (login.message) notice(login.message);
  if (login.status === "CONNECTED") {
    clearInterval(pollTimer);
    $("qr-panel").hidden = true;
    $("two-factor").hidden = true;
    refresh();
  }
  if (["FAILED", "EXPIRED"].includes(login.status)) clearInterval(pollTimer);
}

$("telegram-connect").addEventListener("click", async () => {
  notice();
  try {
    const login = await api("api/telegram/connect/start", {method: "POST"});
    applyLogin(login);
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try { applyLogin(await api(`api/telegram/connect/status/${connectionId}`)); }
      catch (error) { notice(error.message); clearInterval(pollTimer); }
    }, 2000);
  } catch (error) { notice(error.message); }
});

$("two-factor").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = $("two-factor-password").value;
  $("two-factor-password").value = "";
  try {
    applyLogin(await api("api/telegram/connect/2fa", {
      method: "POST",
      body: JSON.stringify({connectionId, password})
    }));
  } catch (error) { notice(error.message); }
});

async function disconnectTelegram(action) {
  try {
    await api("api/telegram/disconnect", {
      method: "POST", body: JSON.stringify({action})
    });
    await refresh();
  } catch (error) { notice(error.message); }
}

$("telegram-disconnect").addEventListener("click", () => disconnectTelegram("local"));
$("telegram-revoke").addEventListener("click", () => disconnectTelegram("revoke"));
$("storage-connect").addEventListener("click", () => {
  window.location.assign("api/storage/google/connect");
});
$("storage-disconnect").addEventListener("click", async () => {
  try {
    await api("api/storage/google/disconnect", {
      method: "POST",
      body: JSON.stringify({remote: selectedRemote})
    });
    selectedRemote = null;
    await refresh();
  } catch (error) { notice(error.message); }
});

refresh();
