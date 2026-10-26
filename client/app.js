// PhoneMouse web client. Speaks protocol v1 (see PROTOCOL.md) over /ws.
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
  const DEFAULT_SETTINGS = { sens: 1.2, scroll: 1.0, strip: false, hiddenButtons: [] };
  let settings = { ...DEFAULT_SETTINGS };
  try { settings = { ...DEFAULT_SETTINGS, ...JSON.parse(store.get('pm.settings') || '{}') }; } catch { /* keep defaults */ }
  const saveSettings = () => store.set('pm.settings', JSON.stringify(settings));

  // The PIN lives in memory only: the server makes a new one on every start,
  // so each fresh open of the page asks for it. Older versions saved it; clear that.
  store.remove('pm.pin');
  let currentPin = null;
  const getPin = () => currentPin;
  const setPin = (pin) => { currentPin = pin; };
  const forgetPin = () => { currentPin = null; };

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

  function setStatus(connected, text) {
    statusEl.classList.toggle('connected', connected);
    statusText.textContent = text;
  }

  function rawSend(msg) {
    if (ws && authed && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ v: V, ...msg }));
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
    if (!pin) { setStatus(false, 'Enter PIN'); showPin(); return; }

    setStatus(false, 'Connecting…');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const sock = new WebSocket(`${proto}//${location.host}/ws`);
    ws = sock;
    let authRejected = false;

    sock.onopen = () => sock.send(JSON.stringify({ v: V, type: 'auth', pin }));
    sock.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      switch (msg.type) {
        case 'auth_ok':
          authed = true;
          pinJustEntered = false;
          reconnectDelay = 500;
          lastPong = Date.now();
          setStatus(true, 'Connected');
          hidePin();
          startPing();
          break;
        case 'auth_failed':
          authRejected = true;
          if (msg.reason === 'locked_out') {
            showPin(`Too many wrong PINs. Try again in ${msg.retry_after || 60} s.`);
          } else {
            forgetPin();
            showPin(pinJustEntered
              ? 'Wrong PIN. Check the PhoneMouse window on your PC.'
              : 'The PIN on the PC has changed. Enter the new PIN from the PhoneMouse window.');
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
          console.warn('PhoneMouse server error:', msg.code, msg.message);
          if (PC_ACTIONS.includes(msg.ref)) showToast(msg.message, { bad: true });
          break;
      }
    };
    sock.onclose = () => {
      if (ws !== sock) return; // already dropped
      ws = null;
      authed = false;
      stopPing();
      if (authRejected) {
        setStatus(false, 'Not connected');
      } else {
        setStatus(false, 'Reconnecting…');
        scheduleReconnect();
      }
    };
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    reconnectDelay = 500;
    if (!ws) { if (getPin()) connect(); return; }
    // Check the socket survived the phone sleeping.
    const sentAt = Date.now();
    rawSend({ type: 'ping', id: ++pingId });
    setTimeout(() => { if (ws && lastPong < sentAt) dropConnection(true); }, 2000);
  });

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

  function renderBar() {
    const hidden = new Set(Array.isArray(settings.hiddenButtons) ? settings.hiddenButtons : []);
    let shown = 0;
    for (const btn of barButtons) {
      btn.hidden = hidden.has(btn.dataset.action);
      if (!btn.hidden) shown++;
    }
    for (const box of barChoices.querySelectorAll('input')) box.checked = !hidden.has(box.dataset.action);
    // Up to 7 buttons fit in one row; more wrap into two even rows.
    bar.style.setProperty('--bar-cols', shown <= 7 ? shown : Math.ceil(shown / 2));
    bar.hidden = shown === 0;
    if (mediaBtn.hidden) setMediaPanel(false);
  }

  function renderSettings() {
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
  const PC_ACTIONS = ['task_manager', 'shutdown', 'cancel_shutdown'];
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
    }
  }

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

  // Opened from the QR code in the PhoneMouse window: the PIN comes after "#", which the
  // browser never sends anywhere. Use it, then wipe it so it isn't kept in history or an icon.
  if (location.hash) {
    const fromQr = /^#pin=(\d{6})$/.exec(location.hash);
    if (fromQr) {
      setPin(fromQr[1]);
      pinJustEntered = true;
    }
    history.replaceState(null, '', location.pathname + location.search);
  }

  renderSettings();
  connect();
})();
