"""Directional, per-track virtual tripwire using person-box footpoints."""
import math


ROLES = {"entrance", "exit"}
DIRECTIONS = {"positive_to_negative", "negative_to_positive"}
DEAD_ZONE = 0.015        # frame-diagonal-normalized distance from the line
MAX_GAP = 1.2           # don't infer crossings over long occlusions
COOLDOWN = 20.0         # at most one alert per track per crossing episode


def validate(role, line, direction):
    if role is None:
        if line is not None or direction is not None:
            raise ValueError("Clear the line and direction when removing the role")
        return
    if role not in ROLES or direction not in DIRECTIONS:
        raise ValueError("Choose Entrance or Exit and a valid crossing direction")
    if (not isinstance(line, (tuple, list)) or len(line) != 4
            or any(isinstance(n, bool) or not isinstance(n, (int, float))
                   or not math.isfinite(n) or n < 0 or n > 1 for n in line)
            or math.hypot(line[2] - line[0], line[3] - line[1]) < .08):
        raise ValueError("Draw a line of at least 8% of the frame inside the image")


def foot(box, shape):
    height, width = shape[:2]
    return ((box[0] + box[2]) / (2.0 * width), box[3] / height)


class CrossingMonitor:
    def __init__(self, role=None, line=None, direction=None):
        validate(role, line, direction)
        self.role = role
        self.line = tuple(line) if line is not None else None
        self.direction = direction
        self.tracks = {}

    def _distance(self, point):
        ax, ay, bx, by = self.line
        dx, dy = bx - ax, by - ay
        return (dx * (point[1] - ay) - dy * (point[0] - ax)) / math.hypot(dx, dy)

    def _on_segment(self, old, new):
        a, b = self._distance(old), self._distance(new)
        if a == b:
            return False
        portion = a / (a - b)
        x = old[0] + portion * (new[0] - old[0])
        y = old[1] + portion * (new[1] - old[1])
        ax, ay, bx, by = self.line
        t = ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / (
            (bx - ax) ** 2 + (by - ay) ** 2)
        return 0 <= portion <= 1 and 0 <= t <= 1

    def update(self, track_ids, boxes, shape, now):
        """Return (index, track_id) for confirmed crossings in configured direction."""
        if not self.role or not self.line:
            return []
        active = set(track_ids)
        for tid, state in list(self.tracks.items()):
            if tid not in active and now - state["at"] > MAX_GAP:
                del self.tracks[tid]
        hits = []
        required_from = 1 if self.direction == "positive_to_negative" else -1
        for i, (tid, box) in enumerate(zip(track_ids, boxes)):
            point = foot(box, shape)
            if not 0 <= point[0] <= 1 or not 0 <= point[1] <= 1:
                continue
            distance = self._distance(point)
            side = 1 if distance > DEAD_ZONE else -1 if distance < -DEAD_ZONE else 0
            state = self.tracks.get(tid)
            if state is None or now - state["at"] > MAX_GAP:
                self.tracks[tid] = {"side": side, "count": 1 if side else 0,
                                    "point": point, "at": now,
                                    "pending": None, "cooldown": 0.0}
                continue  # never alert on a newly seen track
            previous_point = state["point"]
            previous_side = state["side"]
            if side == 0:
                # Keep the last reliable point/time; a long visit to the line
                # must not become a crossing across an unseen interval.
                continue
            state["point"], state["at"] = point, now
            pending = state["pending"]
            if pending is not None:
                if side == pending:
                    state["pending_count"] += 1
                    if state["pending_count"] >= 2:
                        state["side"], state["count"] = side, 2
                        state["pending"] = None
                        if now >= state["cooldown"]:
                            state["cooldown"] = now + COOLDOWN
                            hits.append((i, tid))
                else:
                    state["pending"] = None
                    state["side"], state["count"] = side, 1
                continue
            if side == previous_side:
                state["count"] = min(3, state["count"] + 1)
                continue
            if (previous_side == required_from and side == -required_from
                    and state["count"] >= 2 and now >= state["cooldown"]
                    and math.dist(previous_point, point) <= .35
                    and self._on_segment(previous_point, point)):
                state["pending"] = side
                state["pending_count"] = 1
            else:
                state["side"], state["count"] = side, 1
        return hits
