"""Action agent module"""

import os

from devika.agents.base import BaseAgent


class Action(BaseAgent):
    """Action agent class"""

    _prompt = (
        open(
            os.path.join(os.path.dirname(__file__), "prompt.jinja2"),
            "r",
            encoding="utf-8",
        )
        .read()
        .strip()
    )

    _response_key = "action"

    def execute(self, conversation: list, project_name: str):
        """Execute the agent"""

        if isinstance(conversation, str):
            conversation = [conversation]

        return self._execute(conversation=conversation, project_name=project_name)
