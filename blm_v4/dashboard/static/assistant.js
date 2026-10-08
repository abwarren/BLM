/* BLM assistant — a read-only chat panel over the platform.
 *
 * Talks to POST /api/v4/assistant/chat.  That route sits behind the dashboard's
 * own auth middleware, so the session cookie IS the login; mutating requests
 * need the per-page CSRF token read from /api/auth/me, exactly as dashboard.js
 * does.  The agent behind it is read-only by construction (mode=ro SQLite
 * connections, no shell), so nothing a user asks here can change the system.
 *
 * Self-contained: it injects its own panel and styles, so index.html only needs
 * one extra <script> tag.
 */
(function () {
  "use strict";

  let csrf = "";
  let history = [];        // [{role, content}] — the running conversation
  let busy = false;

  const CSS = `
  #asstPill{position:fixed;right:18px;bottom:18px;z-index:9998;border:0;cursor:pointer;
    border-radius:999px;padding:12px 16px;font:600 14px system-ui,sans-serif;
    background:#1f6feb;color:#fff;box-shadow:0 4px 18px rgba(0,0,0,.45)}
  #asstPill:hover{filter:brightness(1.12)}
  #asstPanel{position:fixed;right:18px;bottom:74px;z-index:9999;width:min(440px,92vw);
    height:min(620px,76vh);display:flex;flex-direction:column;border-radius:14px;
    background:#11151c;color:#e7ecf3;border:1px solid #263041;
    box-shadow:0 18px 48px rgba(0,0,0,.6);font:14px/1.5 system-ui,sans-serif}
  #asstPanel[hidden]{display:none}
  #asstHead{display:flex;align-items:center;gap:8px;padding:10px 12px;
    border-bottom:1px solid #263041;background:#0d1117;border-radius:14px 14px 0 0}
  #asstHead b{font-size:13px;letter-spacing:.3px}
  #asstHead .tag{font-size:11px;color:#8b98a9;border:1px solid #2b3648;
    border-radius:6px;padding:1px 6px}
  #asstHead .sp{flex:1}
  #asstHead button{background:none;border:0;color:#8b98a9;font-size:18px;cursor:pointer}
  #asstLog{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
  .asstMsg{max-width:88%;padding:9px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word}
  .asstUser{align-self:flex-end;background:#1f6feb;color:#fff;border-bottom-right-radius:4px}
  .asstBot{align-self:flex-start;background:#1a2029;border:1px solid #263041;border-bottom-left-radius:4px}
  .asstSys{align-self:center;color:#8b98a9;font-size:12px}
  .asstTools{align-self:flex-start;color:#7f8ea3;font-size:11.5px;padding-left:4px}
  .asstErr{align-self:flex-start;background:#3a1d22;border:1px solid #6b2b33;color:#ffb4bd}
  #asstForm{display:flex;gap:8px;padding:10px;border-top:1px solid #263041}
  #asstInput{flex:1;background:#0d1117;border:1px solid #2b3648;color:#e7ecf3;
    border-radius:9px;padding:10px 11px;font:14px system-ui,sans-serif;outline:none}
  #asstInput:focus{border-color:#1f6feb}
  #asstSend{background:#1f6feb;border:0;color:#fff;border-radius:9px;padding:0 15px;
    font:600 14px system-ui,sans-serif;cursor:pointer}
  #asstSend[disabled]{opacity:.5;cursor:default}
  .asstDot:after{content:"";animation:asstDots 1.2s steps(4,end) infinite}
  @keyframes asstDots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
  `;

  const PANEL = `
  <button id="asstPill" title="Ask the BLM assistant">💬 Assistant</button>
  <div id="asstPanel" hidden>
    <div id="asstHead">
      <b>BLM ASSISTANT</b><span class="tag">read-only</span>
      <span class="sp"></span>
      <button id="asstClose" title="Close">×</button>
    </div>
    <div id="asstLog"></div>
    <form id="asstForm" autocomplete="off">
      <input id="asstInput" placeholder="Ask about the BLM — bets, games, why nothing fired…" maxlength="4000">
      <button id="asstSend" type="submit">Send</button>
    </form>
  </div>`;

  const $ = (id) => document.getElementById(id);

  function inject() {
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    const host = document.createElement("div");
    host.innerHTML = PANEL;
    document.body.appendChild(host);

    $("asstPill").addEventListener("click", toggle);
    $("asstClose").addEventListener("click", () => { $("asstPanel").hidden = true; });
    $("asstForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("asstInput").value.trim();
      if (v) { $("asstInput").value = ""; ask(v); }
    });
    if (window.matchMedia("(max-width: 700px)").matches) {
      // on a phone, open it straight away — that's where it gets used
    }
  }

  function toggle() {
    const p = $("asstPanel");
    p.hidden = !p.hidden;
    if (!p.hidden) $("asstInput").focus();
  }

  function bubble(text, cls) {
    const d = document.createElement("div");
    d.className = "asstMsg " + cls;
    d.textContent = text;
    $("asstLog").appendChild(d);
    $("asstLog").scrollTop = $("asstLog").scrollHeight;
    return d;
  }

  function tools(line) {
    const d = document.createElement("div");
    d.className = "asstTools";
    d.textContent = line;
    $("asstLog").appendChild(d);
    $("asstLog").scrollTop = $("asstLog").scrollHeight;
  }

  async function ensureCsrf() {
    if (csrf) return csrf;
    const r = await fetch("/api/auth/me", { credentials: "same-origin",
                                            headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error("session expired — reload and sign in again");
    const d = await r.json().catch(() => ({}));
    if (d && d.csrf_token) csrf = d.csrf_token;
    return csrf;
  }

  async function ask(text) {
    if (busy) return;
    busy = true;
    $("asstSend").disabled = true;
    bubble(text, "asstUser");
    const thinking = bubble("Thinking", "asstSys asstDot");

    try {
      await ensureCsrf();
      const res = await fetch("/api/v4/assistant/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json",
                   "X-CSRF-Token": csrf,
                   "Accept": "application/json" },
        body: JSON.stringify({ message: text, history: history.slice(-12) }),
      });
      thinking.remove();
      if (res.status === 401 || res.status === 403) {
        csrf = "";
        bubble("Your dashboard session expired. Reload the page and sign in, then ask again.", "asstMsg asstErr");
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        bubble("Assistant error (" + res.status + ")" + (body.detail ? ": " + body.detail : ""), "asstMsg asstErr");
        return;
      }
      const data = await res.json();
      bubble(data.reply || "(no reply)", "asstBot");
      const calls = data.tool_calls || [];
      if (calls.length) {
        tools("looked at: " + calls.map((c) => c.tool).join(", "));
      }
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: data.reply || "" });
      if (history.length > 24) history = history.slice(-24);
    } catch (e) {
      thinking.remove();
      bubble("Could not reach the assistant: " + (e && e.message ? e.message : e), "asstMsg asstErr");
    } finally {
      busy = false;
      $("asstSend").disabled = false;
      $("asstInput").focus();
    }
  }

  async function probe() {
    try {
      const r = await fetch("/api/v4/assistant/status", { credentials: "same-origin",
                                                          headers: { "Accept": "application/json" } });
      if (r.ok) {
        const d = await r.json();
        if (!d.available) {
          $("asstPill").title = "assistant not configured: " + (d.reason || "");
        }
      }
    } catch (e) { /* the panel still opens and reports the error on send */ }
  }

  function boot() {
    if (document.getElementById("asstPill")) return;
    inject();
    probe();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
