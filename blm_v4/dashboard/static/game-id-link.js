/* Shared PokerBet event link renderer for every user-facing GameId. */
(function (root) {
  "use strict";

  const EVENT_PREFIX = "https://www.pokerbet.co.za/en/sports/live/event-view/"
    + "Basketball/Virtual%20Matches/18296900-betual-tbsl/";
  const GAME_ID_PATTERN = /^(\d+)(?:#i\d+)?$/;

  function escapeHTML(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[char]));
  }

  function eventId(value) {
    if (value === null || value === undefined) return null;
    const match = GAME_ID_PATTERN.exec(String(value).trim());
    if (!match || !/[1-9]/.test(match[1])) return null;
    return match[1];
  }

  function url(value) {
    const id = eventId(value);
    return id === null ? null : EVENT_PREFIX + encodeURIComponent(id);
  }

  function html(value, className) {
    if (value === null || value === undefined || value === "") return "";
    const label = escapeHTML(value);
    const href = url(value);
    if (href === null) return label;
    const classes = ["game-id-link"];
    if (className) classes.push(String(className).split(/\s+/)
      .map(escapeHTML).join(" "));
    return `<a class="${classes.join(" ")}" href="${href}" `
      + `target="_blank" rel="noopener noreferrer">${label}</a>`;
  }

  const api = Object.freeze({ eventId, url, html });
  root.GameIdLink = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
