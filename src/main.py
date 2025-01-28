import os
import asyncio
import uvicorn
from typing import Any, cast, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from anthropic import Anthropic
from anthropic.types.beta import BetaMessageParam
from scrapybara import Scrapybara
from scrapybara.anthropic import BashTool, ComputerTool, EditTool, ToolCollection
from scrapybara.client import UbuntuInstance
from dotenv import load_dotenv

from src.db import Database
from src.utils import (
    maybe_filter_to_n_most_recent_images,
    make_tool_result,
    response_to_params,
)
from src.prompt import SYSTEM_PROMPT

# Load environment variables
load_dotenv(override=True)

db = Database()

app = FastAPI()

# Configure CORS for cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatSession:
    """Manages a single chat session including instance lifecycle and message history."""

    def __init__(self, api_key: str, auth_state_id: Optional[str] = None):
        self.messages: list[BetaMessageParam] = []
        self.instance: Optional[UbuntuInstance] = None
        self.tool_collection: Optional[ToolCollection] = None
        self.stream_url: Optional[str] = None
        self.api_key = api_key
        self.auth_state_id = auth_state_id
        self.scrapybara = Scrapybara(api_key=api_key)

    async def initialize_instance(self) -> tuple[bool, Optional[str]]:
        """Initialize a new Scrapybara instance with necessary tools.

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        if not self.instance:
            try:
                instance = self.scrapybara.start_ubuntu()
                self.instance = instance
                self.stream_url = instance.get_stream_url().stream_url

                if self.auth_state_id:
                    instance.browser.start()
                    instance.browser.authenticate(auth_state_id=self.auth_state_id)

                self.tool_collection = ToolCollection(
                    ComputerTool(instance),
                    BashTool(instance),
                    EditTool(instance),
                )
                return True, None
            except Exception as e:
                return False, str(e)
        return True, None

    async def terminate_instance(self):
        """Safely terminate the Scrapybara instance."""
        if self.instance:
            self.instance.stop()
            self.instance = None
            self.tool_collection = None


async def check_pause_message(websocket: WebSocket) -> bool:
    """Check for pause command from client with timeout.

    Returns:
        bool: True if pause command received, False otherwise
    """
    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
        return isinstance(data, dict) and data.get("command") == "pause"
    except asyncio.TimeoutError:
        return False
    except WebSocketDisconnect:
        raise


async def process_chat_message(
    websocket: WebSocket, message: str, chat_session: ChatSession
):
    """Process a single chat message within a session.

    Handles:
    - Usage tracking and quota management
    - Message processing with Claude
    - Tool execution and result handling
    - Real-time response streaming
    """
    # Verify user credentials and check agent credits
    user_id = db.get_user_id(chat_session.api_key)
    agent_credits = db.get_credits(user_id)

    chat_session.messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": message}],
        }
    )

    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    while True:
        try:
            # Check if user has available credits
            if agent_credits <= 0:
                await websocket.send_json(
                    {
                        "type": "out_of_credits",
                        "content": "You have run out of agent credits. Please purchase more to continue.",
                    }
                )
                return

            # Update agent credits
            db.decrement_credits(user_id)

            if await check_pause_message(websocket):
                await websocket.send_json(
                    {"type": "loop_paused", "content": "Loop paused"}
                )
                break

            maybe_filter_to_n_most_recent_images(chat_session.messages, 4, 2)

            assert chat_session.tool_collection is not None  # for type checker

            # Generate response from Claude
            response = client.beta.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                messages=chat_session.messages,
                system=[{"type": "text", "text": SYSTEM_PROMPT}],
                tools=chat_session.tool_collection.to_params(),
                betas=["computer-use-2024-10-22"],
            )

            response_params = response_to_params(response)
            tool_result_content = []

            # Process and stream response content
            for content_block in response_params:
                if content_block["type"] == "text":
                    await websocket.send_json(
                        {"type": "text", "content": content_block["text"]}
                    )
                elif content_block["type"] == "tool_use":
                    await websocket.send_json(
                        {
                            "type": "tool_use",
                            "name": content_block["name"],
                            "input": content_block["input"],
                        }
                    )

                    result = await chat_session.tool_collection.run(
                        name=content_block["name"],
                        tool_input=cast(dict[str, Any], content_block["input"]),
                    )

                    # Capture screenshot for empty bash results
                    if content_block["name"] == "bash" and (
                        not result
                        or (
                            result.output == ""
                            and result.error == ""
                            and result.base64_image is None
                        )
                    ):
                        result = await chat_session.tool_collection.run(
                            name="computer", tool_input={"action": "screenshot"}
                        )

                    if result:
                        tool_result = make_tool_result(result, content_block["id"])
                        tool_result_content.append(tool_result)

                        await websocket.send_json(
                            {
                                "type": "tool_result",
                                "output": result.output if result.output else None,
                                "error": result.error if result.error else None,
                                "image": (
                                    result.base64_image if result.base64_image else None
                                ),
                            }
                        )

            # Update chat history
            chat_session.messages.append(
                {
                    "role": "assistant",
                    "content": response_params,
                }
            )

            if tool_result_content:
                chat_session.messages.append(
                    {"role": "user", "content": tool_result_content}
                )
            else:
                await websocket.send_json(
                    {"type": "loop_complete", "content": "Loop complete"}
                )
                break

        except Exception as e:
            await websocket.send_json(
                {"type": "tool_result", "error": f"Anthropic API error: {str(e)}"}
            )
            await websocket.send_json(
                {"type": "loop_complete", "content": "Loop complete"}
            )
            return


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for handling chat sessions.

    Manages:
    - WebSocket connection lifecycle
    - Chat session initialization and cleanup
    - Message processing loop
    """
    await websocket.accept()
    chat_session = None

    try:
        data = await websocket.receive_json()
        if not isinstance(data, dict) or "api_key" not in data:
            raise HTTPException(status_code=400, detail="API key required")

        api_key = data["api_key"]
        auth_state_id = data.get("auth_state_id", None)
        chat_session = ChatSession(api_key, auth_state_id)

        # Send initial status message
        status_message = "₍ᐢ•(ܫ)•ᐢ₎ Deploying instance"
        if auth_state_id:
            status_message += " with auth state"
        await websocket.send_json({"type": "tool_result", "output": status_message})

        await asyncio.sleep(0)  # Yield control

        # Initialize instance
        success, error_message = await chat_session.initialize_instance()
        if not success:
            await websocket.send_json({"type": "tool_result", "error": error_message})
            await websocket.send_json(
                {"type": "loop_complete", "content": "Loop complete"}
            )
            return

        await websocket.send_json(
            {"type": "tool_result", "output": "₍ᐢ•(ܫ)•ᐢ₎ Launching agent"}
        )
        await websocket.send_json(
            {
                "type": "instance_info",
                "url": chat_session.stream_url,
                "instance_id": chat_session.instance.id,
                "launch_time": chat_session.instance.launch_time.isoformat(),
            }
        )

        assert chat_session.tool_collection is not None  # for type checker

        # Main message processing loop
        while True:
            try:
                data = await websocket.receive_json()

                if isinstance(data, dict):
                    if data.get("command") == "terminate":
                        await chat_session.terminate_instance()
                        break
                    elif "message" in data:
                        await process_chat_message(
                            websocket, data["message"], chat_session
                        )

            except WebSocketDisconnect:
                break

    except Exception as e:
        print(f"WebSocket error: {str(e)}")
    finally:
        if chat_session and chat_session.instance:
            await chat_session.terminate_instance()
        try:
            await websocket.close()
        except RuntimeError:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
