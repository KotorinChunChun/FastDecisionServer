"""文字・質問・画像の境界を実際の契約で検証する。"""
import base64
import unittest
from unittest.mock import patch

from tests.support import image_uri, payload
from fds.contracts import DecisionRequest, prepare_images
from pydantic import ValidationError


class RequestContractTests(unittest.TestCase):
    def test_three_question_types_and_explicit_cpu_are_accepted(self):
        body = DecisionRequest(**payload(questions={
            "yes": {"type": "noul", "criteria": {"false": "違う", "true": "そう"}},
            "kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}},
            "level": {"type": "score", "criteria": ["低", "中", "高"]},
        }))
        self.assertEqual([q.type for q in body.questions.values()], ["noul", "choice", "score"])
        self.assertEqual(body.device, "cpu")

    def test_state_character_limit_and_utf8_total_limit(self):
        self.assertEqual(len(DecisionRequest(**payload(state="x" * 24000)).state), 24000)
        for state in ["x" * 24001, "あ" * 24000]:
            with self.subTest(length=len(state)), self.assertRaises(ValidationError):
                DecisionRequest(**payload(state=state))

    def test_question_count_and_ids_are_bounded(self):
        for questions in [{}, {str(i): {"type": "noul"} for i in range(9)},
                          {"": {"type": "noul"}}, {"a" * 101: {"type": "noul"}}]:
            with self.subTest(questions=list(questions)), self.assertRaises(ValidationError):
                DecisionRequest(**payload(questions=questions))
        self.assertEqual(len(DecisionRequest(**payload(questions={str(i): {"type": "noul"} for i in range(8)})).questions), 8)

    def test_question_specific_invalid_values_are_rejected(self):
        questions = [
            {"type": "choice", "criteria": {"a": "one"}},
            {"type": "choice", "criteria": {f"k{i}": "x" for i in range(65)}},
            {"type": "choice", "criteria": {"1": "one", "b": "two"}},
            {"type": "choice", "criteria": {"": "one", "b": "two"}},
            {"type": "choice", "criteria": {"a": "x" * 1001, "b": "two"}},
            {"type": "noul", "criteria": {"true": "yes"}},
            {"type": "noul", "criteria": {"true": "yes", "false": "no", "other": "?"}},
            {"type": "noul", "criteria": {"true": "x" * 1001, "false": "no"}},
            {"type": "score", "criteria": ["one"]},
            {"type": "score", "criteria": ["x"] * 11},
            {"type": "score", "criteria": ["x" * 1001, "y"]},
            {"type": "noul", "instructions": "x" * 3001},
            {"type": "other"}, {"type": "noul", "unexpected": True},
        ]
        for question in questions:
            with self.subTest(question=question["type"]), self.assertRaises(ValidationError):
                DecisionRequest(**payload(questions={"test": question}))

    def test_enums_timeouts_images_and_unknown_fields_are_bounded(self):
        for change in [{"device": "gpu"}, {"priority": "urgent"}, {"model": ""}, {"model": "m" * 101},
                       {"timeout_seconds": 0}, {"timeout_seconds": 301}, {"timeout_seconds": True},
                       {"timeout_seconds": 1.5}, {"images": ["x"] * 5}, {"unexpected": "secret"}]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                DecisionRequest(**payload(**change))


class ImageContractTests(unittest.TestCase):
    def test_supported_images_are_normalized_to_rgb_and_scaled(self):
        for format in ["PNG", "JPEG", "WEBP"]:
            with self.subTest(format=format):
                result = prepare_images([image_uri(size=(2048, 1024), format=format)])[0]
                self.assertEqual(result.mode, "RGB")
                self.assertEqual(result.size, (1024, 512))

    def test_transparency_is_composited_on_white(self):
        result = prepare_images([image_uri(color=(0, 0, 0, 0))])[0]
        self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))

    def test_paths_urls_invalid_base64_and_nonimages_are_rejected(self):
        invalid = ["C:/image.png", "https://example.invalid/image.png", "data:image/png;base64,%%%",
                   "data:image/png;base64," + base64.b64encode(b"not an image").decode(),
                   image_uri(format="BMP"), image_uri(format="BMP").replace("image/bmp", "image/png")]
        for value in invalid:
            with self.subTest(value=value[:70]), self.assertRaises(ValueError):
                prepare_images([value])

    def test_per_image_byte_limit_is_enforced_before_decoding(self):
        with patch("fds.contracts.base64.b64decode", return_value=b"x" * 8_000_001):
            with self.assertRaisesRegex(ValueError, "8MB"):
                prepare_images(["data:image/png;base64,eA=="])

    def test_total_byte_limit_is_enforced(self):
        # PNG の後ろに無害なパディングを加え、復号後合計だけを超過する。
        png = base64.b64decode(image_uri().partition(",")[2])
        raw = png + b"\0" * (5_400_000 - len(png))
        uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        with self.assertRaisesRegex(ValueError, "16MB"):
            prepare_images([uri] * 3)

    def test_pixel_limit_is_enforced_before_expanding_image(self):
        from PIL import Image
        with patch("fds.contracts.Image.open") as open_image:
            opened = open_image.return_value.__enter__.return_value
            opened.format, opened.width, opened.height = "PNG", 4001, 4000
            with self.assertRaisesRegex(ValueError, "画素数"):
                prepare_images([image_uri()])
            opened.verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
