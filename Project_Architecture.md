repo-agent/
│
├── agent/
│   ├── runtime.py
│   ├── state.py
│   ├── context.py
│   ├── planner.py
│   └── protocol.py
│
├── llm/
│   ├── base.py
│   ├── openai.py
│   └── local.py
│
├── tools/
│   ├── base.py
│   ├── registry.py
│   ├── filesystem.py
│   ├── code_search.py
│   ├── shell.py
│   ├── git.py
│   └── testing.py
│
├── sandbox/
│   ├── docker.py
│   └── policy.py
│
├── retrieval/
│   ├── chunker.py
│   ├── bm25.py
│   └── embedding.py
│
├── mcp/
│   └── client.py
│
├── tracing/
│   ├── tracer.py
│   └── metrics.py
│
├── eval/
│   ├── tasks/
│   ├── runner.py
│   └── metrics.py
│
├── cli/
│   └── main.py
│
└── tests/
