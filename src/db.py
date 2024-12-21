import os
from supabase import create_client


class Database:
    def __init__(self):
        self._client = create_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
        )

    def get_user_id(self, api_key: str) -> str:
        """Get user ID by API key."""
        user = (
            self._client.table("keys")
            .select("user_id")
            .eq("api_key", api_key)
            .single()
            .execute()
        )
        return user.data["user_id"]

    def get_credits(self, user_id: str) -> float:
        """Get user's agent credits."""
        user_data = (
            self._client.table("users")
            .select("agent_credits")
            .eq("id", user_id)
            .single()
            .execute()
        )
        return user_data.data["agent_credits"]

    def decrement_credits(self, user_id: str) -> None:
        """Decrement user's agent credits."""
        self._client.table("users").update(
            {"agent_credits": self.get_credits(user_id) - 1}
        ).eq("id", user_id).execute()
