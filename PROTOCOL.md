# CouchPad protocol, version 1

This describes the messages between the PC server and the web page in `client/`. Server-side validation lives in `server/protocol.py`.

## Transport

- WebSocket at `ws://couchpad.local/ws` or `ws://<pc-lan-ip>/ws`. The port is 80, or 8765 when 80 is taken (choose another with `--port`).
- Each WebSocket **text** frame holds one JSON object, UTF-8 encoded. On an encrypted connection (see [Encryption](#encryption)), every frame after the handshake is a **binary** frame holding one encrypted JSON object instead.
- Every message, in both directions, has `"v": 1` and a `"type"` string.
- The server only accepts connections from private/LAN addresses (10/8, 172.16/12, 192.168/16, link-local, loopback).
- A frame can be at most 512 KB.

## Session flow

```
client                                   server
  | --- connect ws://pc/ws --------------> |
  | --- {"v":1,"type":"auth","pin":"…"} -> |   must be the first message, within 10 s
  | <-- {"v":1,"type":"auth_ok",…} ------- |   or auth_failed + close
  | --- input messages / ping ----------->|
  | <-- pong / error ---------------------- |
```

1. Open the WebSocket.
2. Send `auth` as the **first** message. If the first message is anything else, or nothing arrives within 10 s, the server replies with `error` (`auth_required`) and closes the socket with code **4001**.
3. On success the server replies `auth_ok`. All other message types are accepted only after this.
4. The server does **not** acknowledge input messages, which keeps latency low. It replies only to `ping` (with `pong`), to rejected messages (with `error`), and to successful actions (`task_manager`, `shutdown`, `cancel_shutdown`, `paste_text`, with `action_ok`). Messages are processed strictly in the order they arrive. So a `pong` also means every message sent before its `ping` has been handled.

The PIN is a random 6-digit string. The server prints it at startup, and it changes every time the server restarts. After 5 wrong PINs from one IP address, that address is locked out for 60 s. After 10 wrong PINs from all addresses together within 60 s, every address is locked out until that window has passed.

The server only accepts requests whose `Host` header is the address it printed, and refuses WebSocket handshakes whose `Origin` is not that same address (close code 4031). This stops other websites and DNS-rebinding tricks from reaching it through a browser.

## Client → server messages

| type | fields | effect |
|---|---|---|
| `auth` | `pin`: string, **or** `token`: string | Authenticate. Must be first (or first after the encryption handshake). `token` is one an earlier `auth_ok` returned; it lets a client log in again without the PIN. |
| `enc_hello` | `nonce`: 16 random bytes, base64url without padding | Start an encrypted connection. Only allowed as the very first message; see [Encryption](#encryption). |
| `ping` | `id`: number or string, optional | Server replies `pong` with the same `id`. |
| `move` | `dx`, `dy`: number (px, ±4000) | Move the cursor relative to where it is. Fractions are allowed; the server carries sub-pixel remainders over, so many small moves add up correctly. |
| `click` | `button`: `"left"`, `"right"` or `"middle"` (default `"left"`) | Single click. |
| `double_click` | `button` (default `"left"`) | Double click. |
| `mouse_down` | `button` (default `"left"`) | Press and hold a button (start of a drag). |
| `mouse_up` | `button` (default `"left"`) | Release the button. |
| `scroll` | `dx`, `dy`: number (wheel notches, ±100) | Positive `dy` scrolls **up**, negative `dy` scrolls down. Positive `dx` scrolls right. Fractions give smooth scrolling (the smallest effective step is 1/120 notch). |
| `type_text` | `text`: string (≤ 2000 chars) | Types the text as Unicode, including accented letters (é ü ñ) and emoji. `\n` presses Enter. |
| `paste_text` | `text`: non-empty string (≤ 100 000 chars) | Puts the text on the PC clipboard, then presses Ctrl+V (Cmd+V on macOS), so it is pasted at the cursor in one go. Replies `action_ok`, or `error` (`input_failed`) if the clipboard could not be set. |
| `key` | `name`: key name (see below) | Press and release one key. |
| `hotkey` | `keys`: list of 1–5 key names | Press the keys in order, then release them in reverse order. |
| `task_manager` | none | The server starts Task Manager itself, so it inherits the server's admin rights and Windows shows no UAC prompt. Replies `error` (`input_failed`) if the server is not running as administrator. |
| `shutdown` | none | Shut the PC down after a 5 s delay (`shutdown /s /t 5`). Clients should ask the user to confirm first. |
| `cancel_shutdown` | none | Abort a pending shutdown (`shutdown /a`). Replies `error` (`input_failed`) if none is pending. |

Fields that are missing default to `0` (numbers) or `"left"` (button). Unknown extra fields are ignored, so later clients can add optional fields without breaking older servers.

### Key names

`key` accepts:
`enter`, `backspace`, `tab`, `esc`, `delete`, `home`, `end`, `up`, `down`, `left`, `right`, `space`, `page_up`, `page_down`, and the media keys `volume_down`, `volume_mute`, `volume_up`, `media_previous`, `media_play_pause`, `media_next`

`hotkey` accepts all of the above, plus:
- the modifiers `ctrl`, `alt`, `shift`, `win`
- `f1` … `f12`
- any single printable character, such as `"s"`, `"d"` or `"1"`

Key names are case-insensitive.

Examples:
```json
{"v":1,"type":"hotkey","keys":["win","s"]}      // Windows search
{"v":1,"type":"hotkey","keys":["alt","tab"]}    // switch window
{"v":1,"type":"hotkey","keys":["win","d"]}      // show desktop
{"v":1,"type":"hotkey","keys":["ctrl","c"]}     // copy
```

## Server → client messages

| type | fields | meaning |
|---|---|---|
| `enc_challenge` | `nonce`: 16 random bytes, base64url without padding | Reply to `enc_hello`, sent unencrypted. Everything after it is encrypted in both directions. |
| `auth_ok` | `protocol`: 1; `token`: string (only after a PIN); optional `direct`: the server's IP address and port | Authenticated; input is accepted from now on. Keep `token` to log in again later: it is valid until the server stops. If the page's host name (`couchpad.local`) stops resolving, connect to `ws://<direct>/ws` instead: the server accepts its own page from any of its addresses. |
| `auth_failed` | `reason`: `"wrong_pin"`, `"token_expired"` (the server restarted since the token was issued) or `"locked_out"`; `retry_after`: seconds (only when locked out) | The socket closes right after this. |
| `pong` | `id`: the id from the ping, or null | Reply to `ping`. |
| `action_ok` | `ref`: `"task_manager"`, `"shutdown"`, `"cancel_shutdown"` or `"paste_text"` | The action was carried out. |
| `error` | `code`, `message`, optional `ref` | The message was rejected. The connection stays open, except for `auth_required`. |

### Error codes

| code | cause |
|---|---|
| `bad_json` | The frame is not a JSON object. |
| `bad_version` | `v` is missing or is not `1`. |
| `unknown_type` | `type` is not one listed above. |
| `bad_field` | A field has the wrong type or is out of range. |
| `auth_required` | An input message was sent before `auth`. The socket then closes with 4001. |
| `no_encryption` | `enc_hello` was sent, but the server has no encryption key. The socket then closes with 4001. |
| `bad_state` | `enc_hello` was sent after the connection had started. |
| `input_failed` | Windows rejected the injected input or PC action (`ref` names the message type; `message` says why). |

### Close codes

| code | meaning |
|---|---|
| 4001 | Not authenticated (no or late `auth`, or another message first). |
| 4003 | Wrong PIN. |
| 4029 | Too many wrong PINs; wait `retry_after` seconds. |
| 4030 | The connection did not come from a local network address. |
| 4031 | The connection came from another website's page, or used a different host name. |
| 4032 | An encrypted connection received a frame that doesn't decrypt (wrong key, changed, replayed or out-of-order frame), or an unencrypted frame. Nothing in it was used. A client that gets this after its handshake should drop its saved key and pair again. |

## Encryption

Optional, and used by the web client whenever it has the key. It keeps everything sent over the network (PIN, typed and pasted text, every input) unreadable to others on the same network, and stops recorded traffic from being replayed.

**The key.** The server keeps one random 32-byte key `K` in a file (`%APPDATA%\CouchPad\key` on Windows, `~/Library/Application Support/CouchPad/key` on macOS, `~/.config/couchpad/key` on Linux), so phones stay paired across restarts. `--new-key` replaces it, which unpairs every phone. The QR code carries `K` after `#` (`/#pin=123456&key=<base64url K>`): browsers never send that part over the network, so `K` reaches the phone only through the camera. A PIN typed by hand doesn't give the phone `K`; such connections stay unencrypted.

**Handshake.** The client sends `{"v":1,"type":"enc_hello","nonce":cn}` as its first message, with 16 fresh random bytes `cn`. The server replies `{"v":1,"type":"enc_challenge","nonce":sn}` with 16 fresh random bytes `sn`. Both then compute the session key

```
session_key = SHA-512("CouchPad session v1" || K || sn || cn)[0:32]
```

**Frames.** From then on, every message in both directions is the usual JSON object, UTF-8 encoded and sealed with NaCl `secretbox` (XSalsa20-Poly1305) under `session_key`, sent as one binary frame (ciphertext with its 16-byte tag; the nonce is not sent). The 24-byte nonce is

```
nonce = direction (1 byte: 0 = client to server, 1 = server to client)
        || counter (8 bytes, little-endian: 0 for the first frame in that direction, then 1, 2, ...)
        || 15 zero bytes
```

So a frame that is replayed, dropped, reordered or changed fails to open, and the connection is closed (4032). The first encrypted client message must be `auth`, as on an unencrypted connection.

## Behaviour notes for client authors

- **Connection health:** send `ping` every ~2 s. If no `pong` arrives within ~6 s, treat the socket as dead and reconnect. Mobile operating systems often keep dead sockets "open" after the phone sleeps.
- **Reconnecting:** use a new socket and authenticate again with the `token` from `auth_ok`. The web client saves the token (never the PIN), because iOS often reloads the page after the phone was locked. On `token_expired` or `wrong_pin`, drop the token and ask the user for the PIN again (the server has restarted). Wrong tokens don't count towards the PIN lockout, and a token still works while the PIN is locked out.
- **Coalescing:** batch `move`/`scroll` deltas and send at most once per display frame (~60 Hz). Before sending a discrete event such as `click`, flush any pending move so the order stays right.
- **Drags:** always pair `mouse_down` with `mouse_up`. If the socket drops, the server releases any held buttons itself.
- **Double-click:** the web client sends one `click` per tap. Two quick taps then reach Windows as a native double-click, with no extra wait on single taps. `double_click` is there for clients that want an explicit double-click.

## Versioning

`v` is the protocol version. Changes that keep compatibility (new optional fields, new message types) keep `v: 1`. Any change that alters how an existing message behaves bumps `v`. The server answers messages with a version it doesn't support with `bad_version`.
