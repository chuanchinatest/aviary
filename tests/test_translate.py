"""协议翻译单测:请求与响应,两个方向。"""

import json

from aviary.events import URequest
from aviary.translate import (
    anthropic_request_to_unified, anthropic_response_to_unified,
    openai_request_to_unified, openai_response_to_unified,
    unified_to_anthropic_request, unified_to_anthropic_response,
    unified_to_openai_request, unified_to_openai_response,
)


def test_openai_request_with_tools_and_system():
    body = {
        "model": "x", "stream": True, "max_tokens": 100,
        "messages": [
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": "天气如何"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
    }
    u = openai_request_to_unified(body, "mock-chat")
    assert u.system == "你是一个助手"
    assert u.stream and u.max_tokens == 100
    assert u.tools[0]["name"] == "get_weather" and u.tools[0]["parameters"]["type"] == "object"
    a = unified_to_anthropic_request(u)
    assert a["system"] == "你是一个助手"
    assert a["tools"][0]["input_schema"]["type"] == "object"
    assert a["messages"] == [{"role": "user", "content": "天气如何"}]


def test_anthropic_request_tool_roundtrip():
    body = {
        "model": "claude-mock", "max_tokens": 256, "system": [{"type": "text", "text": "S"}],
        "messages": [
            {"role": "user", "content": "天气如何"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                               "name": "get_weather",
                                               "input": {"city": "杭州"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "晴 28 度"}]},
        ],
        "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
    }
    u = anthropic_request_to_unified(body, "claude-mock")
    assert u.system == "S"
    assert u.messages[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert u.messages[2]["role"] == "tool"
    assert u.messages[2]["tool_call_id"] == "t1"
    o = unified_to_openai_request(u)
    assert o["messages"][0] == {"role": "system", "content": "S"}
    assert o["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "晴 28 度"}
    assert o["tools"][0]["function"]["name"] == "get_weather"
    a = unified_to_anthropic_request(u)
    assert a["messages"][1]["content"][0]["input"] == {"city": "杭州"}
    assert a["messages"][2]["content"][0]["type"] == "tool_result"


def test_openai_response_to_anthropic_response():
    data = {
        "choices": [{"message": {"role": "assistant", "content": "你好",
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "f",
                                                              "arguments": "{\"a\":1}"}}]},
                     "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    _, meta = openai_response_to_unified(data)
    a = unified_to_anthropic_response(meta, "claude-mock")
    assert a["stop_reason"] == "tool_use"
    assert a["content"][0] == {"type": "text", "text": "你好"}
    assert a["content"][1]["input"] == {"a": 1}
    assert a["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_anthropic_response_to_openai_response():
    data = {
        "content": [{"type": "text", "text": "你好"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    _, meta = anthropic_response_to_unified(data)
    o = unified_to_openai_response(meta, "mock-chat")
    assert o["choices"][0]["message"]["content"] == "你好"
    assert o["choices"][0]["finish_reason"] == "stop"
    assert o["usage"]["prompt_tokens"] == 7


def test_length_stop_reason_maps():
    _, meta = openai_response_to_unified(
        {"choices": [{"message": {"content": "截断"}, "finish_reason": "length"}],
         "usage": {}})
    assert unified_to_anthropic_response(meta, "m")["stop_reason"] == "max_tokens"
    assert meta["finish_reason"] == "length"
