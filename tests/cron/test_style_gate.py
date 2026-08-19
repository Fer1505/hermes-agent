"""Stage-4 plain-talk outbound style gate (Olympus).

Long cron chat sends must become a short takeaway plus the full report as a
document attachment — enforced server-side at the delivery chokepoint, not by
doctrine. The gate fails open: delivery must never break because of it.
"""

from unittest.mock import patch

import pytest

from cron.scheduler import (
    _STYLE_GATE_DEFAULT_CEILING,
    _STYLE_GATE_TAKEAWAY_LIMIT,
    _apply_cron_outbound_style_gate,
    _cron_outbound_length_ceiling,
    _style_gate_takeaway,
)


LONG_PARAGRAPH = (
    "Fleet health is stable today. Two trucks need attention this week. "
)
LONG_REPORT = "Today's summary is fine overall.\n\n" + (LONG_PARAGRAPH * 120)


class TestCeilingResolution:
    def test_default(self):
        with patch("cron.scheduler.load_config", return_value={}):
            assert _cron_outbound_length_ceiling({}) == _STYLE_GATE_DEFAULT_CEILING

    def test_config_override(self):
        cfg = {"cron": {"outbound_length_ceiling": 500}}
        with patch("cron.scheduler.load_config", return_value=cfg):
            assert _cron_outbound_length_ceiling({}) == 500

    def test_job_override_wins(self):
        cfg = {"cron": {"outbound_length_ceiling": 500}}
        with patch("cron.scheduler.load_config", return_value=cfg):
            assert _cron_outbound_length_ceiling({"outbound_length_ceiling": 9000}) == 9000

    def test_zero_disables(self):
        with patch("cron.scheduler.load_config", return_value={}):
            assert _cron_outbound_length_ceiling({"outbound_length_ceiling": 0}) == 0

    def test_garbage_falls_back(self):
        with patch("cron.scheduler.load_config", return_value={}):
            assert (
                _cron_outbound_length_ceiling({"outbound_length_ceiling": "many"})
                == _STYLE_GATE_DEFAULT_CEILING
            )

    def test_config_load_failure_falls_back(self):
        with patch("cron.scheduler.load_config", side_effect=RuntimeError("boom")):
            assert _cron_outbound_length_ceiling({}) == _STYLE_GATE_DEFAULT_CEILING


class TestTakeaway:
    def test_short_first_paragraph_pulls_more(self):
        text = "Lead line.\n\nSecond paragraph with the detail that matters here."
        out = _style_gate_takeaway(text)
        assert "Lead line." in out
        assert "Second paragraph" in out

    def test_respects_hard_limit(self):
        out = _style_gate_takeaway(LONG_PARAGRAPH * 40)
        assert len(out) <= _STYLE_GATE_TAKEAWAY_LIMIT
        # trimmed on a sentence boundary, not mid-word
        assert out.endswith(".") or out.endswith("…")


class TestApplyGate:
    def _job(self, **extra):
        return {"id": "style-gate-test", "name": "style gate test", **extra}

    def test_short_content_unchanged(self, tmp_path):
        with patch("cron.scheduler.load_config", return_value={}), \
             patch("cron.jobs._job_output_dir", return_value=tmp_path):
            out = _apply_cron_outbound_style_gate(self._job(), "All good today.")
        assert out == "All good today."

    def test_long_content_becomes_takeaway_plus_attachment(self, tmp_path):
        with patch("cron.scheduler.load_config", return_value={}), \
             patch("cron.jobs._job_output_dir", return_value=tmp_path):
            out = _apply_cron_outbound_style_gate(self._job(), LONG_REPORT)
        assert out != LONG_REPORT
        assert "Full report attached." in out
        assert "[[as_document]]" in out
        media_lines = [l for l in out.splitlines() if l.startswith("MEDIA:")]
        assert len(media_lines) == 1
        report_path = media_lines[0][len("MEDIA:"):]
        with open(report_path, encoding="utf-8") as f:
            assert f.read().strip() == LONG_REPORT.strip()
        # chat part stays short: everything before the attachment block
        chat_part = out.split("Full report attached.")[0]
        assert len(chat_part) <= _STYLE_GATE_TAKEAWAY_LIMIT + 10

    def test_existing_media_tags_ride_along(self, tmp_path):
        content = LONG_REPORT + "\nMEDIA:/tmp/chart.png"
        with patch("cron.scheduler.load_config", return_value={}), \
             patch("cron.jobs._job_output_dir", return_value=tmp_path):
            out = _apply_cron_outbound_style_gate(self._job(), content)
        media_lines = [l for l in out.splitlines() if l.startswith("MEDIA:")]
        assert len(media_lines) == 2
        assert media_lines[1] == "MEDIA:/tmp/chart.png"

    def test_ceiling_zero_disables(self, tmp_path):
        with patch("cron.scheduler.load_config", return_value={}), \
             patch("cron.jobs._job_output_dir", return_value=tmp_path):
            out = _apply_cron_outbound_style_gate(
                self._job(outbound_length_ceiling=0), LONG_REPORT
            )
        assert out == LONG_REPORT

    def test_fails_open_when_report_cannot_be_written(self):
        with patch("cron.scheduler.load_config", return_value={}), \
             patch("cron.jobs._job_output_dir", side_effect=OSError("disk")):
            out = _apply_cron_outbound_style_gate(self._job(), LONG_REPORT)
        assert out == LONG_REPORT


class TestDeliveryWiring:
    def test_deliver_result_applies_gate_before_framing(self):
        """_deliver_result must route content through the gate ahead of any
        wrapper framing, for every delivery lane."""
        from cron import scheduler

        job = {"id": "j-wire", "name": "wire", "deliver": "local"}
        with patch.object(
            scheduler, "_apply_cron_outbound_style_gate", return_value="gated"
        ) as gate, patch.object(
            scheduler, "_resolve_delivery_targets", return_value=[]
        ):
            # local-only: returns before delivery but AFTER target resolution;
            # gate must not have fired for a no-target job.
            assert scheduler._deliver_result(job, "content") is None
            gate.assert_not_called()

        targets = [{"platform": "telegram", "chat_id": "1"}]
        seen: list[str] = []

        def capture_extract(content):
            seen.append(content)
            raise RuntimeError("stop after capture")

        with patch.object(
            scheduler, "_apply_cron_outbound_style_gate", return_value="GATED"
        ) as gate, patch.object(
            scheduler, "_resolve_delivery_targets", return_value=targets
        ), patch(
            "gateway.media_policy.apply_media_policy_env"
        ), patch(
            "gateway.platforms.base.BasePlatformAdapter.extract_media",
            side_effect=capture_extract,
        ):
            # The sentinel abort may surface as a return-value error string or
            # an exception depending on _deliver_result's error handling —
            # either is fine, the capture is the assertion.
            try:
                scheduler._deliver_result(job, "content")
            except RuntimeError:
                pass
            gate.assert_called_once()
            assert gate.call_args.args[1] == "content"
            assert seen and "GATED" in seen[0]
