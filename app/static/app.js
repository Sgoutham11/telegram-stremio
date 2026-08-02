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
  if (!response.ok) {
    const detail = typeof body.detail === "string"
      ? body.detail : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return body;
}

function notice(message = "", type = "info") {
  const element = $("notice");
  element.textContent = message;
  element.classList.toggle("error", Boolean(message) && type === "error");
}

const storageResult = new URLSearchParams(window.location.search).get("storage");
if (storageResult === "failed") {
  notice("Google Drive authorization was not completed or Drive access could not be verified.", "error");
} else if (storageResult === "duplicate") {
  notice("That Google account is already connected. Choose a different account.", "error");
} else if (storageResult === "limit") {
  notice("You have reached the Google Drive connection limit.", "error");
} else if (storageResult === "identity") {
  notice("Google did not return a verified account email.", "error");
} else if (storageResult === "permissions") {
  notice("Google Drive permissions have changed. Please disconnect and reconnect Google Drive.", "error");
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
    notice(error.message, "error");
  }
}

function applyLogin(login) {
  connectionId = login.connectionId;
  $("login-methods").hidden = true;
  $("phone-start").hidden = true;
  $("qr-panel").hidden = login.status !== "WAITING_FOR_SCAN";
  $("phone-code").hidden = login.status !== "WAITING_FOR_CODE";
  $("two-factor").hidden = login.status !== "TWO_FACTOR_REQUIRED";
  if (login.qrImage) $("qr-image").src = login.qrImage;
  if (login.message) {
    const loginError = ["FAILED", "EXPIRED"].includes(login.status)
      || /^(incorrect|invalid|unable|this .* expired|telegram rate limited|telegram has restricted|enter a valid|no telegram account)/i.test(login.message);
    notice(
      login.message,
      loginError ? "error" : "info"
    );
  }
  if (login.status === "CONNECTED") {
    clearInterval(pollTimer);
    $("qr-panel").hidden = true;
    $("phone-code").hidden = true;
    $("two-factor").hidden = true;
    refresh();
  }
  if (["FAILED", "EXPIRED"].includes(login.status)) {
    clearInterval(pollTimer);
    $("telegram-connect").hidden = false;
  }
}

function startLoginPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try { applyLogin(await api(`api/telegram/connect/status/${connectionId}`)); }
    catch (error) { notice(error.message, "error"); clearInterval(pollTimer); }
  }, 2000);
}

$("telegram-connect").addEventListener("click", () => {
  notice();
  $("telegram-connect").hidden = true;
  $("login-methods").hidden = false;
});

$("telegram-connect-phone").addEventListener("click", () => {
  $("login-methods").hidden = true;
  $("phone-start").hidden = false;
  $("telegram-phone").focus();
});

$("telegram-connect-qr").addEventListener("click", async () => {
  notice();
  try {
    const login = await api("api/telegram/connect/start", {method: "POST"});
    applyLogin(login);
    startLoginPolling();
  } catch (error) {
    notice(error.message, "error");
    $("telegram-connect").hidden = false;
  }
});

$("phone-start").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  try {
    const login = await api("api/telegram/connect/phone/start", {
      method: "POST",
      body: JSON.stringify({phoneNumber: $("telegram-phone").value})
    });
    $("telegram-phone").value = "";
    applyLogin(login);
    if (!["FAILED", "EXPIRED"].includes(login.status)) startLoginPolling();
  } catch (error) {
    notice(error.message, "error");
    $("phone-start").hidden = false;
  }
});

$("phone-code").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = $("telegram-code").value;
  $("telegram-code").value = "";
  try {
    applyLogin(await api("api/telegram/connect/phone/code", {
      method: "POST",
      body: JSON.stringify({connectionId, code})
    }));
  } catch (error) { notice(error.message, "error"); }
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
  } catch (error) { notice(error.message, "error"); }
});

async function disconnectTelegram(action) {
  try {
    await api("api/telegram/disconnect", {
      method: "POST", body: JSON.stringify({action})
    });
    await refresh();
  } catch (error) { notice(error.message, "error"); }
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
  } catch (error) { notice(error.message, "error"); }
});

refresh();
