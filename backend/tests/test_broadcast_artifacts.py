from __future__ import annotations

import unittest

from app.broadcast import (
    _reconcile_generated_links,
    _requested_file_generator,
    _tool_created_requested_file,
)


class BroadcastArtifactTests(unittest.TestCase):
    def test_follow_up_powerpoint_request_requires_pptx_generator(self) -> None:
        message = {"role": "user", "content": "give me icons as wlel in the powerpoint"}

        self.assertEqual(_requested_file_generator(message), "generate_pptx")

    def test_image_attempt_does_not_satisfy_powerpoint_request(self) -> None:
        rows = [
            (
                "generate_image",
                {"prompt": "icons"},
                {"result": "Image generation isn't configured.", "citations": None},
            )
        ]

        self.assertFalse(_tool_created_requested_file(rows, "generate_pptx"))

    def test_fabricated_pptx_link_is_replaced_with_tool_link(self) -> None:
        bogus = (
            "[Download deck](/api/files/2b26722cd9284f1cba1a4c5aa4a8ffe3.pptx"
            "?name=missing.pptx)"
        )
        real = (
            "Download fixed.pptx",
            "/api/files/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pptx?name=fixed.pptx",
        )

        fixed, missing = _reconcile_generated_links(bogus, [real])

        self.assertIn(real[1], fixed)
        self.assertNotIn("2b26722cd9284f1cba1a4c5aa4a8ffe3", fixed)
        self.assertEqual(missing, [])

    def test_fabricated_file_link_is_unlinked_without_tool_result(self) -> None:
        bogus = (
            "[Download deck](/api/files/2b26722cd9284f1cba1a4c5aa4a8ffe3.pptx"
            "?name=missing.pptx)"
        )

        fixed, missing = _reconcile_generated_links(bogus, [])

        self.assertEqual(fixed, "Download deck")
        self.assertEqual(missing, [])

    def test_image_link_cannot_replace_fabricated_pptx_link(self) -> None:
        bogus = (
            "[Download deck](/api/files/2b26722cd9284f1cba1a4c5aa4a8ffe3.pptx"
            "?name=missing.pptx)"
        )
        image = (
            "Download icons.png",
            "/api/files/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png?name=icons.png",
        )

        fixed, missing = _reconcile_generated_links(bogus, [image])

        self.assertEqual(fixed, "Download deck")
        self.assertEqual(missing, [image])


if __name__ == "__main__":
    unittest.main()