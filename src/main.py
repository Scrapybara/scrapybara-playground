import asyncio
import uvicorn
import re
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from scrapybara import AsyncScrapybara
from scrapybara.anthropic import Anthropic
from .prompt import SYSTEM_PROMPT
from scrapybara.tools import BashTool, ComputerTool, EditTool
from scrapybara.types import Step, Message, UserMessage, Model, TextPart
from scrapybara.client import AsyncUbuntuInstance

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

    def __init__(
        self,
        api_key: str,
        auth_state_id: Optional[str] = None,
    ):
        self.api_key = api_key
        self.auth_state_id = auth_state_id
        self.client = AsyncScrapybara(api_key=api_key)
        self.instance: Optional[AsyncUbuntuInstance] = None
        self.current_task: Optional[asyncio.Task] = None

    async def initialize_instance(self) -> tuple[bool, Optional[str]]:
        """Initialize a new Scrapybara instance."""
        try:
            self.instance = await self.client.start_ubuntu()
            if self.auth_state_id:
                await self.instance.browser.start()
                await self.instance.browser.authenticate(
                    auth_state_id=self.auth_state_id
                )
            return True, None
        except Exception as e:
            return False, str(e)

    async def terminate_instance(self):
        """Safely terminate the Scrapybara instance."""
        if self.instance:
            await self.instance.stop()
            self.instance = None


async def check_pause_message(websocket: WebSocket) -> bool:
    """Check for pause command from client with timeout."""
    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
        return isinstance(data, dict) and data.get("command") == "pause"
    except asyncio.TimeoutError:
        return False
    except WebSocketDisconnect:
        raise


async def handle_step(websocket: WebSocket, step: Step):
    """Handle each step of the Act execution."""
    try:
        if await check_pause_message(websocket):
            raise asyncio.CancelledError("Pause requested")
    except WebSocketDisconnect:
        raise

    if step.text:
        await websocket.send_json({"type": "text", "content": step.text})

    if step.reasoning_parts:
        for reasoning in step.reasoning_parts:
            await websocket.send_json(
                {"type": "reasoning", "content": reasoning.reasoning}
            )

    if step.tool_calls:
        for call in step.tool_calls:
            await websocket.send_json(
                {
                    "type": "tool_use",
                    "name": call.tool_name,
                    "input": call.args,
                }
            )

    if step.tool_results:
        for result in step.tool_results:
            output = result.result.output
            error = result.result.error

            await websocket.send_json(
                {
                    "type": "tool_result",
                    "output": output,
                    "error": error,
                }
            )


async def process_chat_message(
    websocket: WebSocket,
    messages: List[Message],
    chat_session: ChatSession,
    model_name: str,
) -> List[Message]:
    """Process a single chat message within a session and return updated messages."""

    model: Optional[Model] = None

    if model_name in [
        "claude-3-7-sonnet-20250219",
        "claude-3-7-sonnet-20250219-thinking",
        "claude-3-5-sonnet-20241022",
    ]:
        model = Anthropic(name=model_name)
    else:
        raise HTTPException(status_code=400, detail="Invalid model name")

    async def step_handler(step: Step):
        await handle_step(websocket, step)

    response = await chat_session.client.act(
        model=model,
        tools=[
            BashTool(chat_session.instance),
            ComputerTool(chat_session.instance),
            EditTool(chat_session.instance),
        ],
        system=SYSTEM_PROMPT,
        messages=messages,
        on_step=step_handler,
    )

    await websocket.send_json({"type": "loop_complete", "content": "Loop complete"})
    return response.messages


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for handling chat sessions."""
    await websocket.accept()
    chat_session = None
    messages: List[Message] = []

    try:
        data = await websocket.receive_json()
        if not isinstance(data, dict) or "api_key" not in data:
            raise HTTPException(status_code=400, detail="API key required")

        api_key = data["api_key"]
        model_name = data.get("model_name", "claude-3-7-sonnet-20250219")
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

        if chat_session.instance:
            stream_url = await chat_session.instance.get_stream_url()
            await websocket.send_json(
                {
                    "type": "instance_info",
                    "url": stream_url.stream_url,
                    "instance_id": chat_session.instance.id,
                    "launch_time": chat_session.instance.launch_time.isoformat(),
                }
            )

        # Main message processing loop
        while True:
            try:
                data = await websocket.receive_json()

                if isinstance(data, dict):
                    if data.get("command") == "terminate":
                        if chat_session.current_task:
                            chat_session.current_task.cancel()
                        await chat_session.terminate_instance()
                        break
                    elif data.get("command") == "pause":
                        if chat_session.current_task:
                            chat_session.current_task.cancel()
                    elif "message" in data:
                        messages.append(
                            UserMessage(content=[TextPart(text=data["message"])])
                        )
                        chat_session.current_task = asyncio.create_task(
                            process_chat_message(
                                websocket, messages, chat_session, model_name
                            )
                        )
                        try:
                            updated_messages = await chat_session.current_task
                            if updated_messages:
                                messages = updated_messages
                        except asyncio.CancelledError:
                            await websocket.send_json(
                                {"type": "loop_paused", "content": "Loop paused"}
                            )
                        except Exception as e:
                            print(f"Error: {str(e)}")
                        finally:
                            chat_session.current_task = None
            except WebSocketDisconnect:
                break

    except Exception as e:
        print(f"WebSocket error: {str(e)}")
    finally:
        if chat_session:
            if chat_session.current_task:
                chat_session.current_task.cancel()
            await chat_session.terminate_instance()
        try:
            await websocket.close()
        except RuntimeError:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
