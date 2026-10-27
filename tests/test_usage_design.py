"""Permission presentation and linked selection contracts; not visual snapshots."""

import json
import shutil
import subprocess

import pytest

from _lib.usage_html import _SCRIPT, _context_component, _filesystem_diagram
from _lib.usage import _rollout_inspection


def test_filesystem_tree_preserves_grants_and_read_only_overrides():
    source = _filesystem_diagram({"file_system": {"entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": "/project/workspace"}, "access": "write"},
        {"path": {"type": "path", "path": "/project/workspace/.git"}, "access": "read"},
        {"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"},
    ]}})
    assert "data-path='/'" in source and "/ (filesystem)" in source
    assert "data-path='/project/workspace'" in source and "READ · WRITE" in source
    assert "style='--depth:2' data-path='/project/workspace/.git'" in source
    assert "<span class='z-folder ro'" in source and ">.git</span>" in source
    assert "$TMPDIR" in source


@pytest.mark.parametrize("profile", [None, {}, {"file_system": {"entries": None}}])
def test_missing_permissions_do_not_invent_a_filesystem_tree(profile):
    source = _filesystem_diagram(profile)
    assert "were not recorded" in source
    assert "z-folder" not in source and "READ · WRITE" not in source


def test_tree_does_not_drop_grants_after_twelfth_entry_and_escapes_paths():
    entries = [{"path": f"/workspace/path-{i}", "access": "read"} for i in range(20)]
    entries.append({"path": '/workspace/<script>"unsafe"', "access": "deny"})
    source = _filesystem_diagram({"entries": entries})
    assert source.count("data-path=") == 21
    assert "<script>" not in source and "&lt;script&gt;" in source
    assert "NO ACCESS" in source


def test_context_keeps_unknown_network_unknown_and_missing_files_distinct():
    source = _context_component({"files_read": [{"name": "missing.py", "path": "/missing.py", "status": "missing"}]}, "example")
    assert "Network not recorded" in source and "Network OFF" not in source
    assert "z-doc-x" in source and "not found" in source


def test_context_source_swatches_do_not_share_document_card_classes():
    source = _context_component({"composition": [
        {"segment": "base", "token_estimate": 100},
        {"segment": "file", "name": "", "path": "/src/example.py", "token_estimate": 20},
        {"segment": "file", "token_estimate": 10},
    ]}, "sources")
    assert "z-swatch src-file" in source and "z-swatch z-file" not in source
    assert "Base instructions" in source and "example.py" in source
    assert "File read · name not recorded" in source
    assert "Recorded context sources" in source and "not a snapshot of one model call" in source


@pytest.mark.parametrize("last_input", [0, 20])
def test_window_tank_uses_last_input_including_cache_not_cumulative_usage(last_input):
    inspection = _rollout_inspection([{"timestamp": "2026-10-26T00:00:01Z", "type": "event_msg", "payload": {
        "type": "token_count", "info": {"model_context_window": 100,
        "total_token_usage": {"input_tokens": 5000},
        "last_token_usage": {"input_tokens": last_input, "cached_input_tokens": last_input}},
    }}])
    assert inspection["latest_input_tokens"] == last_input
    source = _context_component(inspection, "latest")
    assert f"{last_input:.1f}% of window" in source
    assert f"height:{last_input:.3f}%" in source


@pytest.mark.parametrize("last_input", [None, True, -1, "20"])
def test_window_without_valid_input_snapshot_stays_unknown(last_input):
    inspection = _rollout_inspection([{"type": "event_msg", "payload": {"type": "token_count", "info": {
        "model_context_window": 100, "last_token_usage": {"input_tokens": last_input},
        "total_token_usage": {"input_tokens": 5000},
    }}}])
    source = _context_component(inspection, "missing-snapshot")
    assert inspection["latest_input_tokens"] is None
    assert "Input snapshot unavailable" in source
    assert "% of window" not in source


def test_file_sources_exclude_failed_reads_and_directory_listings():
    events = []
    for op, name, output in [("read", "missing.py", "No such file"), ("list_files", "", "file listing"), ("read", "good.py", "source content")]:
        events.append({"type": "item.completed", "item": {"type": "command_execution", "command": "inspect", "aggregated_output": output,
            "parsed_cmd": [{"type": op, "name": name, "path": name}]}})
    inspection = _rollout_inspection(events)
    files = [piece for piece in inspection["composition"] if piece["segment"] == "file"]
    assert [piece["name"] for piece in files] == ["good.py"]
    assert files[0]["path"] == "good.py"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for report interaction checks")
def test_actor_selection_is_exact_and_cursor_clears_when_switching():
    runtime = r"""
const assert = require('assert'), vm = require('vm'), fs = require('fs');
const context = {};
vm.runInNewContext(JSON.parse(fs.readFileSync(0, 'utf8')), context);
function classes() {const s=new Set();return {add:x=>s.add(x),remove:x=>s.delete(x),contains:x=>s.has(x),toggle:(x,on)=>on?s.add(x):s.delete(x)}}
const cursor={hidden:true,style:{},label:{textContent:''},querySelector(){return this.label}};
const hint={textContent:''};
const panels=['actor-1','actor-10'].map(id=>({id,classList:classes(),closest:()=>stage}));
const bars=panels.map(p=>({dataset:{actor:p.id},classList:classes(),setAttribute(k,v){this[k]=v}}));
const row={dataset:{position:'50',time:'5.0'},classList:classes(),closest:()=>stage};
const stage={dataset:{},classList:classes(),parentElement:{querySelector:()=>hint},querySelector:()=>cursor,
querySelectorAll(s){return s==='.u-trace-agent'?panels:s==='.u-block'?bars:s==='.u-event.selected'?[row].filter(x=>x.classList.contains('selected')):[row]}};
context.document={getElementById:id=>panels.find(p=>p.id===id)};
context.openTimelineActor('actor-1');
assert(bars[0].classList.contains('active')); assert(!bars[1].classList.contains('active'));
assert.equal(bars[0]['aria-pressed'],'true');
context.selectTimelineEvent(row); assert(!cursor.hidden); assert.equal(cursor.style.top,'50%');
context.openTimelineActor('actor-10'); assert(cursor.hidden); assert(!row.classList.contains('selected'));
assert(!bars[0].classList.contains('active')); assert(bars[1].classList.contains('active'));
context.openTimelineActor('actor-10'); assert(!stage.classList.contains('open'));
assert(bars.every(b=>b['aria-pressed']==='false'));
const contextViews=['context-1','context-10'].map(id=>({id,classList:classes(),closest:s=>s==='.z-inventory'?inventory:null}));
const tabs=contextViews.map(p=>({dataset:{context:p.id},classList:classes(),setAttribute(k,v){this[k]=v}}));
const inventory={querySelectorAll:s=>s==='.z-agent'?contextViews:tabs};
context.document.getElementById=id=>contextViews.find(p=>p.id===id);
context.selectContextAgent('example','context-1');
assert(!contextViews[0].classList.contains('hidden')); assert(contextViews[1].classList.contains('hidden'));
assert(tabs[0].classList.contains('active')); assert(!tabs[1].classList.contains('active'));
context.selectContextAgent('example','context-10');
assert(contextViews[0].classList.contains('hidden')); assert(!contextViews[1].classList.contains('hidden'));
assert(!tabs[0].classList.contains('active')); assert(tabs[1].classList.contains('active'));
"""
    result = subprocess.run([shutil.which("node"), "-e", runtime], input=json.dumps(_SCRIPT), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
