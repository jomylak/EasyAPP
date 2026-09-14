// Runs in the page's own (MAIN world) JS context at document_start, on every
// frame -- see manifest.json's "world": "MAIN", a Chrome 111+ feature that
// lets a Manifest V3 content script write directly to the real window object
// instead of an isolated copy the page's own scripts can't see.
//
// Scope: only the JS-level gaps a bare CDP session has relative to a real
// user's Chrome profile (per https://alterlab.io/blog/playwright-bot-detection-what-actually-works-in-2026):
// missing window.chrome.* objects and an empty navigator.plugins list.
// Deliberately NOT attempting WebGL/canvas/audio fingerprint spoofing here --
// a naive override is itself a detectable inconsistency (real Chrome's noise
// has a very specific shape), and this project has no way to validate a
// spoof is actually convincing. --disable-blink-features=AutomationControlled
// (see chrome.py) already covers navigator.webdriver at the Chromium level;
// the reinforcement below is cheap insurance in case a future Chrome version
// narrows that flag's coverage, not the primary fix for it.
(function () {
  try {
    Object.defineProperty(Navigator.prototype, "webdriver", {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        connect: () => {},
        sendMessage: () => {},
        onMessage: { addListener: () => {} },
      };
    }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = function () {
        const t = performance.timing;
        return {
          requestTime: t.navigationStart / 1000,
          startLoadTime: t.navigationStart / 1000,
          commitLoadTime: t.responseStart / 1000,
          finishDocumentLoadTime: t.domContentLoadedEventEnd / 1000,
          finishLoadTime: t.loadEventEnd / 1000,
          firstPaintTime: 0,
          navigationType: "Other",
          wasFetchedViaSpdy: false,
          wasNpnNegotiated: false,
          npnNegotiatedProtocol: "unknown",
          wasAlternateProtocolAvailable: false,
          connectionInfo: "unknown",
        };
      };
    }
    if (!window.chrome.csi) {
      window.chrome.csi = function () {
        return { onloadT: Date.now(), pageT: Date.now(), startE: Date.now(), tran: 15 };
      };
    }
  } catch (e) {}

  try {
    if (navigator.plugins.length === 0) {
      const fakePlugin = {
        name: "Chrome PDF Plugin",
        filename: "internal-pdf-viewer",
        description: "Portable Document Format",
        length: 1,
      };
      Object.defineProperty(navigator, "plugins", { get: () => [fakePlugin] });
      Object.defineProperty(navigator, "mimeTypes", {
        get: () => [{ type: "application/pdf", suffixes: "pdf", description: "" }],
      });
    }
  } catch (e) {}
})();
