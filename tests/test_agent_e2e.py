"""Exercise the real CLI, runtime and default tools with simulated HTTP responses."""

import json

import httpx

import cli.main as cli
from llm import LLMClient


def test_interactive_file_tasks_with_default_tools(tmp_path, monkeypatch, capsys):
    source = "def add(a, b):\n    return a - b\n"
    corrected = source.replace("a - b", "a + b")
    # Script only the remote model. All local file operations and message conversions are real.
    rounds = iter(
        [
            [("load_tool_group", {"group": "file_editing"})],
            [("make_directory", {"path": "demo"})],
            [
                ("write_file", {"path": "demo/math.py", "content": source}),
                ("write_file", {"path": "demo/scratch.txt", "content": "temporary"}),
            ],
            [("read_file", {"reads": [{"path": "demo/math.py"}]})],
            [("edit_file", {"path": "demo/math.py", "edits": [{"old_text": "a - b", "new_text": "a + b"}]})],
            [
                ("read_file", {"reads": [{"path": "demo/math.py"}]}),
                ("search_files", {"path": "demo", "query": "return a + b"}),
                ("list_files", {"path": "demo"}),
            ],
            "已修复加法函数。",
            [("delete_file", {"path": "demo/scratch.txt"})],
            "已删除临时文件，保留修复后的代码。",
        ]
    )
    requests = []
    observations = {}

    def respond(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"}
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 8192
        assert body["tool_choice"] == "auto"
        requests.append(body)
        for item in body["messages"]:
            if item["role"] == "tool":
                result = json.loads(item["content"])
                assert result["success"], result
                observations[item["tool_call_id"]] = result["data"]
        reply = next(rounds)
        message = {"role": "assistant", "content": reply if isinstance(reply, str) else ""}
        if isinstance(reply, list):
            message["tool_calls"] = [
                {
                    "id": f"step{len(requests)}_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for index, (name, args) in enumerate(reply)
            ]
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if isinstance(reply, list) else "stop",
                    }
                ],
            },
        )

    commands = iter(
        ["创建一个加法函数并将减号修复为加号，核对文件。", "删除刚才的临时文件。", "/exit"]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent", "--sandbox", "local",
            "--root",
            str(tmp_path),
            "--provider",
            "deepseek",
            "--model",
            "mock",
            "--base-url",
            "https://mock.invalid/v1",
        ],
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-key")
    monkeypatch.setenv("LLM_THINKING", "enabled")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
    monkeypatch.setenv("AGENT_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("LLM_TIMEOUT", "30")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENT_SYSTEM_PROMPT", "You are a test coding assistant.")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:

        def make_client(config):
            assert config.timeout == 30
            assert config.max_retries == 0
            return LLMClient(config, http_client=http)

        monkeypatch.setattr("cli.runtime_setup.LLMClient", make_client)
        cli.main()

    output = capsys.readouterr().out
    trace_text = next((tmp_path / "logs").glob("*.trace.log")).read_text()
    events = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("*.trace.jsonl")).read_text().splitlines()
    ]
    assert len([e for e in events if e["event"] == "task_end" and e["status"] == "completed"]) == 2
    assert "状态：" not in output
    assert "模型调用" not in output
    assert "工具 " not in output
    assert "总耗时=" not in output
    assert "tokens：" not in output
    assert "已修复加法函数。" in output
    assert "已删除临时文件，保留修复后的代码。" in output
    assert "模型调用=7 次，工具调用=9 次" in trace_text
    assert "模型调用=2 次，工具调用=1 次" in trace_text
    assert "输入=700，输出=140，合计=840" in trace_text.replace(",", "，").replace(" ", "")
    assert "输入=200，输出=40，合计=240" in trace_text.replace(",", "，").replace(" ", "")
    assert "工具 edit_file" in trace_text
    assert "总耗时=" in trace_text
    assert len(requests) == 9
    initial_tools = {item["function"]["name"] for item in requests[0]["tools"]}
    loaded_tools = {item["function"]["name"] for item in requests[1]["tools"]}
    assert "load_tool_group" in initial_tools and "write_file" not in initial_tools
    assert "write_file" in loaded_tools and "git_diff" not in loaded_tools
    assert requests[0]["messages"][0]["content"] == "You are a test coding assistant."
    assert output.count("┏") == 3
    assert output.count("┗") == 3
    assert output.index("┗") < output.index("已修复加法函数。")
    assert len([m for m in requests[-1]["messages"] if m["role"] == "user"]) == 2
    assert (tmp_path / "demo/math.py").read_text() == corrected
    assert not (tmp_path / "demo/scratch.txt").exists()
    assert "return a + b" in observations["step6_0"]["results"][0]["data"]["content"]
    assert observations["step6_1"]["matches"][0]["path"] == "demo/math.py"
    assert {entry["name"] for entry in observations["step6_2"]["entries"]} == {
        "math.py",
        "scratch.txt",
    }
    # Independently verify the generated code's behavior, outside the Agent.
    namespace = {}
    exec(compile((tmp_path / "demo/math.py").read_text(), "math.py", "exec"), namespace)
    assert namespace["add"](2, 3) == 5
