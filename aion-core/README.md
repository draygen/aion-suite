# Aion: Your Personal AI Assistant

Monorepo note:

- In `drayhub-platform`, `services/aion` is the canonical home for the Python Aion app.
- The older misplaced Node chat copy that previously lived at this path was preserved at
  `services/_legacy/aion-node-chat-precutover`.
- Vast.ai deployments now bootstrap from `drayhub-platform` and run Aion from
  `services/aion` on the remote instance.

Aion is a personal AI assistant designed to be general-purpose and adaptable with your personal information. It leverages large language models (LLMs) for conversational capabilities and a fact retrieval system to provide context-aware responses.

## Features

*   **Conversational AI**: Interact with Aion through a command-line interface.
*   **LLM Integration**: Supports both local Ollama models (e.g., `brian-mistral`) and OpenAI's API (GPT-4o-mini).
*   **Fact Management**: Ingests and retrieves facts from various data sources (JSONL files) to provide personalized and informed answers.
*   **Command Handling**: Built-in commands for help, setting configurations, recalling facts, reloading data, and teaching new information.
*   **Retrieval Augmented Generation (RAG)**: Uses TF-IDF for semantic search over stored facts to enhance LLM responses with relevant context.

## Installation

### Prerequisites

*   Python 3.11 or higher.
*   **Ollama (Recommended for local LLM)**: Download and install Ollama from [ollama.com](https://ollama.com/). After installation, pull a model, e.g., `ollama pull brian-mistral`.
*   **OpenAI API Key (Alternative)**: If you prefer using OpenAI, you'll need an API key. Set `openai_api_key` in `config.py`.

### Steps

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/draygen/drayhub-platform.git
    cd drayhub-platform/services/aion
    ```

2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure Aion**:
    Open `config.py` and adjust settings as needed. Key configurations include:
    *   `model`: The LLM model to use (e.g., `brian-mistral`, `gpt-4o-mini`).
    *   `backend`: `ollama` or `openai`.
    *   `facts_files`: Paths to your JSONL files containing facts. You can add your own personal data here.
    *   `openai_api_key`: Your OpenAI API key if using the OpenAI backend.

    Example `config.py`:
    ```python
    CONFIG = {
        "model": "brian-mistral",
        "backend": "ollama",
        "retrieval": "embed",
        "embed_backend": "tfidf",
        "facts_files": [
            "data/profile.jsonl",
            "data/brian_facts.jsonl",
            "data/fb_qa_pairs.jsonl",
            # Add your custom fact files here
        ],
        "openai_api_key": "sk-YOUR_OPENAI_API_KEY" # Change this if using OpenAI
    }
    ```

## Usage

To start Aion, run:

```bash
python app.py
```

### Commands

Aion supports several commands:

*   `/help`: Show available commands.
*   `/recall`: Display a few loaded facts (for debugging).
*   `/reload`: Reload facts from configured data files.
*   `/why`: Show the retrieved snippets used for the last answer.
*   `/note TEXT`: Save a standalone fact/note to memory immediately.
*   `/teach Q => A`: Teach a question-answer pair to memory immediately.
*   `/set k=v`: Set a runtime option (e.g., `/set model=gpt-4o-mini`).
*   `exit` or `quit`: Exit the application.

### Google Calendar Appointments

AION can create appointments on Brian's primary Google Calendar, which then syncs
to devices signed into `draygen80@gmail.com`.

1.  Install dependencies from `requirements.txt`.
2.  Create a Google Cloud OAuth desktop client with Calendar API access.
3.  Save the downloaded client JSON as `data/google_calendar_credentials.json`.
4.  Run:
    ```bash
    python google_calendar.py auth
    ```
5.  Sign in as `draygen80@gmail.com`. AION saves the reusable token at
    `data/google_calendar_token.json`.

Example chat commands:

```text
calendar dentist tomorrow at 2pm notes: bring insurance card
schedule appointment doctor 2026-07-20 at 9am reminder 30 minutes before
```

## Project Structure

*   `app.py`: Main application logic, handles user input and orchestrates interactions.
*   `config.py`: Configuration settings for the application.
*   `llm.py`: Handles interactions with different LLM backends (Ollama, OpenAI).
*   `brain.py`: Manages fact storage, retrieval (TF-IDF based), and memory.
*   `commands.py`: Defines and parses user commands.
*   `data/`: Directory for fact files (e.g., `profile.jsonl`, `brian_facts.jsonl`, `fb_qa_pairs.jsonl`).
*   `requirements.txt`: Python dependencies.

## Future Enhancements

*   **Improved Fact Management**: More sophisticated fact storage and retrieval mechanisms.
*   **Advanced LLM Integration**: Support for more LLM providers and fine-tuning options.
*   **Modular Architecture**: Further modularization for easier extension and maintenance.
*   **Unit Tests**: Comprehensive test suite for all modules.
*   **Logging**: Implement a robust logging system for better debugging and monitoring.
*   **Containerization**: Provide Docker support for easy deployment.

## Contributing

Contributions are welcome! Please feel free to open issues or submit pull requests.

