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
    // WebRTC ICE gathering runs over raw UDP and ignores Chrome's configured
    // HTTP(S) proxy entirely, so a page can probe RTCPeerConnection to read
    // the real local/host IP straight past APPLY_PROXY's tunnel (confirmed
    // leaking via scripts/fingerprint_check.py -- the command-line
    // --force-webrtc-ip-handling-policy flag only restricts UDP relative to
    // an *active* proxy, so it's a no-op on the direct connection most jobs
    // run on). Nothing in this pipeline uses WebRTC/getUserMedia, so it's
    // safe to remove outright rather than try to filter ICE candidates.
    //
    // Blink installs the RTCPeerConnection binding onto window *after*
    // document_start content scripts run, which silently clobbers a
    // one-shot override here (confirmed: deleting/reassigning it at
    // document_start does nothing, while the identical code run later via
    // CDP Page.evaluate works fine). So reapply on every later lifecycle
    // point available before page scripts can realistically run first.
    const killWebRTC = () => {
      for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "mozRTCPeerConnection", "RTCDataChannel"]) {
        try {
          delete window[name];
        } catch (e) {}
        try {
          Object.defineProperty(window, name, { get: () => undefined, configurable: true });
        } catch (e) {}
      }
    };
    killWebRTC();
    queueMicrotask(killWebRTC);
    document.addEventListener("readystatechange", killWebRTC);
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
