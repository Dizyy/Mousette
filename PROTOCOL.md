# PhoneMouse protocol, version 1

This describes the messages between the PC server and the web page in `client/`. Server-side validation lives in `server/protocol.py`.

## Transport

- WebSocket at `ws://phonemouse.local/ws` or `ws://<pc-lan-ip>/ws`. The port is 80, or 8765 when 80 is taken (choose another with `--port`).
- Each WebSocket **text** frame holds one JSON object, UTF-8 encoded. Binary frames are not used.
- Every message, in both directions, has `"v": 1` and a `"type"` string.
- The server only accepts connections from private/LAN addresses (10/8, 172.16/12, 192.168/16, link-local, loopback).
- A frame can be at most 64 KB.

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
4. The server does **not** acknowledge input messages, which keeps latency low. It replies only to `ping` (with `pong`), to rejected messages (with `error`), and to successful PC actions (`task_manager`, `shutdown`, `cancel_shutdown`, with `action_ok`). Messages are processed strictly in the order they arrive. So a `pong` also means every message sent before its `ping` has been handled.

The PIN is a random 6-digit string. The server prints it at startup, and it changes every time the server restarts. After 5 wrong PINs from one IP address, that address is locked out for 60 s. After 10 wrong PINs from all addresses together within 60 s, every address is locked out until that window has passed.

The server only accepts requests whose `Host` header is the address it printed, and refuses WebSocket handshakes whose `Origin` is not that same address (close code 4031). This stops other websites and DNS-rebinding tricks from reaching it through a browser.

## Client → server messages

| type | fields | effect |
|---|---|---|
| `auth` | `pin`: string | Authenticate. Must be first. |
| `ping` | `id`: number or string, optional | Server replies `pong` with the same `id`. |
| `move` | `dx`, `dy`: number (px, ±4000) | Move the cursor relative to where it is. Fractions are allowed; the server carries sub-pixel remainders over, so many small moves add up correctly. |
| `click` | `button`: `"left"`, `"right"` or `"middle"` (default `"left"`) | Single click. |
| `double_click` | `button` (default `"left"`) | Double click. |
| `mouse_down` | `button` (default `"left"`) | Press and hold a button (start of a drag). |
| `mouse_up` | `button` (default `"left"`) | Release the button. |
| `scroll` | `dx`, `dy`: number (wheel notches, ±100) | Positive `dy` scrolls **up**, negative `dy` scrolls down. Positive `dx` scrolls right. Fractions give smooth scrolling (the smallest effective step is 1/120 notch). |
| `type_text` | `text`: string (≤ 2000 chars) | Types the text as Unicode, including accented letters (é ü ñ) and emoji. `\n` presses Enter. |
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
| `auth_ok` | `protocol`: 1 | Authenticated; input is accepted from now on. |
| `auth_failed` | `reason`: `"wrong_pin"` or `"locked_out"`; `retry_after`: seconds (only when locked out) | The socket closes right after this. |
| `pong` | `id`: the id from the ping, or null | Reply to `ping`. |
| `action_ok` | `ref`: `"task_manager"`, `"shutdown"` or `"cancel_shutdown"` | The PC action was carried out. |
| `error` | `code`, `message`, optional `ref` | The message was rejected. The connection stays open, except for `auth_required`. |

### Error codes

| code | cause |
|---|---|
| `bad_json` | The frame is not a JSON object. |
| `bad_version` | `v` is missing or is not `1`. |
| `unknown_type` | `type` is not one listed above. |
| `bad_field` | A field has the wrong type or is out of range. |
| `auth_required` | An input message was sent before `auth`. The socket then closes with 4001. |
| `input_failed` | Windows rejected the injected input or PC action (`ref` names the message type; `message` says why). |

### Close codes

| code | meaning |
|---|---|
| 4001 | Not authenticated (no or late `auth`, or another message first). |
| 4003 | Wrong PIN. |
| 4029 | Too many wrong PINs; wait `retry_after` seconds. |
| 4030 | The connection did not come from a local network address. |
| 4031 | The connection came from another website's page, or used a different host name. |

## Behaviour notes for client authors

- **Connection health:** send `ping` every ~2 s. If no `pong` arrives within ~6 s, treat the socket as dead and reconnect. Mobile operating systems often keep dead sockets "open" after the phone sleeps.
- **Reconnecting:** use a new socket and authenticate again with the PIN entered earlier in the same session (the web client keeps it in memory only, never in storage). On `wrong_pin`, drop the PIN and ask the user for it again (the server has probably restarted).
- **Coalescing:** batch `move`/`scroll` deltas and send at most once per display frame (~60 Hz). Before sending a discrete event such as `click`, flush any pending move so the order stays right.
- **Drags:** always pair `mouse_down` with `mouse_up`. If the socket drops, the server releases any held buttons itself.
- **Double-click:** the web client sends one `click` per tap. Two quick taps then reach Windows as a native double-click, with no extra wait on single taps. `double_click` is there for clients that want an explicit double-click.

## Versioning

`v` is the protocol version. Changes that keep compatibility (new optional fields, new message types) keep `v: 1`. Any change that alters how an existing message behaves bumps `v`. The server answers messages with a version it doesn't support with `bad_version`.
