<a id="readme-top"></a>

<!-- PROJECT LOGO -->
<br />
<div align="center">

<h3 align="center">opencode-resilience</h3>

  <p align="center">
    Failure detection, node/model failover, and offline policy validation for OpenCode sessions.
    <br />
    <a href="https://github.com/opencode-network/opencode-resilience"><strong>Explore the docs »</strong></a>
    <br />
  </p>

  <!-- PROJECT SHIELDS -->
[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
<!-- [![Latest Release][release-shield]][release-url]
![Release Date][release-date-shield] -->
[![License][license-shield]][license-url]

  <p align="center">
    <a href="https://github.com/opencode-network/opencode-resilience">View Demo</a>
    &middot;
    <a href="https://github.com/opencode-network/opencode-resilience/issues/new?labels=bug&template=bug-report---.md">Report Bug</a>
    &middot;
    <a href="https://github.com/opencode-network/opencode-resilience/issues/new?labels=enhancement&template=feature-request---.md">Request Feature</a>
  </p>
</div>

<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#key-features">Key Features</a></li>
        <li><a href="#built-with">Built With</a></li>
        <li><a href="#-project-structure">Project Structure</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
        <li><a href="#configuration">Configuration</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

<!-- ABOUT THE PROJECT -->
## 📖 About The Project

<!-- Optional: drop an architecture diagram at images/flow.png and reference it here. -->

**opencode-resilience** is a small resilience toolkit for OpenCode sessions. It watches assistant turns for provider and model failures, decides when a retry is worth it, and fails over between nodes and models — without turning every transient blip into a session-wide interruption.

The four files in this folder are **excerpts from the larger `tools/oc-error-monitor` suite**:

| Layer | Module | Role |
|---|---|---|
| P1 | `monitor.py` | Classifies raw errors into failure classes |
| P2 | `controller.py` | Node switching on bursty, node-owned failures |
| P3 | `session_failover.py` | Model switching on the first session-owned failure |
| P4 | `retry_proxy.py` | HTTP proxy exposing `/__stats`, `/__control`, `/__events` |

What ships here is the tooling around that core: an offline backtester that replays the live policy against historical sessions before you enable it, an OpenAI-compatible shim that routes requests through the OpenCode CLI, a linter for broken skill frontmatter, and the proxy dashboard.

> **Note:** `backtest.py` is **not standalone**. It imports `SWITCH_CLASSES`, `PERSISTENT_CLASSES`, and `PERSISTENT_MIN_FAILURES` from the P2/P3 modules so that replay can never drift from production behavior. Keep it next to `monitor.py`, `controller.py`, and `session_failover.py`.

### Key Features

- **OpenAI-compatible CLI shim** — `cli_llm_server.py` serves `/v1/chat/completions` and `/v1/models` in standard OpenAI wire format, but executes every request through `opencode run --model <model> --format json`. That makes free-tier, client-gated models reachable from third-party tools (README generators, doc pipelines, SDKs) without keys or account juggling.
- **Offline failover policy replay** — `backtest.py` reads `opencode.db` read-only, rebuilds each session's error/success timeline, and asks "would the policy have acted here?" For every intervention it records the real next outcome: false positive (the session recovered anyway), justified (it failed again), or stalled (no next attempt — the case intervention exists to prevent).
- **False-positive rate sweeping** — `--sweep` walks `min_failures = 1..5` to plot false-positive rate against stalled sessions, and compares "always act" versus "act only on sustained failure" per error class, so thresholds are chosen from data instead of intuition.
- **YAML frontmatter linting** — `check_skill_frontmatter.py` scans `~/.config/opencode/skills/**/SKILL.md` and flags single-quoted scalars with an unbalanced apostrophe — the usual cause of OpenChamber's "failed to parse frontmatter" noise in `main.log`.
- **Stdin-based prompt execution** — prompts are piped to the CLI over **stdin** rather than passed as arguments, sidestepping `WinError 206` on multi-kilobyte inputs.
- **Concurrency and timeout control** — a bounded queue caps parallel CLI runs (`CLI_LLM_CONCURRENCY`, default 3) and every call is fenced by `CLI_LLM_TIMEOUT` (default 300 s), mapped to 503 / 504 / 502 responses instead of hanging clients.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

* [![Python][Python]][Python-url] — standard library only (`http.server`, `sqlite3`, `argparse`, `queue`, `subprocess`)
* [![SQLite][SQLite]][SQLite-url] — reads `~/.local/share/opencode/opencode.db` in read-only mode
* [![OpenAI][OpenAI]][OpenAI-url] — wire-format compatibility for `/v1/chat/completions`
* [OpenCode CLI](https://github.com/sst/opencode) — the execution backend behind the shim
* Plain HTML + `EventSource` for the `retry_proxy.py` dashboard

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### 📁 Project Structure

<details>
<summary>Click to expand project structure</summary>

```
scratch/
├── backtest.py                  # Offline replay of the failover policy over historical sessions
├── check_skill_frontmatter.py   # Read-only SKILL.md YAML frontmatter linter
├── cli_llm_server.py            # OpenAI-compatible HTTP shim backed by the OpenCode CLI
└── ui.html                      # Dashboard for retry_proxy.py (P4) — must be served, not opened as a file
```

Expected siblings from the parent suite (required by `backtest.py`):

```
tools/oc-error-monitor/
├── monitor.py            # P1 — error classification
├── controller.py         # P2 — node switching policy
├── session_failover.py   # P3 — model switching policy
├── retry_proxy.py        # P4 — HTTP proxy that serves ui.html
└── scratch/              # ← this folder
```

</details>

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->
## 🚀 Getting Started

This is an example of how you may give instructions on setting up your project locally. To get a local copy up and running follow these simple steps.

### Prerequisites

- **Python 3.9+** on `PATH`. No third-party packages are required — every import is standard library or a local module.
- **OpenCode CLI**, for `cli_llm_server.py`. The shim looks for it at
  `%LOCALAPPDATA%\Programs\@openchamberelectron\resources\opencode-cli\opencode.exe`
  and falls back to `opencode` on `PATH`.
- **An OpenCode database** at `~/.local/share/opencode/opencode.db`, for `backtest.py`. It is opened with `mode=ro`, so the backtester never writes to your session history.
- **The P1–P3 modules** (`monitor.py`, `controller.py`, `session_failover.py`) on the same path, required by `backtest.py`.
- **`retry_proxy.py`**, if you want the dashboard in `ui.html` to display anything.

### Installation

```bash
git clone https://github.com/opencode-network/opencode-resilience.git
cd opencode-resilience
```

There is nothing to install — `requirements.txt` is intentionally empty:

```
# requirements.txt — empty: all imports are stdlib or local modules
# (__future__, argparse, queue, subprocess) + monitor, controller, session_failover
```

`monitor`, `controller`, and `session_failover` are **project files, not PyPI packages**, so they are deliberately not pinned. If `monitor` was meant to resolve to the PyPI package of that name, pin `monitor>=0.2` explicitly.

Verify the interpreter and the module layout:

```bash
python --version
python -c "import monitor, controller, session_failover; print('suite modules OK')"
```

### Configuration

`cli_llm_server.py` is configured entirely through the environment:

| Variable | Default | Meaning |
|---|---|---|
| `CLI_LLM_TIMEOUT` | `300` | Per-request CLI timeout in seconds; exceeded → HTTP `504` |
| `CLI_LLM_CONCURRENCY` | `3` | Max simultaneous CLI runs; a queued request waiting >30 s → HTTP `503` |

Both also drive the in-process counters (`requests`, `ok`, `failed`, `total_ms`), which are not exposed over HTTP.

Paths that are resolved at runtime rather than configured:

- **CLI binary** — `%LOCALAPPDATA%\Programs\@openchamberelectron\resources\opencode-cli\opencode.exe`, else `opencode` on `PATH`.
- **Database** — `~/.local/share/opencode/opencode.db`, read-only, for `backtest.py`.
- **Skills root** — `~/.config/opencode/skills`, hardcoded in `check_skill_frontmatter.py` as `ROOTS`.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE EXAMPLES -->
## 💻 Usage

### 1. Replay the failover policy before enabling it

```bash
# Default: read ~/.local/share/opencode/opencode.db, print overall + per-class stats
python backtest.py

# Point at a different history file
python backtest.py --db /path/to/opencode.db

# Sweep min_failures 1..5 and compare "always act" vs "act on sustained failure"
python backtest.py --sweep

# Export span, session count, policy, overall and per-class metrics for dashboards
python backtest.py --json report.json
```

Interpretation is the whole point:

| Next attempt after an error | Verdict |
|---|---|
| Succeeded | **False positive** — the policy would have intervened needlessly |
| Failed again | **Justified** — intervention was warranted |
| Absent | **Stalled session** — the failure mode the policy exists to prevent |

The policy itself is never duplicated — `SWITCH_CLASSES`, `PERSISTENT_CLASSES`, `P3_OWNED_CLASSES`, and `PERSISTENT_MIN_FAILURES` are imported from `controller` and `session_failover`, so replay tracks live behavior.

### 2. Run the OpenAI-compatible shim

```bash
python cli_llm_server.py --host 127.0.0.1 --port 8790
```

Health check and model list:

```bash
curl -sS http://127.0.0.1:8790/__health
curl -sS http://127.0.0.1:8790/v1/models
```

Chat completion:

```bash
curl -sS http://127.0.0.1:8790/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "opencode/grok-code-free",
    "messages": [{"role": "user", "content": "Summarize this repository."}]
  }'
```

Any OpenAI SDK works by pointing `base_url` at the shim:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="unused")
print(client.chat.completions.create(
    model="opencode/grok-code-free",
    messages=[{"role": "user", "content": "Write a README section."}],
).choices[0].message.content)
```

Streaming is accepted (`stream: true`) and answered with a single `chat.completion.chunk` followed by `data: [DONE]`. It is a compatibility shim, not real token-by-token streaming — the full CLI run completes before the first byte is sent, so it will not rescue a client-side timeout.

### 3. Lint skill frontmatter

```bash
python check_skill_frontmatter.py
```

Output is a per-file report plus a total:

```
[broken] C:\Users\me\.config\opencode\skills\foo\SKILL.md
    description: unbalanced quote at 12
1 file(s) with unbalanced single-quoted scalars
```

The rule it enforces: a valid single-quoted YAML scalar escapes internal quotes by doubling (`''`), so **parity**, not presence, decides validity. Known blind spots — lines without a trailing quote (`description: 'ticket's flow`), multi-line `>`/`|` blocks, and non-`\w+` keys — are skipped rather than reported, and the exit code is always `0`, so it will not fail CI.

### 4. Serve the dashboard

`ui.html` is the dashboard for **`retry_proxy.py` (P4)**, not for the CLI shim. It polls `/__stats`, POSTs to `/__control`, and streams `EventSource('/__events')`. Start the proxy and open the page it serves:

```bash
python retry_proxy.py
```

Opening `ui.html` as `file://` yields a dead dashboard — the polling and event stream have no origin to talk to.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ROADMAP -->
## 🗺️ Roadmap

- [x] P1 error classification, P2 node switching, P3 session failover, P4 retry proxy
- [x] Offline policy replay with false-positive / stalled-session accounting
- [x] Threshold sweeping per error class
- [x] OpenAI-compatible shim over the OpenCode CLI
- [x] Skill frontmatter linter
- [ ] Promote `backtest.py` to a package level so it runs without the parent suite on `sys.path`
- [ ] Make the frontmatter linter catch unterminated scalars (match `^(\w+):\s*'` instead of requiring a trailing quote)
- [ ] Return a non-zero exit code from the linter when hits are found, for CI use
- [ ] Support real streaming in the shim instead of a single buffered chunk
- [ ] Replace the hardcoded skills root with a CLI argument
- [ ] Expose `_stats` metrics over HTTP for the dashboard

See the [open issues](https://github.com/opencode-network/opencode-resilience/issues) for a full list of proposed features (and known issues).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTRIBUTING -->
## 🤝 Contributing

Contributions are what make the open-source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".
Don't forget to give the project a star! Thanks again!

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Top contributors:

<a href="https://github.com/opencode-network/opencode-resilience/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=opencode-network/opencode-resilience" alt="contrib.rocks image" />
</a>

<!-- LICENSE -->
## 🎗 License

Copyright © 2024-2025 [opencode-resilience][opencode-resilience]. <br />
Released under the terms described in [LICENSE.txt][license-url].

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTACT -->
## 📧 Contact

Issues: [https://github.com/opencode-network/opencode-resilience/issues](https://github.com/opencode-network/opencode-resilience/issues)

Project Link: [https://github.com/opencode-network/opencode-resilience](https://github.com/opencode-network/opencode-resilience)

For questions, bug reports, or feature requests, open an issue — that is the fastest route to a response.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ACKNOWLEDGMENTS -->
## 🙏 Acknowledgments

- [OpenCode](https://github.com/sst/opencode) for the CLI this shim drives
- [OpenChamber](https://github.com/openchamber) for the desktop environment whose logs surfaced the frontmatter failures
- [Best-README-Template](https://github.com/othneildrew/Best-README-Template) for the structure of this document

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- REFERENCE LINKS -->
[opencode-resilience]: https://github.com/opencode-network/opencode-resilience

<!-- MARKDOWN LINKS & IMAGES -->
[contributors-shield]: https://img.shields.io/github/contributors/opencode-network/opencode-resilience.svg?style=flat-round
[contributors-url]: https://github.com/opencode-network/opencode-resilience/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/opencode-network/opencode-resilience.svg?style=flat-round
[forks-url]: https://github.com/opencode-network/opencode-resilience/network/members
[stars-shield]: https://img.shields.io/github/stars/opencode-network/opencode-resilience.svg?style=flat-round
[stars-url]: https://github.com/opencode-network/opencode-resilience/stargazers
[issues-shield]: https://img.shields.io/github/issues/opencode-network/opencode-resilience.svg?style=flat-round
[issues-url]: https://github.com/opencode-network/opencode-resilience/issues
[release-shield]: https://img.shields.io/github/v/release/opencode-network/opencode-resilience?style=flat-round
[release-url]: https://github.com/opencode-network/opencode-resilience/releases
[release-date-shield]: https://img.shields.io/github/release-date/opencode-network/opencode-resilience?color=9cf&style=flat-round
[license-shield]: https://img.shields.io/github/license/opencode-network/opencode-resilience.svg?style=flat-round
[license-url]: https://github.com/opencode-network/opencode-resilience/blob/master/LICENSE.txt

<!-- Tech Stack -->
<!-- 编程语言 -->
[Python]: https://img.shields.io/badge/Python-3776AB?style=flat-round&logo=python&logoColor=white
[Python-url]: https://www.python.org/
[JavaScript]: https://img.shields.io/badge/JavaScript-F7DF1E?style=flat-round&logo=javascript&logoColor=black
[JavaScript-url]: https://developer.mozilla.org/en-US/docs/Web/JavaScript
[TypeScript]: https://img.shields.io/badge/TypeScript-007ACC?style=flat-round&logo=typescript&logoColor=white
[TypeScript-url]: https://www.typescriptlang.org/
[Java]: https://img.shields.io/badge/Java-ED8B00?style=flat-round&logo=openjdk&logoColor=white
[Java-url]: https://www.oracle.com/java/
[Go]: https://img.shields.io/badge/Go-00ADD8?style=flat-round&logo=go&logoColor=white
[Go-url]: https://golang.org/
[Rust]: https://img.shields.io/badge/Rust-000000?style=flat-round&logo=rust&logoColor=white
[Rust-url]: https://www.rust-lang.org/
[C]: https://img.shields.io/badge/C-00599C?style=flat-round&logo=c&logoColor=white
[C-url]: https://en.wikipedia.org/wiki/C_(programming_language)
[CPP]: https://img.shields.io/badge/C++-00599C?style=flat-round&logo=cplusplus&logoColor=white
[CPP-url]: https://en.wikipedia.org/wiki/C%2B%2B
[CSharp]: https://img.shields.io/badge/C%23-239120?style=flat-round&logo=csharp&logoColor=white
[CSharp-url]: https://docs.microsoft.com/en-us/dotnet/csharp/
[MATLAB]: https://img.shields.io/badge/MATLAB-0076A8?style=flat-round&logo=mathworks&logoColor=white
[MATLAB-url]: https://www.mathworks.com/products/matlab.html

<!-- 前端框架 -->
[React.js]: https://img.shields.io/badge/React-20232A?style=flat-round&logo=react&logoColor=61DAFB
[React-url]: https://reactjs.org/
[Vue.js]: https://img.shields.io/badge/Vue.js-35495E?style=flat-round&logo=vuedotjs&logoColor=4FC08D
[Vue-url]: https://vuejs.org/
[Angular.io]: https://img.shields.io/badge/Angular-DD0031?style=flat-round&logo=angular&logoColor=white
[Angular-url]: https://angular.io/
[Next.js]: https://img.shields.io/badge/next.js-000000?style=flat-round&logo=nextdotjs&logoColor=white
[Next-url]: https://nextjs.org/

<!-- 后端框架 -->
[Flask]: https://img.shields.io/badge/Flask-000000?style=flat-round&logo=flask&logoColor=white
[Flask-url]: https://flask.palletsprojects.com/
[Django]: https://img.shields.io/badge/Django-092E20?style=flat-round&logo=django&logoColor=white
[Django-url]: https://www.djangoproject.com/
[FastAPI]: https://img.shields.io/badge/FastAPI-005571?style=flat-round&logo=fastapi&logoColor=white
[FastAPI-url]: https://fastapi.tiangolo.com/
[Express.js]: https://img.shields.io/badge/Express.js-404D59?style=flat-round&logo=express&logoColor=white
[Express-url]: https://expressjs.com/
[Spring]: https://img.shields.io/badge/Spring-6DB33F?style=flat-round&logo=spring&logoColor=white
[Spring-url]: https://spring.io/
[Node.js]: https://img.shields.io/badge/Node.js-43853D?style=flat-round&logo=node.js&logoColor=white
[Node-url]: https://nodejs.org/

<!-- AI/ML 相关 -->
[OpenAI]: https://img.shields.io/badge/OpenAI-000000?style=flat-round&logo=openai&logoColor=white
[OpenAI-url]: https://openai.com/
[Rich]: https://img.shields.io/badge/Rich-000000?style=flat-round&logo=rich&logoColor=white
[Rich-url]: https://rich.readthedocs.io/

<!-- 数据库 -->
[PostgreSQL]: https://img.shields.io/badge/PostgreSQL-316192?style=flat-round&logo=postgresql&logoColor=white
[PostgreSQL-url]: https://www.postgresql.org/
[MySQL]: https://img.shields.io/badge/MySQL-00000F?style=flat-round&logo=mysql&logoColor=white
[MySQL-url]: https://www.mysql.com/
[MongoDB]: https://img.shields.io/badge/MongoDB-4EA94B?style=flat-round&logo=mongodb&logoColor=white
[MongoDB-url]: https://www.mongodb.com/
[Redis]: https://img.shields.io/badge/Redis-DC382D?style=flat-round&logo=redis&logoColor=white
[Redis-url]: https://redis.io/
[SQLite]: https://img.shields.io/badge/SQLite-07405E?style=flat-round&logo=sqlite&logoColor=white
[SQLite-url]: https://www.sqlite.org/