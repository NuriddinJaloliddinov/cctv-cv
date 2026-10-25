"""Phone-use and entrance/exit incident recording and Telegram dispatch."""
import html
import math
import uuid
from datetime import datetime
from pathlib import Path

import cv2

from . import db, telegram_bot

EVENTS_DIR = Path("data/events")
EVENTS_DIR.mkdir(parents=True, exist_ok=True)

_ACTIVITY = {
    "calling": "On a call (phone at ear)",
    "looking": "Looking at phone",
    "using": "Phone use (model prediction; activity unspecified)",
}


def _caption(event: dict) -> str:
    activity = _ACTIVITY.get(event["kind"], event["kind"])
    return "\n".join([
        "🚨 <b>Phone use detected</b>",
        f"Camera: <b>{html.escape(event['camera_name'])}</b>",
        f"Person: {html.escape(event['person_name'])}",
        f"Activity: {activity}",
        f"Confidence: {event['confidence']:.0%}",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Only group administrators can confirm or reject this event.",
    ])


def _annotate(frame, person_box, phone_box, kind: str, camera_name: str):
    image = frame.copy()
    if person_box:
        x1, y1, x2, y2 = (int(v) for v in person_box[:4])
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 165, 255), 2)
    if phone_box:
        px1, py1, px2, py2 = (int(v) for v in phone_box[:4])
        cv2.rectangle(image, (px1, py1), (px2, py2), (0, 255, 255), 2)
        cv2.putText(image, "phone", (px1, max(py1 - 5, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1,
                    cv2.LINE_AA)

    label = ("PHONE USE: CALLING" if kind == "calling" else
             "PHONE USE: LOOKING AT PHONE" if kind == "looking" else
             "PHONE USE: MODEL PREDICTION")
    cv2.rectangle(image, (0, 0), (image.shape[1], 28), (20, 20, 20), -1)
    cv2.putText(image, f"{label}  |  {camera_name}",
                (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 1,
                cv2.LINE_AA)
    return image


def _save_snapshot(event_id: str, frame, person_box, phone_box,
                   kind: str, camera_name: str) -> str | None:
    try:
        image = _annotate(frame, person_box, phone_box, kind, camera_name)
        if image.shape[1] > 1280:
            scale = 1280.0 / image.shape[1]
            image = cv2.resize(
                image, (1280, max(1, int(image.shape[0] * scale))),
                interpolation=cv2.INTER_AREA)
        path = EVENTS_DIR / f"{event_id}.jpg"
        if not cv2.imwrite(str(path), image,
                           [int(cv2.IMWRITE_JPEG_QUALITY), 85]):
            return None
        return str(path)
    except Exception as e:
        print(f"[phone] could not save snapshot: {e}")
        return None


def report_phone_use(camera_id: str, camera_name: str, track_id: int,
                     person_name: str, kind: str, score: float,
                     frame, person_box, phone_box) -> dict | None:
    """Persist an incident and queue a Telegram alert.

    Returns the event row, or None when Telegram is not configured (in that
    case no event is stored, because nobody could confirm it).
    """
    if not telegram_bot.service.is_configured():
        return None

    event_id = uuid.uuid4().hex
    snapshot_path = _save_snapshot(
        event_id, frame, person_box, phone_box, kind, camera_name)
    event = db.create_event(
        event_id=event_id,
        camera_id=camera_id,
        camera_name=camera_name or camera_id,
        track_id=int(track_id) if track_id is not None else None,
        person_name=person_name or "Unknown",
        kind=kind,
        confidence=float(score),
        snapshot_path=snapshot_path,
        caption="",                      # filled below from the stored row
    )
    caption = _caption(event)
    db.update_event(event_id, {"caption": caption})
    event["caption"] = caption
    telegram_bot.service.notify(event_id, snapshot_path, caption)
    print(f"[phone] {camera_name}: {kind} alert for "
          f"{person_name or 'Unknown'} (event {event_id[:8]})")
    return event


def report_crossing(camera_id: str, camera_name: str, track_id: int,
                    person_name: str, role: str, score: float, frame,
                    person_box, line, direction: str,
                    count_customer: bool = False) -> dict | None:
    """Send an entrance/exit photo using the existing Telegram review queue."""
    if not telegram_bot.service.is_configured():
        return None
    action = "entered" if role == "entrance" else "left"
    is_customer = bool(count_customer and action in db.ATTENDANCE_KINDS)
    event_id = uuid.uuid4().hex
    snapshot_path = None
    try:
        image = frame.copy()
        h, w = image.shape[:2]
        ax, ay, bx, by = line
        a, b = (int(ax * w), int(ay * h)), (int(bx * w), int(by * h))
        cv2.line(image, a, b, (0, 210, 255), 3)
        dx, dy = bx - ax, by - ay
        length = max(math.hypot(dx, dy), .001)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        side_points = [
            (int((mx - dy / length * .045) * w),
             int((my + dx / length * .045) * h)),
            (int((mx + dy / length * .045) * w),
             int((my - dx / length * .045) * h)),
        ]
        src, dst = ((0, 1) if direction == "positive_to_negative"
                    else (1, 0))
        cv2.arrowedLine(image, side_points[src], side_points[dst],
                        (0, 255, 0), 3, tipLength=.3)
        x1, y1, x2, y2 = map(int, person_box[:4])
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 100), 3)
        label = f"{person_name or 'Unknown'} {action}"
        cv2.putText(image, label, (max(0, x1), max(25, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 100), 2, cv2.LINE_AA)
        if w > 1280:
            image = cv2.resize(image, (1280, max(1, int(h * 1280 / w))),
                               interpolation=cv2.INTER_AREA)
        path = EVENTS_DIR / f"{event_id}.jpg"
        if cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 85]):
            snapshot_path = str(path)
    except Exception as exc:
        print(f"[crossing] could not save snapshot: {exc}")
    display_name = person_name or "Unknown"
    recognized_person_id = db.find_person_id_by_name(display_name)
    event = db.create_event(
        event_id=event_id, camera_id=camera_id, camera_name=camera_name,
        track_id=track_id, person_name=display_name, kind=action,
        confidence=float(score), snapshot_path=snapshot_path, caption="",
        recognized_person_id=recognized_person_id,
        recognized_person_name=display_name,
        customer=is_customer)
    customer_line = f"\n🛍 Customer: <b>{'Yes' if is_customer else 'No'}</b>"
    caption = (f"🚨 <b>{html.escape(display_name)} {action}</b>\n"
               f"Camera: <b>{html.escape(camera_name)}</b> ({role.title()})"
               f"{customer_line}\n"
               f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
               "Confirm to record attendance for enrolled people. If this is "
               "an enrolled person the model missed, choose <b>Specify "
               "person</b> first. Use <b>Count as customer</b> to include this "
               "crossing in the Customers section.\n\n"
               "Only group administrators can confirm or reject this event.")
    db.update_event(event_id, {"caption": caption})
    event["caption"] = caption
    telegram_bot.service.notify(event_id, snapshot_path, caption)
    return event
