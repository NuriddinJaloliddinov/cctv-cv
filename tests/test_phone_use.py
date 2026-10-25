"""Geometry tests for phone-use classification (no models or camera needed)."""
import unittest

from backend.phone_use import (classify, filter_phones, head_pitch_ratio,
                               head_yaw_ratio, _associate)

PERSON = (100, 100, 300, 600)          # x1, y1, x2, y2, w=200, h=500


def face_row(eye_y=145, nose_y=170, mouth_y=190, nose_x=200,
             x=170, y=120, w=60, h=70):
    return [
        x, y, w, h,
        185, eye_y, 215, eye_y,        # right eye, left eye
        nose_x, nose_y,                # nose tip
        185, mouth_y, 215, mouth_y,    # mouth corners
        0.99,
    ]


def phone(cx, cy, w=40, h=70):
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, 0.8)


class PhoneUseTests(unittest.TestCase):
    def test_phone_at_ear_is_a_call(self):
        # Phone to the left of the face, at eye/ear level.
        self.assertEqual(
            classify(PERSON, phone(152, 158), face_row()), "calling")

    def test_phone_centred_in_front_of_face_is_not_a_call(self):
        # Same height, but centred in front of the face: not at an ear.
        self.assertIsNone(classify(PERSON, phone(200, 158), face_row()))

    def test_phone_at_chest_while_looking_down_counts(self):
        pitched_down = face_row(eye_y=145, nose_y=182, mouth_y=195)
        self.assertEqual(
            classify(PERSON, phone(205, 295), pitched_down), "looking")

    def test_phone_at_waist_with_upright_head_is_ignored(self):
        upright = face_row(eye_y=145, nose_y=170, mouth_y=190)
        self.assertIsNone(classify(PERSON, phone(205, 395), upright))

    def test_phone_held_at_side_is_ignored(self):
        self.assertIsNone(
            classify(PERSON, phone(40, 430), face_row()), "looking")

    def test_ear_phone_without_a_visible_face_counts_in_a_closeup(self):
        # Head-and-shoulders crop: the phone is lateral but still near the
        # person's centreline.
        person = (100, 100, 500, 500)
        self.assertEqual(classify(person, phone(340, 290)), "calling")

    def test_phone_far_out_at_the_side_without_a_face_is_ignored(self):
        person = (100, 100, 500, 500)
        self.assertIsNone(classify(person, phone(80, 290)))

    def test_looking_requires_a_strong_downward_pitch(self):
        phone_at_chest = phone(205, 295)
        slightly_down = face_row(eye_y=145, nose_y=178, mouth_y=195)
        self.assertIsNone(classify(PERSON, phone_at_chest, slightly_down))
        clearly_down = face_row(eye_y=145, nose_y=182, mouth_y=195)
        self.assertEqual(
            classify(PERSON, phone_at_chest, clearly_down), "looking")

    def test_implausibly_large_phone_box_is_discarded(self):
        huge = (90, 80, 310, 620, 0.9)          # covers most of the person
        real = phone(200, 300)
        kept = filter_phones([PERSON], [huge, real])
        self.assertEqual([tuple(round(v, 2) for v in p) for p in kept],
                         [tuple(round(v, 2) for v in real)])

    def test_overlapping_phone_boxes_are_deduplicated(self):
        weak = (180, 260, 220, 330, 0.4)
        strong = (181, 261, 221, 331, 0.8)
        kept = filter_phones([PERSON], [weak, strong])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][4], 0.8)

    def test_head_pitch_and_yaw_helpers(self):
        upright = face_row()
        down = face_row(nose_y=185, mouth_y=195)
        self.assertAlmostEqual(head_pitch_ratio(upright), 0.556, places=3)
        self.assertGreater(head_pitch_ratio(down), head_pitch_ratio(upright))
        self.assertAlmostEqual(head_yaw_ratio(upright), 0.0, places=3)
        self.assertGreater(head_yaw_ratio(face_row(nose_x=225)), 0.3)

    def test_phone_is_assigned_to_the_nearest_person_only(self):
        left = (100, 100, 220, 600)
        right = (300, 100, 420, 600)
        phones = [phone(330, 300)]
        owners = _associate([left, right], phones)
        self.assertEqual(owners[0], [])
        self.assertEqual(len(owners[1]), 1)

        middle = phone(260, 300)
        owners = _associate([left, right], [middle])
        self.assertEqual(sum(len(x) for x in owners), 1)


class WaistPhoneTests(unittest.TestCase):
    """A phone held low over the lap counts when the head is tilted down."""

    def test_waist_phone_with_a_pitched_down_head_counts(self):
        pitched = face_row(eye_y=145, nose_y=182, mouth_y=195)
        self.assertEqual(classify(PERSON, phone(205, 395), pitched), "looking")

    def test_phone_at_the_thigh_is_still_ignored(self):
        pitched = face_row(eye_y=145, nose_y=182, mouth_y=195)
        self.assertIsNone(classify(PERSON, phone(205, 590), pitched))

    def test_narrow_profile_person_keeps_a_phone_held_in_front(self):
        # In profile the person box is narrow, so a width cap taken only from
        # that box would discard a real phone.
        person = (140, 100, 240, 600)
        pitched = face_row(eye_y=145, nose_y=182, mouth_y=195, x=160, w=60)
        self.assertEqual(classify(person, phone(195, 295, w=50), pitched),
                         "looking")

    def test_width_cap_uses_the_person_height_for_narrow_boxes(self):
        person = (140, 100, 240, 600)          # w=100, h=500
        wide = phone(195, 295, w=56)           # 56% of the person's width
        self.assertEqual(len(filter_phones([person], [wide])), 1)


if __name__ == "__main__":
    unittest.main()
