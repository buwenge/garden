import unittest

import garden_delivery


class GardenConfirmationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_error_does_not_stop_the_normal_reply_path(self):
        async def failing_to_thread(*_args):
            raise OSError("temporary garden write failure")

        warnings = []
        confirmed = await garden_delivery.confirm_event_safely(
            {"event_id": "bond:cat001:level:1", "delivery_token": "lease-token"},
            confirm=lambda _event_id, _token: True,
            to_thread=failing_to_thread,
            warn=lambda message, error: warnings.append((message, str(error))),
        )
        normal_reply_path = []

        async def send_normal_reply_and_schedule():
            normal_reply_path.append("sent_and_scheduled")

        await send_normal_reply_and_schedule()
        self.assertFalse(confirmed)
        self.assertEqual(normal_reply_path, ["sent_and_scheduled"])
        self.assertEqual(warnings[0][1], "temporary garden write failure")


if __name__ == "__main__":
    unittest.main()
