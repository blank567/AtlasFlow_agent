from atlasflow.observability import _sanitize_trace_inputs, _sanitize_trace_outputs


def test_trace_content_is_redacted_by_default(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_CONTENT", "false")

    inputs = _sanitize_trace_inputs(
        {"self": object(), "query": "confidential", "api_key": "secret"}
    )
    outputs = _sanitize_trace_outputs("confidential result")

    assert inputs == {"content_redacted": True, "input_fields": ["query"]}
    assert outputs == {"content_redacted": True, "output_type": "str"}
    assert "confidential" not in repr((inputs, outputs))


def test_trace_content_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACE_CONTENT", "true")

    assert _sanitize_trace_inputs({"query": "public demo"}) == {"query": "public demo"}
    assert _sanitize_trace_outputs(["public output"]) == {"output": ["public output"]}
