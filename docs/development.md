**English** · [Русский](development.ru.md)

# Development

Install `requirements.txt`. Run `python tools/check.py` from the repository root to verify schedule
parity and run both test suites in separate Python processes. Separate processes matter because
both service directories use local module names such as `transfer_stats`.

| Module | Responsibility |
| --- | --- |
| `archiver/archive.py` | Discovery, queue, buffered segment downloads and durable commit |
| `archiver/chat_archive.py` | Replay cursor persistence and exports |
| `archiver/archive_insights.py` | Message-derived timelines and activity summaries |
| `archiver/transfer_stats.py` | Durable transfer telemetry and scheduled ETA |
| `dashboard/server.py` | Authenticated HTTP API, static UI and bounded media reads |
| `dashboard/agent.py` | Unix peer authentication and allowlisted control operations |

Use temporary roots in tests. Mock Twitch responses and subprocesses. Never run a benchmark or
download against a real archive as part of CI. The archived media format is transport stream (`.ts`);
FFmpeg/ffprobe are external tools and are not vendored here.
