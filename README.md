<div id="toc" align="center">
  <ul style="list-style: none">
    <summary>
      <h1><img src="images/pls.gif" alt="Scrapybara" width="24"> Scrapybara Playground <img src="images/pls.gif" alt="Scrapybara" width="24"></h1>
    </summary>
  </ul>
</div>

<p align="center">
  Computer use playground hosted on Scrapybara instances
</p>

<p align="center">
  <a href="https://scrapybara.com/playground"><img alt="Static Badge" src="https://img.shields.io/badge/Check%20it%20out-6D1CCF"></a>
  <a href="https://github.com/scrapybara/scrapybara-playground/blob/main/license"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-blue" /></a>
  <a href="https://discord.gg/s4bPUVFXqA"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20the%20community-yellow.svg?logo=discord" /></a>
</p>

## Intro

This is the FastAPI backend powering the official [Scrapybara playground](https://scrapybara.com/playground). It provides a WebSocket interface for users to interact with a Claude Computer Use agent running on a virtual Scrapybara instance.

### How it works

- FastAPI with WebSocket endpoint at `/ws/chat` for message streaming
- Each chat session managed by a `ChatSession` class that handles:
  - Scrapybara instance lifecycle
  - Message history and context management
  - Tool execution (Computer, Bash, and Edit tools)
- Agent calls with Scrapybara Act SDK

## Local Development

### Prerequisites

- Python 3.11 or higher
- Poetry

### Installation

1. Clone the repository

```bash
git clone https://github.com/scrapybara/scrapybara-playground.git
cd scrapybara-playground
```

2. Install dependencies using Poetry

```bash
poetry install
```

### Running the server

Start the development server:

```bash
poetry run uvicorn src.main:app --reload
```

The API will be available at `http://localhost:8000`. To connect to the websocket, use the `ws://localhost:8000/ws/chat` endpoint.

## Contributing

Please join our [Discord](https://discord.gg/s4bPUVFXqA) to discuss your ideas before submitting a contribution:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add and verify tests
5. Commit your changes
6. Push to your fork
7. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
