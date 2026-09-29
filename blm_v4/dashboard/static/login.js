/* ══════════════════════════════════════════════════════════════════
   BLM — login page behaviour.
   No secrets, no tokens and no credentials are ever persisted here: the
   session lives in an HTTP-only cookie the JavaScript cannot read, and the
   CSRF token is held in memory for the page's lifetime only.
   ══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var form        = document.getElementById("loginForm");
  var identifier  = document.getElementById("identifier");
  var password    = document.getElementById("password");
  var remember    = document.getElementById("remember");
  var submitBtn   = document.getElementById("submitBtn");
  var submitLabel = document.getElementById("submitLabel");
  var submitBusy  = document.getElementById("submitBusy");
  var alertBox    = document.getElementById("loginError");
  var alertText   = document.getElementById("loginErrorText");
  var togglePw    = document.getElementById("togglePw");
  var eyeOpen     = document.getElementById("eyeOpen");
  var eyeOff      = document.getElementById("eyeOff");
  var nextField   = document.getElementById("nextField");
  var identifierCtl = document.getElementById("identifierControl");
  var passwordCtl = document.getElementById("passwordControl");

  /* Messages are OURS, keyed by outcome.  Server error bodies are never
     rendered — an unexpected failure can therefore never leak an internal
     message to the operator. */
  var MESSAGES = {
    invalid:    "Invalid username or password",
    validation: "Enter your username and password",
    rate:       "Too many login attempts. Please try again shortly.",
    unavailable:"Unable to sign in right now. Please try again.",
    expired:    "Your session expired. Please sign in again."
  };

  function sameSitePath(value) {
    if (!value) return "/";
    value = String(value).trim();
    if (value.charAt(0) !== "/" || value.charAt(1) === "/") return "/";
    if (/[\r\n\\]/.test(value)) return "/";
    if (value.indexOf("/login") === 0) return "/";
    return value;
  }

  function showError(message) {
    alertText.textContent = message;
    alertBox.classList.add("show");
  }

  function clearError() {
    alertBox.classList.remove("show");
  }

  function setBusy(busy) {
    submitBtn.disabled = busy;
    submitLabel.style.display = busy ? "none" : "inline-flex";
    submitBusy.style.display  = busy ? "inline-flex" : "none";
    submitBtn.setAttribute("aria-busy", busy ? "true" : "false");
    identifier.readOnly = busy;
    password.readOnly = busy;
  }

  function markInvalid() {
    identifierCtl.classList.add("is-invalid");
    passwordCtl.classList.add("is-invalid");
  }

  function clearInvalid() {
    identifierCtl.classList.remove("is-invalid");
    passwordCtl.classList.remove("is-invalid");
  }

  /* ── query parameters: error flash + post-login destination ───── */
  var params = new URLSearchParams(window.location.search);
  var errorCode = params.get("error");
  var nextTarget = sameSitePath(params.get("next"));
  nextField.value = nextTarget;
  if (errorCode && MESSAGES[errorCode]) {
    showError(MESSAGES[errorCode]);
    if (errorCode === "invalid" || errorCode === "validation") markInvalid();
  }

  /* ── password visibility ──────────────────────────────────────── */
  togglePw.addEventListener("click", function () {
    var showing = password.type === "text";
    password.type = showing ? "password" : "text";
    eyeOpen.style.display = showing ? "" : "none";
    eyeOff.style.display  = showing ? "none" : "";
    togglePw.setAttribute("aria-pressed", showing ? "false" : "true");
    var lbl = showing ? "Show password" : "Hide password";
    togglePw.setAttribute("aria-label", lbl);
    togglePw.setAttribute("title", lbl);
    password.focus();
  });

  /* ── inert affordances (present in the design, not configured) ── */
  var forgot = document.getElementById("forgotLink");
  if (forgot) {
    forgot.addEventListener("click", function (ev) {
      ev.preventDefault();
      showError("Password recovery is not enabled. Contact your administrator.");
    });
  }
  var sso = document.getElementById("ssoBtn");
  if (sso) {
    sso.addEventListener("click", function () {
      showError("Single sign-on is not enabled on this deployment.");
    });
  }

  identifier.addEventListener("input", clearInvalid);
  password.addEventListener("input", clearInvalid);

  /* ── already authenticated? go straight through ───────────────── */
  fetch("/api/auth/me", {
    credentials: "same-origin",
    headers: { "Accept": "application/json" },
    cache: "no-store"
  }).then(function (res) {
    if (res && res.ok) {
      window.location.replace(nextTarget);
    }
  }).catch(function () { /* stay on the form */ });

  /* ── submit ───────────────────────────────────────────────────── */
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearError();
    clearInvalid();

    var user = identifier.value.trim();
    var pass = password.value;
    if (!user || !pass) {
      showError(MESSAGES.validation);
      markInvalid();
      (user ? password : identifier).focus();
      return;
    }

    setBusy(true);
    fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json",
                 "Accept": "application/json" },
      cache: "no-store",
      body: JSON.stringify({
        username: user,
        password: pass,
        remember: !!(remember && remember.checked),
        next: nextTarget
      })
    }).then(function (res) {
      if (res.ok) {
        return res.json().then(function (data) {
          window.location.replace(sameSitePath(data && data.redirect));
        });
      }
      if (res.status === 401 || res.status === 403) {
        showError(MESSAGES.invalid);
        markInvalid();
        password.value = "";
        password.focus();
      } else if (res.status === 400 || res.status === 422) {
        showError(MESSAGES.validation);
        markInvalid();
      } else if (res.status === 429) {
        showError(MESSAGES.rate);
      } else {
        showError(MESSAGES.unavailable);
      }
      setBusy(false);
    }).catch(function () {
      showError(MESSAGES.unavailable);
      setBusy(false);
    });
  });
})();
