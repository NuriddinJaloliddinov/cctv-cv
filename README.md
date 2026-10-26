# CCTV CV

Real-time person and phone-use detection for ONVIF cameras, with optional
Telegram alerts that group administrators can confirm or reject.

## Setup (Windows, Linux, macOS)

Install **64-bit Python 3.10–3.12** (3.11 recommended) and make sure the
computer has internet access on first setup. From the repository directory:

```powershell
# Windows (PowerShell):
py -3.11 bootstrap.py
# or: .\setup.ps1
.\.venv\Scripts\python.exe run.py
```

```sh
# Linux / macOS (use python3.11 if python3 is a different version):
python3 bootstrap.py
# or: sh setup.sh
.venv/bin/python run.py
```

The bootstrap creates `.venv`, installs `requirements.txt` **into that venv**,
copies `.env.example` to `.env` only if needed, downloads the two official
face models, and exports YOLOv8n only if its ONNX model is missing. YOLO export
uses a disposable environment, so PyTorch and GUI OpenCV are **not installed
in the server environment**. Existing models and `data/cctv.db` are not
overwritten. Setup fails if downloads, dependency resolution or validation
fails; it never prints a success message after a required step fails.

To verify an existing installation **offline** without changing packages,
models or saved cameras, run `python bootstrap.py --check` (Windows) or
`python3 bootstrap.py --check` (Linux/macOS). The check runs YOLO inference,
loads and exercises both face models, verifies video/WebRTC support, and
starts the API briefly against a **temporary empty database**. A busy
configured port is reported separately, since the existing app may be using
it. If you copy the project to another computer, run the bootstrap there;
virtual environments are not portable.

**Server notes:** The bootstrap verifies the software locally, not your camera
credentials, ONVIF/RTSP network path, firewall, or frame rate on that hardware.
The app defaults to `HOST=0.0.0.0` and serves plain HTTP. Sign-in and role-based
access control are built in, but the connection is not encrypted by default;
put it behind an HTTPS reverse proxy before exposing it beyond a trusted LAN.
The session cookie becomes `Secure` automatically when the request arrives over
HTTPS (including a trusted `X-Forwarded-Proto` proxy).
CPU inference works without GPU libraries. NVIDIA/other GPU providers require
matching drivers and runtime libraries and must be configured separately;
the readiness check deliberately tests the reliable CPU path.

## Open
    http://localhost:8000

