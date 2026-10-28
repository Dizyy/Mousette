// Mousette web client. Speaks protocol v1 (see PROTOCOL.md) over /ws.
(() => {
  'use strict';

  const V = 1;
  const $ = (id) => document.getElementById(id);

  // ---------- Storage (all access guarded: private mode can throw) ----------
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  // keysHidden: data-action names of shortcut keys (Trackpad tab) the user turned off.
  // keysOrder: data-action names in row order ([] = the order in index.html).
  // barPos: tab bar 'top' | 'bottom'. keysPos: shortcut keys 'top' | 'bottom' | 'off'.
  const DEFAULT_SETTINGS = {
    sens: 1.2, scroll: 1.0, strip: false, accent: '#9cc5a1',
    keysHidden: ['sendtext', 'search', 'alttab', 'desktop'], keysOrder: [],
    barPos: 'top', keysPos: 'bottom',
  };
  let settings = { ...DEFAULT_SETTINGS };
  try { settings = { ...DEFAULT_SETTINGS, ...JSON.parse(store.get('pm.settings') || '{}') }; } catch { /* keep defaults */ }
  const saveSettings = () => store.set('pm.settings', JSON.stringify(settings));

  const appleTouch = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const homeScreenApp = navigator.standalone === true
    || matchMedia('(display-mode: standalone), (display-mode: fullscreen)').matches;

  // The PIN lives in memory only. Older versions saved it; clear that.
  // After a correct PIN the server hands out a token instead, which is saved: iOS often
  // reloads the page after the phone was locked, and the token logs in again without the PIN.
  // It stops working when the server restarts, just like the PIN.
  store.remove('pm.pin');
  let currentPin = null;
  const getPin = () => currentPin;
  const setPin = (pin) => { currentPin = pin; };
  const forgetPin = () => { currentPin = null; };
  const getToken = () => store.get('pm.token');
  const setToken = (token) => store.set('pm.token', token);
  const forgetToken = () => store.remove('pm.token');

  // ---------- Encryption (see server/encryption.py and PROTOCOL.md) ----------
  // The QR code carries the PC's key after "#", so it never travels over the network.
  // With it, each connection agrees on a session key and every frame is encrypted and
  // numbered (NaCl secretbox via tweetnacl.js). A typed PIN alone connects unencrypted.
  const SESSION_LABEL = new TextEncoder().encode('Mousette session v1');
  const FROM_CLIENT = 0;
  const FROM_SERVER = 1;
  const canEncrypt = typeof nacl !== 'undefined';

  function b64encode(bytes) {
    let s = '';
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function b64decode(text) {
    const s = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((text.length + 3) % 4));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  }
  function getKey() {
    const saved = store.get('pm.key');
    if (!saved || !canEncrypt) return null;
    try { const key = b64decode(saved); return key.length === 32 ? key : null; } catch { return null; }
  }
  const setKey = (text) => store.set('pm.key', text);
  const forgetKey = () => store.remove('pm.key');

  function frameNonce(direction, counter) {
    const nonce = new Uint8Array(24);
    nonce[0] = direction;
    for (let i = 1; i <= 8; i++) { nonce[i] = counter % 256; counter = Math.floor(counter / 256); }
    return nonce;
  }
  function makeSession(key, serverNonce, clientNonce) {
    const all = new Uint8Array(SESSION_LABEL.length + key.length + serverNonce.length + clientNonce.length);
    all.set(SESSION_LABEL, 0);
    all.set(key, SESSION_LABEL.length);
    all.set(serverNonce, SESSION_LABEL.length + key.length);
    all.set(clientNonce, SESSION_LABEL.length + key.length + serverNonce.length);
    return { key: nacl.hash(all).subarray(0, 32), sent: 0, received: 0 };
  }

  // Sends on one socket, encrypted once its handshake is done.
  function sockSend(sock, msg) {
    const text = JSON.stringify({ v: V, ...msg });
    if (!sock.session) { sock.send(text); return; }
    const s = sock.session;
    sock.send(nacl.secretbox(new TextEncoder().encode(text), frameNonce(FROM_CLIENT, s.sent++), s.key));
  }

  // The PC's IP address, from auth_ok. After a long sleep the phone sometimes can't look up
  // mousette.local any more; then the connection goes to the IP address instead.
  const getDirect = () => store.get('pm.direct');
  let viaDirect = false;
  function failedToOpen() {
    const direct = getDirect();
    if (direct && direct !== location.host) viaDirect = !viaDirect; // alternate name and IP
  }

  // ---------- Connection ----------
  const statusEl = $('status');
  const statusText = $('status-text');
  let ws = null;
  let authed = false;
  let reconnectDelay = 500;
  let reconnectTimer = null;
  let pingTimer = null;
  let lastPong = 0;
  let pingId = 0;
  let pinJustEntered = false; // typed just now, rather than reused for a reconnect
  let warnedUnencrypted = false;
  const CLOSE_BAD_FRAME = 4032; // the server couldn't decrypt what we sent

  function setStatus(connected, text) {
    statusEl.classList.toggle('connected', connected);
    statusText.textContent = text;
  }

  function rawSend(msg) {
    if (ws && authed && ws.readyState === WebSocket.OPEN) {
      sockSend(ws, msg);
      return true;
    }
    return false;
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 5000);
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      if (Date.now() - lastPong > 6000) { dropConnection(true); return; }
      rawSend({ type: 'ping', id: ++pingId });
    }, 2000);
  }

  function stopPing() { clearInterval(pingTimer); pingTimer = null; }

  // Abandon the current socket immediately (iOS often leaves dead sockets open after sleep).
  function dropConnection(reconnect) {
    const sock = ws;
    ws = null;
    authed = false;
    stopPing();
    if (sock) { try { sock.close(); } catch { /* ignore */ } }
    setStatus(false, reconnect ? 'Reconnecting…' : 'Not connected');
    if (reconnect) scheduleReconnect();
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (ws) return;
    const pin = getPin();
    // A freshly entered or scanned PIN wins over an old token.
    const token = pin ? null : getToken();
    if (!pin && !token) { setStatus(false, 'Enter PIN'); showPin(); return; }

    setStatus(false, 'Connecting…');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const target = viaDirect && getDirect() ? getDirect() : location.host;
    const sock = new WebSocket(`${proto}//${target}/ws`);
    sock.binaryType = 'arraybuffer';
    ws = sock;
    const key = getKey();
    let clientNonce = null;
    const sendAuth = () => sockSend(sock, token ? { type: 'auth', token } : { type: 'auth', pin });
    let authRejected = false;
    let opened = false;
    // After the phone wakes up, a new socket sometimes hangs without opening or failing.
    setTimeout(() => {
      if (ws !== sock || authed || authRejected) return;
      if (!opened) failedToOpen();
      dropConnection(true);
    }, 6000);

    sock.onopen = () => {
      opened = true;
      if (!key) { sendAuth(); return; }
      clientNonce = nacl.randomBytes(16);
      sockSend(sock, { type: 'enc_hello', nonce: b64encode(clientNonce) });
    };
    sock.onmessage = (event) => {
      let text = event.data;
      if (typeof text !== 'string') {
        if (!sock.session) return;
        const s = sock.session;
        const plain = nacl.secretbox.open(new Uint8Array(text), frameNonce(FROM_SERVER, s.received++), s.key);
        if (!plain) { if (ws === sock) dropConnection(true); return; } // tampered or out of order
        text = new TextDecoder().decode(plain);
      } else if (sock.session) {
        return; // never trust an unencrypted message on an encrypted connection
      }
      let msg;
      try { msg = JSON.parse(text); } catch { return; }
      if (msg.type === 'enc_challenge') {
        if (!clientNonce || sock.session) return;
        sock.session = makeSession(key, b64decode(msg.nonce), clientNonce);
        sendAuth();
        return;
      }
      switch (msg.type) {
        case 'auth_ok':
          authed = true;
          pinJustEntered = false;
          if (msg.direct) store.set('pm.direct', msg.direct);
          if (msg.token) {
            setToken(msg.token);
            if (getToken() === msg.token) forgetPin(); // keep the PIN if storage is blocked
          }
          reconnectDelay = 500;
          lastPong = Date.now();
          setStatus(true, sock.session ? 'Connected · encrypted' : 'Connected');
          if (!sock.session && !warnedUnencrypted) {
            warnedUnencrypted = true;
            showToast('Not encrypted. Scan the QR code on the PC to encrypt the connection.', { ms: 6000 });
          }
          hidePin();
          sendAccent();
          startPing();
          maybeShowHomeTip();
          break;
        case 'auth_failed':
          authRejected = true;
          if (msg.reason === 'token_expired') {
            forgetToken();
            showPin('Mousette on the PC was restarted. Enter the new PIN from the Mousette window.');
          } else if (msg.reason === 'locked_out') {
            showPin(`Too many wrong PINs. Try again in ${msg.retry_after || 60} s.`);
          } else {
            forgetToken();
            forgetPin();
            showPin(pinJustEntered
              ? 'Wrong PIN. Check the Mousette window on your PC.'
              : 'The PIN on the PC has changed. Enter the new PIN from the Mousette window.');
          }
          pinJustEntered = false;
          break;
        case 'pong':
          lastPong = Date.now();
          break;
        case 'action_ok':
          onActionOk(msg.ref);
          break;
        case 'accent': // picked in the PC window
          if (ACCENTS.some(([, c]) => c === msg.color) && settings.accent !== msg.color) {
            settings.accent = msg.color;
            renderSettings();
            saveSettings();
          }
          break;
        case 'error':
          console.warn('Mousette server error:', msg.code, msg.message);
          if (PC_ACTIONS.includes(msg.ref)) showToast(msg.message, { bad: true });
          break;
      }
    };
    sock.onclose = (event) => {
      if (ws !== sock) return; // already dropped
      ws = null;
      authed = false;
      stopPing();
      if (event.code === CLOSE_BAD_FRAME && key) {
        // The PC has a different key now (a new key, or another PC): pair again.
        forgetKey();
        cleanAddress();
        authRejected = true;
        showPin('Mousette has a new encryption key. Scan the QR code on the PC again, or enter the PIN to connect without encryption.');
      }
      if (authRejected) {
        setStatus(false, 'Not connected');
      } else {
        if (!opened) failedToOpen();
        setStatus(false, 'Reconnecting…');
        scheduleReconnect();
      }
    };
  }

  // Coming back from a locked phone or another app: reconnect straight away instead of
  // waiting for the backoff timer, and check an open socket really survived the sleep.
  // iOS doesn't always send visibilitychange (home-screen app, back/forward cache),
  // so pageshow, focus and online do the same.
  let lastWake = 0;
  function wake() {
    if (document.visibilityState !== 'visible') return;
    const now = Date.now();
    if (now - lastWake < 1000) return; // these events often fire together
    lastWake = now;
    reconnectDelay = 500;
    if (!ws) { if (getPin() || getToken()) connect(); return; }
    if (!authed) return; // still connecting
    rawSend({ type: 'ping', id: ++pingId });
    setTimeout(() => { if (ws && authed && lastPong < now) dropConnection(true); }, 2000);
  }
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('pageshow', wake);
  window.addEventListener('focus', wake);
  window.addEventListener('online', wake);

  // ---------- Outgoing input, with move/scroll coalesced per animation frame ----------
  const pendingMove = { dx: 0, dy: 0 };
  const pendingScroll = { dx: 0, dy: 0 };
  let pendingZoom = 0;
  let flushScheduled = false;
  const round2 = (n) => Math.round(n * 100) / 100;

  function flush() {
    flushScheduled = false;
    if (pendingMove.dx || pendingMove.dy) {
      rawSend({ type: 'move', dx: round2(pendingMove.dx), dy: round2(pendingMove.dy) });
      pendingMove.dx = pendingMove.dy = 0;
    }
    // Smallest wheel step Windows understands is 1/120 of a notch.
    if (Math.abs(pendingScroll.dx) >= 1 / 120 || Math.abs(pendingScroll.dy) >= 1 / 120) {
      rawSend({ type: 'scroll', dx: round2(pendingScroll.dx), dy: round2(pendingScroll.dy) });
      pendingScroll.dx = pendingScroll.dy = 0;
    }
    if (Math.abs(pendingZoom) >= 1 / 120) {
      rawSend({ type: 'zoom', steps: round2(pendingZoom) });
      pendingZoom = 0;
    }
  }

  function scheduleFlush() {
    if (!flushScheduled) { flushScheduled = true; requestAnimationFrame(flush); }
  }

  // Discrete events flush queued movement first so ordering is preserved.
  function send(msg) {
    flush();
    return rawSend(msg);
  }

  // ---------- Trackpad gestures ----------
  const pad = $('trackpad');
  const TAP_SLOP = 10;        // px a finger may wander and still count as a tap
  const HOLD_MS = 400;        // press this long without moving to start a click-drag
  const HOLD_SLOP = 4;        // px of movement that cancels a pending click-drag
  const MULTI_TAP_MS = 350;   // max duration of a two-, three- or four-finger tap
  const SCROLL_PER_PX = 1 / 40; // wheel notches per px of two-finger travel at speed 1
  const PINCH_PX = 24;        // change in finger spread that makes two fingers a pinch
  const ZOOM_PER_LOG = 6;     // zoom notches per e-fold of the finger spread (~4 per doubling)
  const SWIPE_PX = 45;        // three/four-finger travel that fires a swipe
  const SWITCH_STEP_PX = 70;  // sideways travel per app in the three-finger app switcher
  const COAST_DECAY_MS = 300; // momentum scrolling slows down by e every this many ms

  let mode = 'idle';          // idle | pending | move | drag | multi
  let start = null;           // {x, y, t}
  let last = null;            // {x, y, t}
  let speed = 0;              // smoothed finger speed, px/ms
  let holdTimer = null;
  // Two or more fingers: `fingers` is the most seen at once in this gesture; `kind` is
  // decided by the first clear movement (scroll or pinch for two, swipe or switcher for 3-4).
  let multi = null;           // {fingers, t, start, last, spread, kind, switchX}

  function centroid(touches) {
    let x = 0; let y = 0;
    for (const t of touches) { x += t.clientX; y += t.clientY; }
    return { x: x / touches.length, y: y / touches.length };
  }

  function spread(touches) {
    return Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
  }

  // Momentum scrolling: after a flick the page keeps going and slows down, like Windows.
  let coastV = null;          // {dx, dy} notches per ms while coasting
  let scrollV = { dx: 0, dy: 0, t: 0 };
  function trackScrollSpeed(ddx, ddy, now) {
    const dt = Math.max(1, now - scrollV.t);
    const a = scrollV.t && dt < 100 ? 0.6 : 0;
    scrollV = { dx: a * scrollV.dx + (1 - a) * ddx / dt, dy: a * scrollV.dy + (1 - a) * ddy / dt, t: now };
  }
  function startCoast(now) {
    const v = scrollV;
    scrollV = { dx: 0, dy: 0, t: 0 };
    if (now - v.t > 80 || Math.hypot(v.dx, v.dy) < 0.01) return; // slow scroll or finger stopped: no glide
    coastV = { dx: v.dx, dy: v.dy };
    let prev = performance.now();
    const step = (t) => {
      if (!coastV) return;
      const dt = Math.min(50, t - prev);
      prev = t;
      const f = Math.exp(-dt / COAST_DECAY_MS);
      // Distance covered while the speed decays from v to v*f.
      pendingScroll.dx += coastV.dx * COAST_DECAY_MS * (1 - f);
      pendingScroll.dy += coastV.dy * COAST_DECAY_MS * (1 - f);
      coastV.dx *= f; coastV.dy *= f;
      flush();
      if (Math.hypot(coastV.dx, coastV.dy) < 0.001) { coastV = null; return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function stopCoast() { coastV = null; }

  // Three- and four-finger swipes do what they do on a Windows touchpad.
  function swipeAction(fingers, dir) {
    if (dir === 'up') return send({ type: 'hotkey', keys: ['win', 'tab'] });       // Task view
    if (dir === 'down') return send({ type: 'hotkey', keys: ['win', 'd'] });       // Show desktop
    if (fingers >= 4) {
      // Content follows the fingers: swipe left to reach the desktop on the right.
      return send({ type: 'hotkey', keys: ['ctrl', 'win', dir === 'left' ? 'right' : 'left'] });
    }
    // Three fingers sideways: the app switcher stays open and follows the hand; lift picks.
    send({ type: 'hotkey', keys: ['ctrl', 'alt', 'tab'] });
    multi.kind = 'switcher';
    multi.switchX = multi.last.x;
  }

  // Pointer acceleration: slow finger => < 1x for precision, fast swipe => several x.
  function gain(v) {
    return settings.sens * (0.5 + 1.8 * Math.pow(Math.min(v, 2.5), 1.3));
  }

  function endDrag() {
    if (mode === 'drag') send({ type: 'mouse_up', button: 'left' });
    pad.classList.remove('dragging');
  }

  function reset() {
    clearTimeout(holdTimer);
    mode = 'idle';
    pad.classList.remove('active');
  }

  pad.addEventListener('touchstart', (e) => {
    e.preventDefault();
    pad.classList.add('active');
    stopCoast();
    const now = e.timeStamp;

    if (e.touches.length === 1 && mode === 'idle') {
      const t = e.touches[0];
      start = { x: t.clientX, y: t.clientY, t: now };
      last = { ...start };
      speed = 0;
      mode = 'pending';
      holdTimer = setTimeout(() => {
        if (mode !== 'pending') return;
        mode = 'drag';
        pad.classList.add('dragging');
        send({ type: 'mouse_down', button: 'left' });
      }, HOLD_MS);
      return;
    }

    if (e.touches.length >= 2) {
      clearTimeout(holdTimer);
      if (mode === 'drag') return; // keep dragging; ignore extra fingers
      const c = centroid(e.touches);
      if (mode !== 'multi') {
        mode = 'multi';
        multi = { fingers: 0, t: now, start: c, last: c, spread: spread(e.touches), kind: null, switchX: 0 };
      } else if (!multi.kind) {
        multi.start = c; // fingers rarely land together: measure from the last one down
        multi.spread = spread(e.touches);
      }
      if (!multi.kind) multi.fingers = Math.max(multi.fingers, Math.min(e.touches.length, 4));
      multi.last = c;
    }
  }, { passive: false });

  pad.addEventListener('touchmove', (e) => {
    e.preventDefault();
    const now = e.timeStamp;

    if (mode === 'multi') {
      if (e.touches.length < 2) return;
      const c = centroid(e.touches);
      const mx = c.x - multi.start.x;
      const my = c.y - multi.start.y;
      if (!multi.kind) {
        if (multi.fingers === 2) {
          if (Math.abs(spread(e.touches) - multi.spread) > PINCH_PX) {
            multi.kind = 'pinch'; // zooms from the starting spread, so no movement is lost
          } else if (Math.hypot(mx, my) > TAP_SLOP) {
            multi.kind = 'scroll';
          }
        } else if (Math.hypot(mx, my) > SWIPE_PX) {
          multi.kind = 'swipe';
          const dir = Math.abs(mx) > Math.abs(my) ? (mx < 0 ? 'left' : 'right') : (my < 0 ? 'up' : 'down');
          swipeAction(multi.fingers, dir);
        }
      }
      if (multi.kind === 'scroll') {
        // Natural scrolling: content follows the fingers.
        const k = SCROLL_PER_PX * settings.scroll;
        const ddy = (c.y - multi.last.y) * k;
        const ddx = -(c.x - multi.last.x) * k;
        pendingScroll.dy += ddy;
        pendingScroll.dx += ddx;
        trackScrollSpeed(ddx, ddy, now);
        scheduleFlush();
      } else if (multi.kind === 'pinch' && e.touches.length === 2) {
        const d = spread(e.touches);
        pendingZoom += Math.log(d / multi.spread) * ZOOM_PER_LOG;
        multi.spread = d;
        scheduleFlush();
      } else if (multi.kind === 'switcher') {
        while (c.x - multi.switchX > SWITCH_STEP_PX) { send({ type: 'key', name: 'right' }); multi.switchX += SWITCH_STEP_PX; }
        while (multi.switchX - c.x > SWITCH_STEP_PX) { send({ type: 'key', name: 'left' }); multi.switchX -= SWITCH_STEP_PX; }
      }
      multi.last = c;
      return;
    }

    if (e.touches.length !== 1 || !last) return;
    const t = e.touches[0];
    const dx = t.clientX - last.x;
    const dy = t.clientY - last.y;
    const dt = Math.max(1, now - last.t);
    last = { x: t.clientX, y: t.clientY, t: now };

    if (mode === 'pending') {
      const moved = Math.hypot(t.clientX - start.x, t.clientY - start.y);
      // A slow, deliberate move must not turn into a click-drag.
      if (moved > HOLD_SLOP) clearTimeout(holdTimer);
      if (moved <= TAP_SLOP) return;
      mode = 'move';
    }
    if (mode !== 'move' && mode !== 'drag') return;

    speed = 0.6 * speed + 0.4 * (Math.hypot(dx, dy) / dt);
    const g = gain(speed);
    pendingMove.dx += dx * g;
    pendingMove.dy += dy * g;
    scheduleFlush();
  }, { passive: false });

  function onTouchEnd(e) {
    e.preventDefault();
    if (mode === 'multi' && e.touches.length >= 2) {
      multi.last = centroid(e.touches);
      if (multi.kind === 'pinch') multi.spread = spread(e.touches);
      return;
    }
    if (e.touches.length > 0) return; // wait until every finger is up

    const now = e.timeStamp;
    if (e.type === 'touchend') {
      if (mode === 'pending') {
        // Each tap is a click; two quick taps reach Windows as a native double-click.
        send({ type: 'click', button: 'left' });
      } else if (mode === 'multi' && !multi.kind && now - multi.t < MULTI_TAP_MS) {
        if (multi.fingers === 2) send({ type: 'click', button: 'right' });
        else if (multi.fingers === 3) send({ type: 'click', button: 'middle' });
        else send({ type: 'hotkey', keys: ['win', 'n'] }); // notifications
      }
    }
    if (mode === 'multi' && multi.kind === 'switcher') send({ type: 'key', name: 'enter' });
    if (mode === 'multi' && multi.kind === 'scroll' && e.type === 'touchend') startCoast(now);
    endDrag();
    flush();
    reset();
  }
  pad.addEventListener('touchend', onTouchEnd, { passive: false });
  pad.addEventListener('touchcancel', onTouchEnd, { passive: false });

  // ---------- Scroll strip (optional, right edge) ----------
  // One-finger drag scrolls exactly like a two-finger drag on the trackpad.
  const strip = $('scroll-strip');
  let stripLast = null;

  strip.addEventListener('touchstart', (e) => {
    e.preventDefault();
    stopCoast();
    strip.classList.add('active');
    stripLast = e.targetTouches[0].clientY;
  }, { passive: false });

  strip.addEventListener('touchmove', (e) => {
    e.preventDefault();
    if (stripLast === null || !e.targetTouches.length) return;
    const y = e.targetTouches[0].clientY;
    const ddy = (y - stripLast) * SCROLL_PER_PX * settings.scroll;
    pendingScroll.dy += ddy;
    trackScrollSpeed(0, ddy, e.timeStamp);
    stripLast = y;
    scheduleFlush();
  }, { passive: false });

  function onStripEnd(e) {
    e.preventDefault();
    if (e.targetTouches.length) { stripLast = e.targetTouches[0].clientY; return; }
    stripLast = null;
    strip.classList.remove('active');
    flush();
    if (e.type === 'touchend') startCoast(e.timeStamp);
  }
  strip.addEventListener('touchend', onStripEnd, { passive: false });
  strip.addEventListener('touchcancel', onStripEnd, { passive: false });

  // Block pinch-zoom and other Safari page gestures.
  document.addEventListener('gesturestart', (e) => e.preventDefault());
  document.addEventListener('touchmove', (e) => {
    // Sliders and the settings card (it scrolls on small screens) keep their own touch moves.
    if (!e.target.closest('input[type="range"], .sheet-card')) e.preventDefault();
  }, { passive: false });

  // ---------- Hidden keyboard input ----------
  const kbd = $('kbd');
  const typebox = $('typebox');
  const typeboxText = $('typebox-text');
  // Pre-filled text so iOS has something to delete (backspace in an empty field sends nothing).
  // Underscores rather than spaces avoid iOS's "double space inserts a period" shortcut.
  const SENTINEL = '________';
  let composing = false;

  function resetKbd() {
    kbd.value = SENTINEL;
    try { kbd.setSelectionRange(SENTINEL.length, SENTINEL.length); } catch { /* ignore */ }
  }

  function processKbd() {
    const v = kbd.value;
    if (v === SENTINEL) return;
    // Diff against the sentinel: common prefix + common suffix, the rest was edited.
    let p = 0;
    while (p < v.length && p < SENTINEL.length && v[p] === SENTINEL[p]) p++;
    let s = 0;
    while (s < v.length - p && s < SENTINEL.length - p
           && v[v.length - 1 - s] === SENTINEL[SENTINEL.length - 1 - s]) s++;
    const removed = SENTINEL.length - p - s;
    const inserted = v.slice(p, v.length - s);
    for (let i = 0; i < removed; i++) send({ type: 'key', name: 'backspace' });
    if (inserted) send({ type: 'type_text', text: inserted });
    resetKbd();
  }

  kbd.addEventListener('compositionstart', () => { composing = true; });
  kbd.addEventListener('compositionend', () => { composing = false; processKbd(); });
  kbd.addEventListener('input', () => { if (!composing) processKbd(); });
  kbd.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.keyCode === 13) {
      e.preventDefault();
      send({ type: 'key', name: 'enter' });
    }
  });
  // Pasting into the keyboard goes onto the PC clipboard and is pasted there in one go,
  // instead of being typed out letter by letter.
  kbd.addEventListener('paste', (e) => {
    const text = e.clipboardData && e.clipboardData.getData('text/plain');
    if (!text) return;
    e.preventDefault();
    pasteOnPc(text);
  });
  function showKbdState(open) {
    typebox.classList.toggle('on', open);
    typeboxText.textContent = open ? 'Keyboard open · tap to close' : "Tap to type with your phone's keyboard";
  }
  kbd.addEventListener('focus', () => showKbdState(true));
  kbd.addEventListener('blur', () => showKbdState(false));

  function toggleKeyboard() {
    if (document.activeElement === kbd) {
      kbd.blur();
    } else {
      resetKbd();
      kbd.focus(); // must run inside the tap handler or iOS won't show the keyboard
    }
  }

  // ---------- Buttons: tabs, quick actions, shortcut keys, deck, media ----------
  const hotkey = (...keys) => () => send({ type: 'hotkey', keys });
  const key = (name) => () => send({ type: 'key', name });
  const actions = {
    left: () => send({ type: 'click', button: 'left' }),
    right: () => send({ type: 'click', button: 'right' }),
    keyboard: toggleKeyboard,
    copy: hotkey('ctrl', 'c'),
    paste: hotkey('ctrl', 'v'),
    sendtext: openSendSheet,
    search: hotkey('win', 's'),
    alttab: hotkey('alt', 'tab'),
    desktop: hotkey('win', 'd'),
    newtab: hotkey('ctrl', 't'),
    reopentab: hotkey('ctrl', 'shift', 't'),
    closetab: hotkey('ctrl', 'w'),
    screenshot: hotkey('win', 'shift', 's'),
    fullscreen: hotkey('f'),
    esc: key('esc'),
    tab: key('tab'),
    enter: key('enter'),
    backspace: key('backspace'),
    delete: key('delete'),
    home: key('home'),
    end: key('end'),
    pageup: key('page_up'),
    pagedown: key('page_down'),
    arrowleft: key('left'),
    arrowup: key('up'),
    arrowdown: key('down'),
    arrowright: key('right'),
    // The server starts Task Manager itself, so Windows shows no UAC prompt the phone can't answer.
    taskmgr: () => pcAction('task_manager'),
    shutdown: () => { if (window.confirm('Shut down PC?')) pcAction('shutdown'); },
  };

  const app = $('app');
  const tabButtons = [...document.querySelectorAll('.tab')];
  const views = [...document.querySelectorAll('.view')];
  const quick = $('quick');
  const quickBtn = $('quick-btn');

  function showTab(name) {
    for (const b of tabButtons) b.classList.toggle('on', b.dataset.tab === name);
    for (const v of views) v.hidden = v.dataset.view !== name;
    setQuick(false);
  }

  function setQuick(open) {
    quick.hidden = !open;
    quickBtn.classList.toggle('on', open);
    quickBtn.setAttribute('aria-expanded', String(open));
  }

  function runButton(btn) {
    if (btn.dataset.tab) { showTab(btn.dataset.tab); return; }
    if (btn === quickBtn) { setQuick(quick.hidden); return; }
    if (btn.dataset.key) send({ type: 'key', name: btn.dataset.key });
    else if (actions[btn.dataset.action]) actions[btn.dataset.action]();
    if (btn.closest('#quick')) setQuick(false);
  }

  for (const btn of document.querySelectorAll('.press')) {
    // Handling touchend (and cancelling touchstart) keeps the hidden input focused,
    // so the iOS keyboard stays open while using these buttons.
    btn.addEventListener('touchstart', (e) => {
      e.preventDefault();
      btn.classList.add('pressed');
    }, { passive: false });
    btn.addEventListener('touchend', (e) => {
      e.preventDefault();
      btn.classList.remove('pressed');
      const t = e.changedTouches[0];
      const r = btn.getBoundingClientRect();
      if (t.clientX >= r.left && t.clientX <= r.right && t.clientY >= r.top && t.clientY <= r.bottom) runButton(btn);
    }, { passive: false });
    btn.addEventListener('touchcancel', () => btn.classList.remove('pressed'));
    btn.addEventListener('click', () => runButton(btn)); // mouse / desktop browsers
  }

  // Any touch outside Quick actions closes it, without swallowing that touch.
  const closeQuickOutside = (e) => {
    if (!quick.hidden && !e.target.closest('#quick, #quick-btn')) setQuick(false);
  };
  document.addEventListener('touchstart', closeQuickOutside, { capture: true, passive: true });
  document.addEventListener('mousedown', closeQuickOutside, true);
  showTab('pad');

  // ---------- Keep layout above the iOS keyboard ----------
  const rootStyle = document.documentElement.style;
  function fitViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    // iOS can leave a home-screen app's page short of the screen bottom (by about the status bar
    // height), and the page can't draw there. That strip already clears the home bar, so the
    // bottom row then needs no gap of its own. A shorter page with the keyboard open doesn't count.
    if (appleTouch && homeScreenApp) {
      const full = matchMedia('(orientation: landscape)').matches
        ? Math.min(screen.width, screen.height) : Math.max(screen.width, screen.height);
      const missing = full - vv.height;
      document.documentElement.classList.toggle('short-screen', missing > 20 && missing < 150);
    }
    rootStyle.setProperty('--app-height', `${vv.height}px`);
    rootStyle.setProperty('--app-top', `${vv.offsetTop}px`);
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', fitViewport);
    window.visualViewport.addEventListener('scroll', fitViewport);
    fitViewport();
  }

  // ---------- Settings sheet ----------
  const settingsSheet = $('settings');
  const sensInput = $('sens');
  const scrollInput = $('scroll');
  const sensVal = $('sens-val');
  const scrollVal = $('scroll-val');

  const stripInput = $('strip');
  const keyrow = $('keyrow');
  const keyButtons = [...keyrow.children];

  // One tick box per shortcut key, built from the row itself so new keys appear here too.
  const barChoices = $('bar-choices');
  for (const btn of keyButtons) {
    const label = document.createElement('label');
    label.className = 'bar-choice';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.action = btn.dataset.action;
    box.addEventListener('change', () => {
      const hidden = new Set(settings.keysHidden);
      if (box.checked) hidden.delete(box.dataset.action); else hidden.add(box.dataset.action);
      settings.keysHidden = [...hidden];
      renderSettings();
      saveSettings();
    });
    label.append(box, btn.textContent);
    barChoices.append(label);
  }

  // Accent color presets. The first one is the default.
  const ACCENTS = [
    ['Sage', '#9cc5a1'], ['Blue', '#7fb8e0'], ['Violet', '#b5a3e0'], ['Amber', '#e3b866'],
    ['Rose', '#e3a0b4'], ['Coral', '#e39a8a'], ['Teal', '#7cc7bd'], ['Sand', '#d6c7a8'],
    ['Lime', '#b5d66e'], ['Sky', '#8fd0ec'], ['Orchid', '#d79be0'], ['Peach', '#f0b48a'],
    ['Slate', '#a9b4c2'], ['Cream', '#ece6da'],
  ];
  const swatches = $('swatches');
  for (const [name, color] of ACCENTS) {
    const swatch = document.createElement('button');
    swatch.className = 'swatch';
    swatch.setAttribute('role', 'radio');
    swatch.setAttribute('aria-label', name);
    swatch.style.setProperty('--c', color);
    swatch.dataset.color = color;
    swatch.addEventListener('click', () => { settings.accent = color; renderSettings(); saveSettings(); sendAccent(); });
    swatches.append(swatch);
  }

  // The PC window colours its QR code to match the accent.
  function sendAccent() { rawSend({ type: 'accent', color: settings.accent }); }

  function renderAccent() {
    const accent = ACCENTS.some(([, c]) => c === settings.accent) ? settings.accent : DEFAULT_SETTINGS.accent;
    document.documentElement.style.setProperty('--accent', accent);
    for (const s of swatches.children) s.setAttribute('aria-checked', String(s.dataset.color === accent));
  }

  // Shortcut key order: one row per key with a drag handle and up/down buttons.
  const DEFAULT_ORDER = keyButtons.map((btn) => btn.dataset.action);
  const orderList = $('order-list');
  const orderRows = {};
  const GRIP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm0 7a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm-1.5 8.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM18 5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm-1.5 8.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM18 19a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z"/></svg>';
  const UP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14l-6-6Z"/></svg>';
  const DOWN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6-1.41-1.41Z"/></svg>';

  // Saved order, cleaned up: unknown names dropped, keys missing from it added at the end.
  function keyOrder() {
    const saved = Array.isArray(settings.keysOrder) ? settings.keysOrder.filter((a) => DEFAULT_ORDER.includes(a)) : [];
    const order = [...new Set(saved)];
    for (const action of DEFAULT_ORDER) if (!order.includes(action)) order.push(action);
    return order;
  }

  function moveKey(action, step) {
    const order = keyOrder();
    const i = order.indexOf(action);
    const j = i + step;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    settings.keysOrder = order;
    renderSettings();
    saveSettings();
  }

  for (const btn of keyButtons) {
    const action = btn.dataset.action;
    const name = btn.getAttribute('aria-label') || btn.textContent;
    const row = document.createElement('li');
    row.className = 'order-row';
    row.dataset.action = action;
    row.innerHTML = `<button class="grip" aria-label="Drag to move ${name}">${GRIP}</button>`
      + `<span class="name"><span>${btn.textContent}</span><span class="tag">hidden</span></span>`
      + `<button class="move up" aria-label="Move ${name} up">${UP}</button>`
      + `<button class="move down" aria-label="Move ${name} down">${DOWN}</button>`;
    row.querySelector('.up').addEventListener('click', () => moveKey(action, -1));
    row.querySelector('.down').addEventListener('click', () => moveKey(action, 1));

    // Pointer events cover touch, pen and mouse: the row moves as the finger passes other rows' midpoints.
    // The neighbours are moved, not the dragged row: taking it out of the page would end the pointer capture.
    const grip = row.querySelector('.grip');
    const mid = (el) => { const b = el.getBoundingClientRect(); return b.top + b.height / 2; };
    let pointer = null;
    grip.addEventListener('pointerdown', (e) => {
      pointer = e.pointerId;
      grip.setPointerCapture(pointer);
      row.classList.add('dragging');
      e.preventDefault();
    });
    grip.addEventListener('pointermove', (e) => {
      if (e.pointerId !== pointer) return;
      let moved = false;
      while (row.nextElementSibling && e.clientY > mid(row.nextElementSibling)) {
        orderList.insertBefore(row.nextElementSibling, row);
        moved = true;
      }
      while (row.previousElementSibling && e.clientY < mid(row.previousElementSibling)) {
        orderList.insertBefore(row.previousElementSibling, row.nextElementSibling);
        moved = true;
      }
      if (!moved) return;
      settings.keysOrder = [...orderList.children].map((r) => r.dataset.action);
      renderSettings();
    });
    const endDrag = (e) => {
      if (e.pointerId !== pointer) return;
      pointer = null;
      row.classList.remove('dragging');
      saveSettings();
    };
    grip.addEventListener('pointerup', endDrag);
    grip.addEventListener('pointercancel', endDrag);
    orderRows[action] = row;
  }

  // Tab bar top/bottom and shortcut keys top/bottom/off.
  const segments = { barPos: $('bar-pos'), keysPos: $('keys-pos') };
  for (const [name, seg] of Object.entries(segments)) {
    for (const b of seg.children) {
      b.addEventListener('click', () => { settings[name] = b.dataset.v; renderSettings(); saveSettings(); });
    }
  }
  $('reset-bar').addEventListener('click', () => {
    settings.keysHidden = [...DEFAULT_SETTINGS.keysHidden];
    settings.keysOrder = [];
    settings.keysPos = DEFAULT_SETTINGS.keysPos;
    renderSettings();
    saveSettings();
  });

  function renderLayout() {
    const hidden = new Set(Array.isArray(settings.keysHidden) ? settings.keysHidden : []);
    let shown = 0;
    keyOrder().forEach((action, i) => {
      const btn = keyButtons.find((b) => b.dataset.action === action);
      keyrow.append(btn); // moving a node keeps its listeners
      btn.hidden = hidden.has(action);
      if (!btn.hidden) shown++;
      const row = orderRows[action];
      if (orderList.children[i] !== row) orderList.insertBefore(row, orderList.children[i] || null);
      row.classList.toggle('is-hidden', btn.hidden);
      row.querySelector('.up').disabled = i === 0;
      row.querySelector('.down').disabled = i === DEFAULT_ORDER.length - 1;
    });
    for (const box of barChoices.querySelectorAll('input')) box.checked = !hidden.has(box.dataset.action);
    // Up to 8 keys fit in one row; more wrap into two even rows.
    keyrow.style.setProperty('--key-cols', shown <= 8 ? shown : Math.ceil(shown / 2));
    const barPos = settings.barPos === 'bottom' ? 'bottom' : 'top';
    const keysPos = ['top', 'bottom', 'off'].includes(settings.keysPos) ? settings.keysPos : DEFAULT_SETTINGS.keysPos;
    app.dataset.bar = barPos;
    document.documentElement.dataset.bar = barPos; // messages (toasts) sit below a top tab bar
    app.dataset.keys = shown ? keysPos : 'off';
    for (const [name, seg] of Object.entries(segments)) {
      const value = name === 'barPos' ? barPos : keysPos;
      for (const b of seg.children) b.setAttribute('aria-checked', String(b.dataset.v === value));
    }
  }

  function renderSettings() {
    renderAccent();
    renderLayout();
    sensInput.value = settings.sens;
    scrollInput.value = settings.scroll;
    stripInput.checked = !!settings.strip;
    strip.hidden = !settings.strip;
    sensVal.textContent = `${Number(settings.sens).toFixed(1)}×`;
    scrollVal.textContent = `${Number(settings.scroll).toFixed(1)}×`;
  }

  sensInput.addEventListener('input', () => { settings.sens = Number(sensInput.value); renderSettings(); saveSettings(); });
  scrollInput.addEventListener('input', () => { settings.scroll = Number(scrollInput.value); renderSettings(); saveSettings(); });
  stripInput.addEventListener('change', () => { settings.strip = stripInput.checked; renderSettings(); saveSettings(); });
  $('settings-btn').addEventListener('click', () => { renderSettings(); setQuick(false); settingsSheet.hidden = false; });
  $('settings-close').addEventListener('click', () => { settingsSheet.hidden = true; });
  settingsSheet.addEventListener('click', (e) => { if (e.target === settingsSheet) settingsSheet.hidden = true; });

  // ---------- PC actions (Task Manager, shutdown) ----------
  const PC_ACTIONS = ['task_manager', 'shutdown', 'cancel_shutdown', 'paste_text'];
  const toast = $('toast');
  const toastText = $('toast-text');
  const toastBtn = $('toast-btn');
  const SHUTDOWN_SECONDS = 5; // matches the server's `shutdown /t 5`
  let toastTimer = null;
  let toastAction = null;
  let countdownTimer = null;

  function showToast(text, { bad = false, ms = 4000, button = null, onButton = null } = {}) {
    clearTimeout(toastTimer);
    clearInterval(countdownTimer); // a new message (e.g. "Shutdown cancelled.") ends any countdown
    toastText.textContent = text;
    toast.classList.toggle('bad', bad);
    toastBtn.hidden = !button;
    toastBtn.textContent = button || '';
    toastAction = onButton;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, ms);
  }

  toastBtn.addEventListener('click', () => {
    toast.hidden = true;
    if (toastAction) toastAction();
  });

  function pcAction(type) {
    if (!send({ type })) showToast('Not connected to the PC.', { bad: true });
  }

  function startShutdownCountdown() {
    let left = SHUTDOWN_SECONDS;
    const label = () => (left > 0 ? `PC shuts down in ${left}…` : 'Shutting down…');
    showToast(label(), { ms: (SHUTDOWN_SECONDS + 2) * 1000, button: 'Cancel', onButton: () => pcAction('cancel_shutdown') });
    countdownTimer = setInterval(() => {
      left -= 1;
      toastText.textContent = label();
      if (left <= 0) {
        clearInterval(countdownTimer);
        toastBtn.hidden = true;
      }
    }, 1000);
  }

  function onActionOk(ref) {
    if (ref === 'shutdown') {
      settingsSheet.hidden = true;
      startShutdownCountdown();
    } else if (ref === 'cancel_shutdown') {
      showToast('Shutdown cancelled.');
    } else if (ref === 'task_manager') {
      settingsSheet.hidden = true;
    } else if (ref === 'paste_text') {
      showToast('Pasted on the PC.', { ms: 2000 });
    }
  }

  // ---------- Send text: phone clipboard to the PC ----------
  const sendSheet = $('send-sheet');
  const sendText = $('send-text');

  function pasteOnPc(text) {
    if (!send({ type: 'paste_text', text })) {
      showToast('Not connected to the PC.', { bad: true });
      return false;
    }
    return true;
  }

  function openSendSheet() {
    kbd.blur();
    sendText.value = '';
    sendSheet.hidden = false;
    sendText.focus(); // inside the tap handler, so the iOS keyboard opens
  }

  $('send-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (sendText.value && pasteOnPc(sendText.value)) sendSheet.hidden = true;
  });
  $('send-cancel').addEventListener('click', () => { sendSheet.hidden = true; });
  sendSheet.addEventListener('click', (e) => { if (e.target === sendSheet) sendSheet.hidden = true; });

  $('taskmgr-btn').addEventListener('click', () => pcAction('task_manager'));
  $('shutdown-btn').addEventListener('click', () => {
    if (window.confirm('Shut down PC?')) pcAction('shutdown');
  });
  $('cancel-shutdown-btn').addEventListener('click', () => pcAction('cancel_shutdown'));

  // ---------- PIN sheet ----------
  const pinSheet = $('pin-sheet');
  const pinInput = $('pin-input');
  const pinError = $('pin-error');

  function showPin(message) {
    pinError.textContent = message || '';
    pinError.hidden = !message;
    pinInput.value = '';
    pinSheet.hidden = false;
  }

  function hidePin() { pinSheet.hidden = true; }

  $('pin-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const pin = pinInput.value.trim();
    if (!/^\d{6}$/.test(pin)) { showPin('The PIN has 6 digits.'); return; }
    setPin(pin);
    pinJustEntered = true;
    pinInput.blur();
    pinError.hidden = true;
    dropConnection(false);
    reconnectDelay = 500;
    connect();
  });

  // Opened from the QR code in the Mousette window: the PIN (and the encryption key) come
  // after "#", which the browser never sends anywhere. Use it, then wipe it so it isn't kept in history or an icon.
  // Pages linked from other websites arrive as /?from=link (see server/app.py): there the
  // PIN is ignored, so a website can't make Mousette try wrong PINs.
  // A home-screen icon opens "#k=…" (see cleanAddress): the key only, no PIN.
  // Returns true when a PIN or key from the address was taken.
  function takePinFromQr() {
    if (!location.hash) { cleanAddress(); return false; }
    // "#p=…&k=…" (the long "#pin=…&key=…" from 1.5/1.6 works too).
    const fromQr = /^#p(?:in)?=(\d{6})(?:&k(?:ey)?=([A-Za-z0-9_-]{43}))?$/.exec(location.hash);
    const keyOnly = /^#k=([A-Za-z0-9_-]{43})$/.exec(location.hash);
    const trusted = !new URLSearchParams(location.search).has('from');
    history.replaceState(null, '', location.pathname);
    if (trusted && keyOnly) setKey(keyOnly[1]);
    if (trusted && fromQr) {
      setPin(fromQr[1]);
      if (fromQr[2]) setKey(fromQr[2]);
      // Not "just entered": a QR code from an earlier start then says the PIN has changed.
      pinJustEntered = false;
    }
    cleanAddress();
    return trusted && Boolean(fromQr || keyOnly);
  }

  // iPhone gives a home-screen app its own empty storage, and the camera always opens Safari, so
  // the app could never get the key. In Safari the address therefore keeps "#k=<key>" (never the
  // PIN): an icon added from there carries the key over. Like the QR code, "#" never leaves the phone.
  function cleanAddress() {
    const key = store.get('pm.key');
    const keep = appleTouch && !homeScreenApp && canEncrypt && key;
    history.replaceState(null, '', location.pathname + (keep ? `#k=${key}` : ''));
  }

  // Scanning again while Mousette is already open only changes the "#" part.
  window.addEventListener('hashchange', () => {
    if (takePinFromQr()) {
      dropConnection(false);
      reconnectDelay = 500;
      connect();
    }
  });

  // iPhones and iPads can open the PC's .local name, which keeps working when the router gives
  // the PC a new IP address (so does a home-screen icon added there). The QR code uses the IP
  // address so that every phone can open it; an iPhone moves over to the name when it answers.
  // The "#" part, with the PIN and key, comes along and still never leaves the phone.
  async function moveToLocalName() {
    if (!appleTouch || !/^\d+\.\d+\.\d+\.\d+$/.test(location.hostname) || !/^#p(?:in)?=/.test(location.hash)
        || new URLSearchParams(location.search).has('from')) return false;
    try {
      const { name } = await (await fetch('/name', { cache: 'no-store' })).json();
      if (!name) return false;
      // Some networks block .local names: only move when it answers quickly.
      const stop = new AbortController();
      const timer = setTimeout(() => stop.abort(), 2500);
      await fetch(`http://${name}/name`, { mode: 'no-cors', cache: 'no-store', signal: stop.signal });
      clearTimeout(timer);
      location.replace(`http://${name}/${location.hash}`);
      return true;
    } catch {
      return false;
    }
  }

  // Opened in the browser: suggest adding Mousette to the home screen (full screen, no address
  // bar). Shown once per page load on up to 3 visits, or until "Got it". Waits a few seconds so it
  // doesn't cover the "Not encrypted" message.
  const android = /Android/.test(navigator.userAgent);
  let homeTipShown = false;
  function maybeShowHomeTip() {
    if (homeTipShown || homeScreenApp || !(appleTouch || android)) return;
    // Without the key an icon would connect unencrypted; the "Not encrypted" message says to scan.
    if (appleTouch && !getKey()) return;
    const shown = Number(store.get('pm.hometip')) || 0;
    if (shown >= 3) return;
    homeTipShown = true;
    setTimeout(() => {
      if (!toast.hidden) return;
      store.set('pm.hometip', String(shown + 1));
      const how = appleTouch ? 'tap Share, then Add to Home Screen' : 'tap ⋮, then Add to Home screen';
      showToast(`Tip: ${how} to open Mousette full screen.`, {
        ms: 15000, button: 'Got it', onButton: () => store.set('pm.hometip', '3'),
      });
    }, 7000);
  }

  renderSettings();
  moveToLocalName().then((moving) => {
    if (moving) return;
    takePinFromQr();
    connect();
  });
})();
