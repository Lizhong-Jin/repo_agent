"""Exercise the public session model on old and new installed packages, without HTTP."""

import argparse
import json
from pathlib import Path

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from cli.session_status import SessionStatus
from llm import LLMConfig, Message


class NoNetworkModel:
    config = LLMConfig("deepseek", "upgrade-model", api_key="not-sent")

    def generate(self, request):
        raise AssertionError("Upgrade acceptance must not call a model")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("seed", "check"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    model = NoNetworkModel()
    store = SessionStore(args.root).open()
    try:
        status = SessionStatus(args.root, context_window=32000)
        runtime = AgentRuntime(model, on_event=status)
        conversation = SavedConversation(store, runtime, model.config, status)
        if args.phase == "seed":
            conversation.history = (Message("user", "upgrade-memory"), Message("assistant", "kept"))
            has_queue = hasattr(conversation, "queue")
            if has_queue:
                conversation.queue.add("unfinished upgrade task")
            (args.root / "upgrade-capabilities.json").write_text(json.dumps({"queue": has_queue}))
            conversation.checkpoint(strict=True)
        else:
            assert any(m.content == "upgrade-memory" for m in conversation.history)
            capabilities = json.loads((args.root / "upgrade-capabilities.json").read_text())
            expected = ["unfinished upgrade task"] if capabilities["queue"] else []
            assert [t["text"] for t in conversation.queue.pending] == expected
            if expected:
                assert conversation.queue.paused, "Recovered tasks must not execute automatically"
            print(json.dumps({"previous_version_had_queue": capabilities["queue"]}))
        print("upgrade session " + args.phase + " passed")
    finally:
        store.close()


if __name__ == "__main__":
    main()
