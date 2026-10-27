// CouchPad web client. Speaks protocol v1 (see PROTOCOL.md) over /ws.
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

  // hiddenButtons: data-action names of bottom-bar buttons the user turned off.
  // barOrder: data-action names in bar order ([] = the order in index.html).
  // barAlign: 'left' | 'center' | 'right' | 'spread'.
  const DEFAULT_SETTINGS = {
    sens: 1.2, scroll: 1.0, strip: false, hiddenButtons: [],
    accent: '#9cc5a1', barOrder: [], barAlign: 'spread',
  };
  let settings = { ...DEFAULT_SETTINGS };
  try { settings = { ...DEFAULT_SETTINGS, ...JSON.parse(store.get('pm.settings') || '{}') }; } catch { /* keep defaults */ }
  const saveSettings = () => store.set('pm.settings', JSON.stringify(settings));

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
  const SESSION_LABEL = new TextEncoder().encode('CouchPad session v1');
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
  // couchpad.local any more; then the connection goes to the IP address instead.
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
          startPing();
          break;
        case 'auth_failed':
          authRejected = true;
          if (msg.reason === 'token_expired') {
            forgetToken();
            showPin('CouchPad on the PC was restarted. Enter the new PIN from the CouchPad window.');
          } else if (msg.reason === 'locked_out') {
            showPin(`Too many wrong PINs. Try again in ${msg.retry_after || 60} s.`);
          } else {
            forgetToken();
            forgetPin();
            showPin(pinJustEntered
              ? 'Wrong PIN. Check the CouchPad window on your PC.'
              : 'The PIN on the PC has changed. Enter the new PIN from the CouchPad window.');
          }
          pinJustEntered = false;
          break;
        case 'pong':
          lastPong = Date.now();
          break;
        case 'action_ok':
          onActionOk(msg.ref);
          break;
        case 'error':
          console.warn('CouchPad server error:', msg.code, msg.message);
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
        authRejected = true;
        showPin('CouchPad has a new encryption key. Scan the QR code on the PC again, or enter the PIN to connect without encryption.');
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
  const TWO_TAP_MS = 300;     // max duration of a two-finger tap
  const SCROLL_PER_PX = 1 / 40; // wheel notches per px of two-finger travel at speed 1

  let mode = 'idle';          // idle | pending | move | drag | two
  let start = null;           // {x, y, t}
  let last = null;            // {x, y, t}
  let speed = 0;              // smoothed finger speed, px/ms
  let holdTimer = null;
  let twoStartT = 0;
  let twoMoved = false;
  let twoStart = null;
  let twoLast = null;

  function centroid(touches) {
    let x = 0; let y = 0;
    for (const t of touches) { x += t.clientX; y += t.clientY; }
    return { x: x / touches.length, y: y / touches.length };
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
      if (mode !== 'two') {
        mode = 'two';
        twoStartT = now;
        twoMoved = false;
        twoStart = centroid(e.touches);
      }
      twoLast = centroid(e.touches);
    }
  }, { passive: false });

  pad.addEventListener('touchmove', (e) => {
    e.preventDefault();
    const now = e.timeStamp;

    if (mode === 'two') {
      if (e.touches.length < 2) return;
      const c = centroid(e.touches);
      if (!twoMoved && Math.hypot(c.x - twoStart.x, c.y - twoStart.y) > TAP_SLOP) twoMoved = true;
      if (twoMoved) {
        // Natural scrolling: content follows the fingers.
        const k = SCROLL_PER_PX * settings.scroll;
        pendingScroll.dy += (c.y - twoLast.y) * k;
        pendingScroll.dx -= (c.x - twoLast.x) * k;
        scheduleFlush();
      }
      twoLast = c;
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
    if (mode === 'two' && e.touches.length >= 2) { twoLast = centroid(e.touches); return; }
    if (e.touches.length > 0) return; // wait until every finger is up

    const now = e.timeStamp;
    if (e.type === 'touchend') {
      if (mode === 'pending') {
        // Each tap is a click; two quick taps reach Windows as a native double-click.
        send({ type: 'click', button: 'left' });
      } else if (mode === 'two' && !twoMoved && now - twoStartT < TWO_TAP_MS) {
        send({ type: 'click', button: 'right' });
      }
    }
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
    strip.classList.add('active');
    stripLast = e.targetTouches[0].clientY;
  }, { passive: false });

  strip.addEventListener('touchmove', (e) => {
    e.preventDefault();
    if (stripLast === null || !e.targetTouches.length) return;
    const y = e.targetTouches[0].clientY;
    pendingScroll.dy += (y - stripLast) * SCROLL_PER_PX * settings.scroll;
    stripLast = y;
    scheduleFlush();
  }, { passive: false });

  function onStripEnd(e) {
    e.preventDefault();
    if (e.targetTouches.length) { stripLast = e.targetTouches[0].clientY; return; }
    stripLast = null;
    strip.classList.remove('active');
    flush();
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
  const kbdBtn = $('keyboard-btn');
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
  kbd.addEventListener('focus', () => kbdBtn.classList.add('on'));
  kbd.addEventListener('blur', () => kbdBtn.classList.remove('on'));

  function toggleKeyboard() {
    if (document.activeElement === kbd) {
      kbd.blur();
    } else {
      resetKbd();
      kbd.focus(); // must run inside the tap handler or iOS won't show the keyboard
    }
  }

  // ---------- Bottom bar ----------
  const actions = {
    left: () => send({ type: 'click', button: 'left' }),
    right: () => send({ type: 'click', button: 'right' }),
    keyboard: toggleKeyboard,
    copy: () => send({ type: 'hotkey', keys: ['ctrl', 'c'] }),
    paste: () => send({ type: 'hotkey', keys: ['ctrl', 'v'] }),
    sendtext: openSendSheet,
    media: () => setMediaPanel(mediaPanel.hidden),
    search: () => send({ type: 'hotkey', keys: ['win', 's'] }),
    alttab: () => send({ type: 'hotkey', keys: ['alt', 'tab'] }),
    desktop: () => send({ type: 'hotkey', keys: ['win', 'd'] }),
    esc: () => send({ type: 'key', name: 'esc' }),
  };

  for (const btn of document.querySelectorAll('.bar-btn')) {
    const run = actions[btn.dataset.action];
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
      if (t.clientX >= r.left && t.clientX <= r.right && t.clientY >= r.top && t.clientY <= r.bottom) run();
    }, { passive: false });
    btn.addEventListener('touchcancel', () => btn.classList.remove('pressed'));
    btn.addEventListener('click', run); // mouse / desktop browsers
  }

  // ---------- Media panel (volume, playback) ----------
  const mediaPanel = $('media-panel');
  const mediaBtn = $('media-btn');

  function setMediaPanel(open) {
    mediaPanel.hidden = !open;
    mediaBtn.classList.toggle('on', open);
  }

  for (const btn of mediaPanel.querySelectorAll('.media-btn')) {
    const press = () => send({ type: 'key', name: btn.dataset.key });
    // Same touch handling as the bar, so the panel stays open for repeated taps (volume).
    btn.addEventListener('touchstart', (e) => { e.preventDefault(); btn.classList.add('pressed'); }, { passive: false });
    btn.addEventListener('touchend', (e) => { e.preventDefault(); btn.classList.remove('pressed'); press(); }, { passive: false });
    btn.addEventListener('touchcancel', () => btn.classList.remove('pressed'));
    btn.addEventListener('click', press);
  }

  // Any touch outside the panel (trackpad, other buttons) closes it, without swallowing that touch.
  const closeMediaOutside = (e) => {
    if (!mediaPanel.hidden && !e.target.closest('#media-panel, #media-btn')) setMediaPanel(false);
  };
  document.addEventListener('touchstart', closeMediaOutside, { capture: true, passive: true });
  document.addEventListener('mousedown', closeMediaOutside, true);

  // ---------- Keep layout above the iOS keyboard ----------
  const rootStyle = document.documentElement.style;
  function fitViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
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
  const bar = document.querySelector('.bottombar');
  const barButtons = [...document.querySelectorAll('.bar-btn')];

  // One tick box per bottom-bar button, built from the bar itself so new buttons appear here too.
  const barChoices = $('bar-choices');
  for (const btn of barButtons) {
    const label = document.createElement('label');
    label.className = 'bar-choice';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.action = btn.dataset.action;
    box.addEventListener('change', () => {
      const hidden = new Set(settings.hiddenButtons);
      if (box.checked) hidden.delete(box.dataset.action); else hidden.add(box.dataset.action);
      settings.hiddenButtons = [...hidden];
      renderSettings();
      saveSettings();
    });
    label.append(box, btn.querySelector('span').textContent);
    barChoices.append(label);
  }

  // Accent color presets. The first one is the default.
  const ACCENTS = [
    ['Sage', '#9cc5a1'], ['Blue', '#7fb8e0'], ['Violet', '#b5a3e0'], ['Amber', '#e3b866'],
    ['Rose', '#e3a0b4'], ['Coral', '#e39a8a'], ['Teal', '#7cc7bd'], ['Sand', '#d6c7a8'],
  ];
  const swatches = $('swatches');
  for (const [name, color] of ACCENTS) {
    const swatch = document.createElement('button');
    swatch.className = 'swatch';
    swatch.setAttribute('role', 'radio');
    swatch.setAttribute('aria-label', name);
    swatch.style.setProperty('--c', color);
    swatch.dataset.color = color;
    swatch.addEventListener('click', () => { settings.accent = color; renderSettings(); saveSettings(); });
    swatches.append(swatch);
  }

  function renderAccent() {
    const accent = ACCENTS.some(([, c]) => c === settings.accent) ? settings.accent : DEFAULT_SETTINGS.accent;
    document.documentElement.style.setProperty('--accent', accent);
    for (const s of swatches.children) s.setAttribute('aria-checked', String(s.dataset.color === accent));
  }

  // Bar order: one row per button with a drag handle and up/down buttons.
  const DEFAULT_ORDER = barButtons.map((btn) => btn.dataset.action);
  const ALIGNS = ['left', 'center', 'right', 'spread'];
  const orderList = $('order-list');
  const orderRows = {};
  const GRIP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm0 7a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm-1.5 8.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM18 5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Zm-1.5 8.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3ZM18 19a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z"/></svg>';
  const UP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14l-6-6Z"/></svg>';
  const DOWN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6-1.41-1.41Z"/></svg>';

  // Saved order, cleaned up: unknown names dropped, buttons missing from it added at the end.
  function barOrder() {
    const saved = Array.isArray(settings.barOrder) ? settings.barOrder.filter((a) => DEFAULT_ORDER.includes(a)) : [];
    const order = [...new Set(saved)];
    for (const action of DEFAULT_ORDER) if (!order.includes(action)) order.push(action);
    return order;
  }

  function moveButton(action, step) {
    const order = barOrder();
    const i = order.indexOf(action);
    const j = i + step;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    settings.barOrder = order;
    renderSettings();
    saveSettings();
  }

  for (const btn of barButtons) {
    const action = btn.dataset.action;
    const name = btn.querySelector('span').textContent;
    const row = document.createElement('li');
    row.className = 'order-row';
    row.dataset.action = action;
    row.innerHTML = `<button class="grip" aria-label="Drag to move ${name}">${GRIP}</button>`
      + `<span class="name">${btn.querySelector('svg').outerHTML}<span>${name}</span><span class="tag">hidden</span></span>`
      + `<button class="move up" aria-label="Move ${name} up">${UP}</button>`
      + `<button class="move down" aria-label="Move ${name} down">${DOWN}</button>`;
    row.querySelector('.up').addEventListener('click', () => moveButton(action, -1));
    row.querySelector('.down').addEventListener('click', () => moveButton(action, 1));

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
      settings.barOrder = [...orderList.children].map((r) => r.dataset.action);
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

  const alignButtons = [...$('bar-align').children];
  for (const b of alignButtons) {
    b.addEventListener('click', () => { settings.barAlign = b.dataset.align; renderSettings(); saveSettings(); });
  }
  $('reset-bar').addEventListener('click', () => {
    settings.hiddenButtons = [];
    settings.barOrder = [];
    settings.barAlign = DEFAULT_SETTINGS.barAlign;
    renderSettings();
    saveSettings();
  });

  function renderBar() {
    const hidden = new Set(Array.isArray(settings.hiddenButtons) ? settings.hiddenButtons : []);
    const order = barOrder();
    const byAction = Object.fromEntries(barButtons.map((btn) => [btn.dataset.action, btn]));
    let shown = 0;
    order.forEach((action, i) => {
      const btn = byAction[action];
      bar.append(btn); // moving a node keeps its listeners
      btn.hidden = hidden.has(action);
      if (!btn.hidden) shown++;
      const row = orderRows[action];
      if (orderList.children[i] !== row) orderList.insertBefore(row, orderList.children[i] || null);
      row.classList.toggle('is-hidden', btn.hidden);
      row.querySelector('.up').disabled = i === 0;
      row.querySelector('.down').disabled = i === order.length - 1;
    });
    for (const box of barChoices.querySelectorAll('input')) box.checked = !hidden.has(box.dataset.action);
    // Up to 7 buttons fit in one row; more wrap into two even rows.
    bar.style.setProperty('--bar-cols', shown <= 7 ? shown : Math.ceil(shown / 2));
    bar.hidden = shown === 0;
    const align = ALIGNS.includes(settings.barAlign) ? settings.barAlign : DEFAULT_SETTINGS.barAlign;
    bar.dataset.align = align;
    for (const b of alignButtons) b.setAttribute('aria-checked', String(b.dataset.align === align));
    if (mediaBtn.hidden) setMediaPanel(false);
  }

  function renderSettings() {
    renderAccent();
    renderBar();
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
  $('settings-btn').addEventListener('click', () => { renderSettings(); settingsSheet.hidden = false; });
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

  // The server starts Task Manager itself, so Windows shows no UAC prompt the phone can't answer.
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

  // Opened from the QR code in the CouchPad window: the PIN (and the encryption key) come
  // after "#", which the browser never sends anywhere. Use it, then wipe it so it isn't kept in history or an icon.
  // Pages linked from other websites arrive as /?from=link (see server/app.py): there the
  // PIN is ignored, so a website can't make CouchPad try wrong PINs.
  // Returns true when a PIN from the QR code was taken.
  function takePinFromQr() {
    if (!location.hash) return false;
    const fromQr = /^#pin=(\d{6})(?:&key=([A-Za-z0-9_-]{43}))?$/.exec(location.hash);
    const trusted = !new URLSearchParams(location.search).has('from');
    history.replaceState(null, '', location.pathname);
    if (!fromQr || !trusted) return false;
    setPin(fromQr[1]);
    if (fromQr[2]) setKey(fromQr[2]);
    // Not "just entered": a QR code from an earlier start then says the PIN has changed.
    pinJustEntered = false;
    return true;
  }

  // Scanning again while CouchPad is already open only changes the "#" part.
  window.addEventListener('hashchange', () => {
    if (takePinFromQr()) {
      dropConnection(false);
      reconnectDelay = 500;
      connect();
    }
  });

  takePinFromQr();
  renderSettings();
  connect();
})();
