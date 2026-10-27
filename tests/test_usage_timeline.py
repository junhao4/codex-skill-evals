"""Regression coverage for the shared clock and retained trace content."""

from html.parser import HTMLParser

from _lib.usage_html import _timeline_component
from _lib.usage import _rollout_inspection


def test_late_session_settings_do_not_extend_agent_work_span():
    inspection = _rollout_inspection([
        {"timestamp": "2026-10-26T00:00:00Z", "type": "session_meta", "payload": {}},
        {"timestamp": "2026-10-26T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-10-26T00:01:50Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done"}},
        {"timestamp": "2026-10-26T02:03:20Z", "type": "event_msg", "payload": {"type": "thread_settings_applied"}},
    ])
    assert inspection["start_ts"] == "2026-10-26T00:00:01Z"
    assert inspection["end_ts"] == "2026-10-26T00:01:50Z"


def test_unfinished_task_uses_latest_observed_activity():
    inspection = _rollout_inspection([
        {"timestamp": "2026-10-26T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-10-26T00:00:10Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "Working"}]}},
        {"timestamp": "2026-10-26T02:00:00Z", "type": "turn_context", "payload": {}},
    ])
    assert inspection["end_ts"] == "2026-10-26T00:00:10Z"


class EventRows(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.rows = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "u-event" in attrs.get("class", "").split():
            self.rows.append(attrs)


def test_child_event_positions_use_run_clock_and_preserve_every_event():
    source = _timeline_component({
        "start_ts": "2026-10-26T00:00:00Z",
        "end_ts": "2026-10-26T00:01:40Z",
        "children": [{
            "start_ts": "2026-10-26T00:00:20Z",
            "end_ts": "2026-10-26T00:00:28Z",
            "events": [
                {"ts": "2026-10-26T00:00:22Z", "kind": "cmd", "command": "cat <file>", "output": "first line\n" + "long output " * 600},
                {"ts": "2026-10-26T00:00:21Z", "kind": "reason", "text": "Recorded summary"},
            ],
            "final_message": "Complete response " * 100,
        }],
        "final_message": "Run complete",
    }, "example")
    rows = EventRows(source).rows
    assert [row["data-time"] for row in rows] == ["100.0", "21.0", "22.0", "28.0"]
    assert [float(row["data-position"]) for row in rows] == [99.7, 21.0, 22.0, 28.0]
    assert "cat &lt;file&gt;" in source
    assert "first line\n" + "long output " * 600 in source
    assert "Complete response " * 100 in source
    assert "<details" not in source


def test_event_without_timestamp_does_not_invent_a_map_position():
    source = _timeline_component({
        "start_ts": "2026-10-26T00:00:00Z",
        "end_ts": "2026-10-26T00:00:10Z",
        "events": [{"kind": "cmd", "command": "pwd", "output": "/workspace"}],
    }, "missing")
    row, = EventRows(source).rows
    assert "data-position" not in row
    assert "onclick" not in row
    assert "pwd" in source and "/workspace" in source


def test_lane_packing_reuses_waves_and_separates_overlap_and_unknowns():
    from _lib.usage_html import _pack_timeline_lanes
    def actor(start, end, known=True):
        return {'_s': start, '_e': end, '_start': start if known else None,
                '_end': end if known else None, '_column': 0}
    main = actor(0, 100)
    first, overlapping, later = actor(1, 10), actor(2, 8), actor(10, 20)
    unknown, early = actor(0, .2, False), actor(0, 1)
    columns = _pack_timeline_lanes([main, first, overlapping, later, unknown, early])
    assert columns[0] == [main]
    assert first['_column'] == later['_column'] == early['_column']
    assert overlapping['_column'] != first['_column']
    assert columns[unknown['_column']] == [unknown]


def test_minimum_height_bars_stay_inside_map():
    from _lib.usage_html import _timeline_bar_geometry
    for duration in [1, 10, 7400]:
        for start, end in [(0, 0), (duration, duration), (duration-.01, duration), (-1, 1)]:
            top, height = _timeline_bar_geometry(start, end, duration)
            assert 0 <= top < 100
            assert 0 < height <= 100 - top


def test_mixed_timezone_and_reversed_child_spans_do_not_crash():
    source = _timeline_component({
        'start_ts': '2026-10-26T00:00:00',
        'end_ts': '2026-10-26T08:01:00+08:00',
        'children': [{'start_ts': '2026-10-26T00:00:20Z',
                      'end_ts': '2026-10-26T00:00:10Z'}],
        'events': [{'ts': '2026-10-26T08:00:30+08:00', 'text': 'hello'}],
    }, 'mixed')
    assert 'trace not captured' in source
    assert EventRows(source).rows[0]['data-time'] == '30.0'