The first visit redirects to the sign-in page. The bootstrap administrator is
`admin` with password `12345`; **change that password after the first sign-in**
using **Password** in the sidebar. See [Sign-in and accounts](#sign-in-and-accounts).

The sidebar has nine sections. Administrators see **Overview** for attendance,
customer and event counters; **Cameras** for discovery, configuration and live
analytics feeds; **Enrollment** for registering people and reviewing
unrecognised faces; **Attendance** for confirmed entrance/exit records;
**Customers** for customer-tagged crossings; **Server** for a measured capacity
assessment; **Training** for phone-use review
and candidate models; **Telegram** for bot setup and admin-confirmed events; and
**Accounts** for user management. Readers see the read-only sections only. The
current section is kept in the URL, so it can be bookmarked or refreshed.
Use the **Language / Язык / Til** selector in the sidebar (or on the sign-in
page) to switch between English, Russian and Uzbek (Latin script). The choice
is saved in that browser; it does not modify stored camera or event data.
On narrow screens, use the menu button beside the page title to open the
sidebar; choose a section or tap outside the drawer to close it.

### Server capacity assessment

Administrators can use **Server** to enter the planned camera count, FPS, stream
resolution, expected people, viewers, bitrate, and snapshot storage/retention.
The check observes existing running cameras for 15 seconds; it does **not** start
new cameras or run synthetic inference. Results include CPU/RAM/disk inventory,
measured inference rates and recent inference latency, observed bottlenecks and
qualified planning estimates. A workload without comparable running cameras is
reported **Not yet verified**, not approved based on hardware model names.
Camera codecs, crowded scenes, GPU sharing, disk throughput and network peaks
need a sustained full-size test before purchasing or certifying a server.

## Sign-in and accounts

Every request needs a signed-in account.

- **Roles** — `admin` accounts can change settings, enroll people, run the
  pipeline, configure Telegram and manage accounts. `reader` accounts can view
  the dashboard, camera list and live feeds, attendance, customers, people and
  events. Read-only restrictions are enforced by the server, not just hidden in
  the browser.
- **Bootstrap account** — the first start creates `admin` / `12345` only when the
  users table is empty. Change the password immediately; deleting
  `data/cctv.db` would recreate this default.
- **Creating accounts** — an administrator opens **Accounts → + Add account**,
  chooses `reader` or `admin`, and sets the initial password. Passwords can be
  reset later without knowing the old one.
- **Disable / enable** — disabling an account signs it out immediately and blocks
  new sign-ins until an administrator re-enables it. An administrator cannot
  disable or demote their own account, and the last active administrator cannot
  be removed.
- **Lockout** — three consecutive wrong passwords lock the account. A correct
  password does not work while it is locked. An administrator uses **Unlock** in
  **Accounts** to reset the counter and allow another try.
- **Audit** — sign-ins, failures, lockouts, password changes and account changes
  are recorded in `auth_audit` and available at `/api/auth/audit` (administrators
  only).

Security details: passwords are stored as salted PBKDF2-HMAC-SHA256 hashes;
sessions use a random token in an `HttpOnly`, `SameSite=Strict` cookie (with
`Secure` on HTTPS), and only the token's SHA-256 digest is stored. Sessions
expire after 12 hours by default, and changing a password or disabling an
account ends the other sessions. Unsafe requests with a mismatched `Origin` are
rejected as CSRF protection. Unknown usernames still pay the password-hash cost
so response timing does not reveal which accounts exist.

Optional environment overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CCTV_SESSION_HOURS` | `12` | Session lifetime in hours |
| `CCTV_AUTH_ITERATIONS` | `240000` | PBKDF2 work factor |
| `CCTV_MAX_FAILED_ATTEMPTS` | `3` | Wrong passwords before lockout |
| `CCTV_SECURE_COOKIE` | `auto` | `auto`, `1` or `0` for the Secure cookie flag |

## Camera discovery

Open **Cameras** and click **🔍 Discover NVR & Camera**. Discovery does not
submit or try any username/password, including saved credentials or `.env`
defaults. Select a device from the results, enter **that device's** login,
then click **Connect & Add**. Only this explicit action tries those credentials
on the selected endpoint and saves verified video.

Discovery uses every local interface, the OS routing table, WS-Discovery (multicast
and unicast), SSDP/UPnP, the ARP/neighbour cache, and a TCP port sweep. Each
candidate host is then checked with ONVIF and RTSP.

Discovery reports endpoints with ONVIF or RTSP evidence:

- **ONVIF** — a SOAP response in an ONVIF namespace (`GetSystemDateAndTime`,
  `GetDeviceInformation`, `GetCapabilities`/`GetServices`) or a WS-Discovery
  advertisement for a `NetworkVideoTransmitter`.
- **RTSP** — a `DESCRIBE` with an SDP `m=video` track, or a service that
  challenges anonymous RTSP requests. The latter remains **needs login**, not
  verified video, until you choose it and supply credentials.

A device is marked **video OK** only after a real frame is decoded from one of
its streams. Devices that need a password are still listed, with **needs
login**, so they never silently disappear. Generic web servers, routers,
printers and NAS boxes that answer HTTP/SSDP but speak neither ONVIF nor RTSP
are not reported. A bare HTTP `401` without independent video-device evidence
is not treated as an ONVIF camera. A `credentials` RTSP result means an RTSP
service requested authentication; its video stream cannot be proved until
the device accepts credentials.

The list shows detected endpoints, their status and known channels. Select an
entry to enter its own credentials. **Connect & Add** verifies the selected
device only and saves it if a frame can be decoded; for a multi-channel NVR,
accessible channels become separate camera entries. Its credentials are stored
only when you add it, so **Check feeds** can re-enumerate channels later.
An ONVIF model or explicit recorder advertisement can identify an **NVR** or
**Camera**. If the device gives no reliable type, the list says **ONVIF device**
or **RTSP device** instead of guessing; several stream profiles alone do not
prove an NVR because a single camera may offer multiple streams.

### Advanced options

- **Extra subnets or hosts** — add an isolated network or a specific address,
  e.g. `192.168.1.0/24, 10.0.0.12`. Local networks are scanned automatically.
- **Deep scan** — probes all 65,535 TCP ports on responsive hosts. Useful for
  a camera on a non-standard port; noticeably slower, and limited to a small
  number of hosts per run.

Saved camera addresses and paths are included when requested, but their
passwords are never used during scanning. `POST /api/discover` accepts optional
`subnets`, `targets`, `deep` and `include_saved` inputs and returns `devices`,
`networks`, `scanned_hosts`, `duration_ms` and `warnings`. Legacy credential
fields in a discovery request are ignored. `POST /api/discover/connect` takes
the selected `address`, `username` and `password` to verify that one device.

### What discovery cannot do

No port/protocol scan can guarantee finding every camera. Discovery cannot
see:

- devices on a network this computer cannot route to (a different VLAN without
  a route, or a network behind NAT) — add that subnet explicitly;
- devices that block discovery traffic (client/AP isolation, a firewall that
  drops ARP or multicast), or that are powered off;
- IPv6-only devices;
- devices that only expose a proprietary protocol (no ONVIF and no RTSP) and
  no discoverable video port;
- a generic RTSP *media server* that is not a camera — it may be reported as
  an RTSP source because video RTSP is indistinguishable from camera RTSP;
- the video behind a login until you connect the chosen device. Some cameras
  lock accounts after repeated bad logins, so connection attempts are limited.

When a device is not found, enter its address under **Advanced → Extra
subnets or hosts**, or add its RTSP URL directly.

## Latency

Live video should stay within about a second of the camera's own clock.

What the pipeline does to keep it that way:

- The RTSP/MJPEG reader runs in its own thread and always drains the stream,
  keeping only the newest frame. It never lets a decoder backlog grow.
- Person detection and face recognition run on separate threads from the
  video output, so a slow inference pass cannot freeze the picture.
- Face-identified people keep their name on a per-camera track when they turn
  away. Brief gaps can be reconnected using nearby clothing colours; these
  tentative labels are marked `(visual)` rather than presented as face matches.
- Output is downscaled to `STREAM_MAX_WIDTH` (default 1280) before WebRTC
  encoding, and the WebRTC track is paced at the camera's configured fps
  instead of duplicating frames at 30 fps.
- Several devices (for example a laptop and a phone) can watch the same camera
  at the same time. Each viewer gets an independent WebRTC connection, so
  opening a second view does not disconnect the first; each stream is encoded
  separately, which costs some additional CPU per viewer.
- Detection and face matching run on a separate copy at `DETECT_MAX_WIDTH`
  (default 1920; `0` keeps the native resolution). Shrinking a 4K frame for
  the browser would also destroy the small phones and distant faces the
  detector needs.
- Tracking follows a constant-velocity prediction, so a person missed for a
  frame or two keeps their track id — and their phone-use timer — instead of
  starting over.

Tune with `.env` (see `.env.example`):

| Variable | Default | Effect |
| --- | --- | --- |
| `STREAM_MAX_WIDTH` | `1280` | Lower it (e.g. `960`) for less CPU and bandwidth. |
| `DETECT_MAX_WIDTH` | `1920` | Longest side used for detection and face matching; `0` = native. |
| `DETECT_INTERVAL` | `0.2` | Seconds between detector runs. Raise to `0.5` on slow CPUs; `0` runs every frame. |
| `FACE_INTERVAL` | `1.0` | Seconds between face-recognition runs. Raise to `2.0` to save CPU. |
| `TRACK_MAX_GAP` | `2.0` | Maximum gap in seconds to reconnect a lost track. |
| `TRACK_IDENTITY_TTL` | `30.0` | Forget a track's name if no face confirms it within this time. |
| `TRACK_VISUAL_THRESHOLD` | `0.88` | Similarity required for a visual reconnect; raise to be more cautious. |
| `RTSP_TRANSPORT` | `tcp` | Try `udp` on a wired LAN if your camera supports it. |
| `RTSP_LOW_LATENCY` | `1` | `0` restores FFmpeg's default buffering. |

Settings on the camera itself matter just as much:

- Use the camera's **sub-stream** (e.g. 1280×720 or 960×540) rather than 4K.
- Prefer **H.264** with a **1–2 second I-frame interval**; long GOPs and
  B-frames add decoder delay.
- Disable camera-side audio and any "smooth" / buffering mode.
- Check the camera's clock/NTP. A **constant** offset between the burned-in
  timestamp and your clock is a camera clock problem, not stream latency.
  A **growing** offset is a buffering problem.

## Face recognition

1. Run `bootstrap.py` as described above — it installs and verifies YuNet
   and SFace in `data/` on every supported operating system.

2. Open **Enrollment** and click **+ Enroll Person**, enter a name, upload
   images (or an optional short video showing the person clearly), and save.

3. In **Cameras**, select a camera and click **Run CV Model** — known people get a green box with their
   name; others get a red **Unknown** box.

Live labels distinguish `(face)` (fresh face match), `(tracked)` (same person
track after a face match), and amber `(visual)` (short-range clothing similarity
after a brief gap). Clothing similarity is **not** a trained Body ReID model
or a biometric identifier. It works only in the same camera, for recently
face-identified tracks; similar outfits can still cause false associations.
An unmatched or stale person is shown as **Unknown**, not guessed by clothing.

### Thresholds
- `FACE_MATCH_THRESHOLD` (default `0.40`) — lower = more permissive
- `FACE_MATCH_HIGH` (default `0.55`) — informational only for now
- Set via `.env` to override.

### Notes
- Enrollment rejects images where the face is < 64 px, blurry, or where
  more than one face is present (avoids mixing people).
- Face embeddings and cropped sample images are stored **plaintext** in
  `data/persons/<id>/` and in `data/cctv.db`. Fine on a trusted LAN.
- Deleting a person removes their files and embeddings immediately.

## Detection controls

In **Cameras → Detection controls**, independently enable or disable person
and phone-use detection. **Global** settings apply to all cameras and remove
old **Exclusive** overrides; Exclusive changes only selected saved cameras.
Global Off always takes precedence over an Exclusive On. Changes persist in
SQLite and apply immediately to live feeds, without stopping video. New
camera pipelines also load the effective settings on start. Person detection
Off skips person inference, face recognition, phone-use analysis, review
auto-capture, and entrance/exit crossing alerts; Phone-use Off alone leaves
person boxes, face recognition, and crossing alerts on. Re-enabling starts
fresh tracks and phone-use timers, avoiding old state triggering alerts.

## Unrecognised face review

In **Enrollment → Unrecognised face review**, opt in globally or for selected
cameras. Only a detected **single, sharp, sufficiently large face** that fails
recognition is eligible; an Unknown person box without a usable face is not.
Candidate images contain a padded face crop (not the whole CCTV frame) and
must pass the regular enrollment checks before being queued. The same tracked
person is throttled, highly similar crops are deduplicated across cameras,
and a per-camera rolling-hour cap (default 2, configurable from 1 to 30)
and 300-image storage ceiling prevent unbounded collection. The switch is
**off by default**; Global Off overrides Exclusive On. Person detection must
be active for that camera. Settings persist in SQLite and take effect on
running feeds without a restart.

Review each image in Enrollment: **Add to selected person**, **Enroll as new
person**, or **Discard image**. Assignment requires confirmation and invokes
the existing face-enrollment quality checks; no face is automatically added
to anyone's gallery. Accepting adds a new SFace embedding, **not** model
training. Candidates are stored in `data/face_candidates/` until assigned
or discarded; restricting dashboard access and obtaining permission for face
collection are the operator's responsibility. A tiny/distant face that fails
quality checks is intentionally not collected.

## Entrance / Exit camera roles

In **Cameras → Edit**, assign an **Entrance** or **Exit** role, load a frame
from the saved camera, and drag a virtual line from A to B across the doorway.
Choose **Yellow side → Blue side** or the reverse; the green arrow shows the
direction that counts. Saving the camera updates a running feed immediately.
The line is stored in normalized coordinates, so it also fits a resized live
feed. A tracked person's **bottom-center (feet)** must move across the drawn
segment in the chosen direction, with stable observations on each side. A
short dead zone and per-track cooldown reduce double alerts. Entrance sends
"Name entered"; Exit sends "Name left"; if no face was recognized it says
"Unknown". The existing Telegram group receives a snapshot with the person
box and line, with its usual administrator Confirm/Reject buttons. Telegram
must be configured and verified for alerts to be sent. Camera motion,
occlusion, inaccurate person boxes and missing face detail can still cause
missed crossings or unknown identities. If a camera moves, redraw its line.
Tick **Count as Customer** on an Entrance or Exit camera to tag its crossings as
customers; the flag is cleared automatically when the role is removed. The tag
can also be toggled per alert in Telegram before confirming. Removing the role
disables crossing alerts without affecting phone-use alerts.

## Attendance

**Attendance** lists every enrolled person with their current status
(**Inside** / **Outside** / **No record**), last entrance and exit (time and
camera), and today's counts. Only confirmed **Entrance** and **Exit** crossings
add records; phone-use events and cameras without a crossing role never do.

When an entrance or exit alert arrives in Telegram, a group administrator can:

* **Confirm** — for an automatically recognised enrolled person, this records
  the crossing in that person's attendance.
* **Specify person** — if the model showed **Unknown** (or the wrong enrolled
  person), open the paginated list of enrolled people, choose the correct one,
  then press **Confirm**. The selected identity is what gets recorded.
* **Count as customer** — toggle the customer tag on this crossing without
  confirming it. Pressing **Customer ✓ (remove)** clears the tag.
* **Reject** — saves the event as rejected and changes no attendance.

If the person is genuinely unknown, confirming without a selection saves the
event and lists it under **Unassigned crossings** in Attendance, but does not
add an attendance record. Unknown crossings also remain visible in Telegram →
Events. Selecting a person is only a proposal; nothing is recorded until
**Confirm** is pressed. Only group administrators can perform these actions.

The dashboard polls attendance while the section is open. Names are matched to
enrolled people at crossing time; duplicate enrolled names are treated as
ambiguous and must be selected explicitly. Deleting a person does not remove
their historical attendance rows, and removing a camera's role stops new
records from it.

## Overview and Customers

**Overview** is the landing section. It summarises enrolled people (**Inside**,
today's entrances and exits), customer crossings (confirmed today, pending, and
total) and event counts. It also shows the latest attendance activity, recent
events, recent customer crossings, and every **Not confirmed** event that is
still waiting for a Telegram decision.

**Customers** lists customer-tagged crossings with a status filter (**All**,
**Pending**, **Confirmed**, **Rejected**, **Failed**) and today's confirmed
customer entrances/exits. Only confirmed customer entrances count towards the
Overview customer total; pending crossings are shown separately. Unconfirmed
events are labelled **Not confirmed** everywhere instead of being hidden.

## Phone-use detection

### Optional pretrained phone-use classifier

For a live trial of the [MIT-licensed MobileNetV2 phone-use classifier](https://huggingface.co/sreenathsree1578/mobile-phone-usage-detector),
install its optional CPU runtime and verified TFLite weights from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-phone-classifier.txt
.\.venv\Scripts\python.exe -m backend.phone_classifier
```

Restart the server. The classifier runs on up to four largest person crops per
phone analysis (configure `PHONE_CLASSIFIER_MAX_PEOPLE`); it can detect use
without a visible phone box. Such detections are shown as **phone use (model)**,
not incorrectly called "calling" or "looking". Existing geometric detections
retain their labels and take priority. The existing episode/alert confirmation
still applies. `PHONE_CLASSIFIER_THRESHOLD` defaults to 0.80; tune against
false alarms on your feeds. The camera pipeline status includes
`phone_classifier: active` when the model loaded. If the weights or optional
runtime are absent, the original detector continues to run unchanged. This
classifier was not validated on your CCTV footage; its probabilities and
published accuracy are not calibrated to your cameras. Remove or rename the
weights to disable it (then restart the server).

### Training / Review & Label

The **Training** sidebar section accepts images or short clips (up to 20
seconds / 30 MB; up to eight 1-second frames). Choose a saved camera, then
label each image **Calling**, **Looking**, **No phone use**, **No person**, or **Unclear**
directly in the section. On a full scene, use **Draw person box** to select
exactly one person's area for that image's label; draw a separate box on each
frame from a clip. Each sample has one activity label and one optional person
box; upload the same frame again to label a different person. The saved box
crops the image during classifier training
and validation. It does *not* teach this binary classifier to detect phone
objects or enable phone bounding boxes in the live feed. If no box is drawn,
the entire image is used and should already be a single-person crop.

Camera ID and capture date are retained for held-out validation. The
**Automatic snapshot capture** controls in Training persist in SQLite and
take effect immediately. **Global** applies on/off and the rolling-hour
maximum to every camera and clears previous **Exclusive** overrides;
**Exclusive** configures only selected cameras. Global Off disables capture
on every camera, including ones marked On exclusively. Limits (1, 2, 3, 5,
or custom 1–120) apply **per camera per rolling hour**, persist across
restarts, and do not count manual uploads. They are combined with the
existing per-track throttling and `REVIEW_MAX_SAMPLES` storage limit; deleting
a sample does not reset its hourly quota. `REVIEW_CAPTURE=1` and
`REVIEW_CAPTURE_PER_HOUR=1` in `.env` provide initial defaults until the
Training UI saves a policy. Automatic capture is **off by default**.

If a sample contains no person (a false person detection), label it **No
person**, not No phone use. No-person and Unclear examples are retained for
review but **excluded** from binary phone-use training: this workflow does
not retrain the upstream person detector.
Samples are stored locally in `data/review/`; use **Delete image** to remove
them. Restrict access to this dashboard and obtain permission before recording
or uploading footage; the app does not add authentication to these endpoints.

After reviewing at least 20 positive and 20 negative images across camera-day
groups (with both classes in held-out groups), stop live cameras and click
**Train candidate**. An isolated worker trains a MobileNetV2 classification
head on ImageNet features (the publisher's `.h5` is incompatible with this
installation's Keras 3), exports a separate TFLite model, and compares
image-level recall and precision to the original on held-out camera-day
groups. Training can consume substantial CPU and may download ImageNet weights
on its first run. A candidate that fails the recall/precision gate cannot be
deployed. **Deploy validated candidate** is a separate explicit action; then
start the cameras to load it. **Roll back** restores the original when cameras
are stopped. The trained models and worker log live in `data/phone_candidates/`.
Even passing this check does not establish event-level recall: confirm live
alerts and false positives at different distances before relying on it.

The live detector also looks for the COCO `cell phone` class and raises a
phone-use state only when the geometry shows *active* use:

- **On a call** — the phone is beside the head at ear level, not centred in
  front of the face.
- **Looking at the phone** — the phone is below the face over the body, and
  YuNet face landmarks show the head pitched down toward it. A phone held low
  over the lap or waist counts, as long as the head is tilted down to it.

A phone hanging at the side, below the hip, or out to the side is **not**
counted, and neither is one the head is not turned toward. The person's box
turns amber and is labelled `[on call]` or `[looking at phone]` while the
state is active. Every detected phone is also boxed in the live feed; the one
responsible for the alert is highlighted and labelled `PHONE IN USE`.

Alerts are confirmed from a window of analyses rather than a single frame. An
episode starts at the first positive analysis and survives a dropout of up to
`PHONE_USE_EPISODE_SECONDS` (default 3 s), so a phone that flickers out of
detection no longer restarts the timer. Before alerting, the share of positive
analyses in the recent window must reach `PHONE_USE_EVIDENCE` (default 0.5)
with at least three positive analyses; a phone box that jumps to a different
part of the body counts as a new detection instead of extending the episode.
A per-person cooldown stops one long episode from spamming.

Each camera keeps its own **Phone sensitivity** (full-frame phone confidence)
and **Look-down threshold** (how much head tilt counts as looking); edit a
camera to change them. Lower values find more phone use, higher values are
stricter.

Because phones are tiny at CCTV distance, each person is re-scanned in a
tighter crop (the top 85% of the person box, with a little padding) at a lower
confidence threshold. The full-frame and crop results are merged, implausible
boxes (too large for the person, or outside their box) are discarded, and
overlapping duplicates are removed. `PHONE_CROP_MAX_PEOPLE` caps how many
people are re-scanned per frame to bound CPU use. The frame is also
searched in a small grid of tiles (`PHONE_TILE_GRID`, default 2x2), so a
phone held out to the side, or belonging to someone who did not fit in the
crop budget, is still looked for at close range; the same merge and
plausibility checks apply, and `PHONE_TILE_MAX` caps the extra passes.

This is a geometric heuristic, not a dedicated action-recognition model. The
YOLOv8n COCO phone class works best for reasonably large, well-lit phones;
long-range phones can still be missed, and small dark rectangles can be
confused with a phone. Raise `PHONE_CONF_THRESHOLD` if you see false positives,
or lower it if genuine phone use is missed. `PHONE_LOOK_PITCH_RATIO` controls
how much downward head tilt counts as looking (default `0.70`, high enough to
ignore someone merely holding a phone while walking).

## Telegram alerts

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot to the Telegram group that should receive alerts, then promote
   it to **administrator**. Telegram only guarantees `getChatMember` lookups
   for other users when the bot is an admin, and the app uses that call to
   verify who may confirm or reject an event.
3. Open **Telegram**, paste the token and the group ID, then click
   **Verify connection**. Numeric group IDs are negative: supergroups start
   with `-100`, while older basic groups look like `-123456789`. A positive
   number is a user ID, not a group. A public supergroup's `@username` also
   works — it is converted to the numeric ID when you verify.
4. When phone use is detected, the bot posts an annotated snapshot with
   **Confirm** and **Reject** buttons.

**Verify connection** checks the values currently in the form and only saves
them if Telegram accepts them, so a failed check never overwrites a working
configuration. Failures are shown in the card with the reason and what to
check:

| Message | Fix |
| --- | --- |
| Bot token rejected | Copy the token again from @BotFather; remove spaces or line breaks. |
| Telegram cannot find that group | Use the numeric, negative group ID. Add the bot to the group. If the group was upgraded to a supergroup, use the new `-100…` ID. |
| The bot is not a member | Add the bot to the group. |
| The bot is not an administrator | Promote the bot to administrator; Telegram only guarantees member checks for admins. |
| Could not reach Telegram | Allow outgoing HTTPS to `api.telegram.org`; check the firewall or proxy. |

Only group **administrators/creator** can use the buttons. Every click is
checked with Telegram's `getChatMember`; other members get an "only group
administrators" message and the event stays pending. **Telegram → Events**
shows all events with a status badge — **Not confirmed**, **Confirmed**,
**Rejected**, or **Failed** — together with the snapshot and the admin who
confirmed or rejected them.

The bot uses **long polling**, so no public URL, port forwarding or webhook
is required — the app only needs outgoing HTTPS access. If a webhook was
previously set for the bot, the app removes it automatically. The token is
stored in `data/cctv.db` and masked in the API/UI; snapshots are written to
`data/events/`. The web UI has no user authentication, so keep the app on
a trusted network or behind an authenticated HTTPS proxy.

Tune with `.env`:

| Variable | Default | Effect |
| --- | --- | --- |
| `PHONE_CONF_THRESHOLD` | `0.25` | Full-frame phone confidence. Raise to reduce false positives. |
| `PHONE_CROP_CONF_THRESHOLD` | `0.15` | Confidence for the per-person crop pass that finds small phones. |
| `PHONE_CROP_MAX_PEOPLE` | `4` | Largest people re-scanned for phones per frame (CPU bound). |
| `PHONE_TILE_GRID` | `2` | Frame split into an N×N tile grid for one more phone pass; `0` disables tiling. |
| `PHONE_TILE_MAX` | `1` | Tiles scanned per pass, busiest first; each costs about one crop pass. |
| `PHONE_TILE_OVERLAP` | `0.15` | Margin around each tile so a phone on a boundary is not cut. |
| `PHONE_TILE_CONF_THRESHOLD` | `0.20` | Confidence threshold for the tile pass. |
| `PHONE_USE_INTERVAL` | `0.5` | Seconds between phone geometry analyses. |
| `PHONE_USE_MIN_SECONDS` | `2.0` | How long active use must persist before alerting. |
| `PHONE_USE_GRACE_SECONDS` | `1.0` | Tolerate brief detector dropouts. |
| `PHONE_USE_EPISODE_SECONDS` | `3.0` | How long an episode survives a dropout before its timer restarts. |
| `PHONE_USE_EVIDENCE` | `0.5` | Share of recent analyses that must show phone use before alerting. |
| `PHONE_ALERT_COOLDOWN` | `120` | Seconds before the same person track can alert again. |
| `PHONE_LOOK_PITCH_RATIO` | `0.70` | Downward head-tilt threshold for "looking" (0 = eyes, 1 = mouth). |
