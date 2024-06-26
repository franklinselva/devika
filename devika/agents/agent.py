"""Meta agent that orchestrates the flow of execution"""

import json
import platform
import time
from typing import Set

import tiktoken

from devika.bert.sentence import SentenceBert
from devika.browser import Browser, SearchEngine, start_interaction
from devika.documenter.pdf import PDFGenerator
from devika.filesystem import CodeToMarkdownConvertor
from devika.logger import Logger
from devika.project import ProjectManager
from devika.services import Netlify
from devika.state import AgentState

from .roles.action import Action
from .roles.answer import Answer
from .roles.coder import Coder
from .roles.decision import Decision
from .roles.feature import Feature
from .roles.formatter import Formatter
from .roles.internal_monologue import InternalMonologue
from .roles.patcher import Patcher
from .roles.planner import Planner
from .roles.reporter import Reporter
from .roles.researcher import Researcher
from .roles.runner import Runner


class Agent:
    """Meta agent that orchestrates the flow of execution"""

    def __init__(self, base_model: str):
        if not base_model:
            raise ValueError("base_model is required")

        self.logger = Logger()
        self.base_model = base_model

        # Accumulate contextual keywords from chained prompts of all preparation agents
        self.collected_context_keywords: Set[str] = set()

        # Initialize all the agents
        self.planner = Planner(base_model=base_model)
        self.researcher = Researcher(base_model=base_model)
        self.formatter = Formatter(base_model=base_model)
        self.coder = Coder(base_model=base_model)
        self.action = Action(base_model=base_model)
        self.internal_monologue = InternalMonologue(base_model=base_model)
        self.answer = Answer(base_model=base_model)
        self.runner = Runner(base_model=base_model)
        self.feature = Feature(base_model=base_model)
        self.patcher = Patcher(base_model=base_model)
        self.reporter = Reporter(base_model=base_model)
        self.decision = Decision(base_model=base_model)

        # Bert extractor
        self._bert_extractor = SentenceBert()

        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def search_queries(self, queries: list, project_name: str) -> dict:
        results = {}
        # TODO: Add distilled data as knowledge base

        web_search = SearchEngine()
        browser = Browser()

        for query in queries:

            # Strip query
            query = query.strip().lower()

            # Search for query
            web_search.search(query)
            result_url = web_search.get_first_link()

            # Pass the link to browser
            browser.go_to(result_url)
            browser.screenshot(project_name)

            # Format the search results
            result_content = browser.extract_text()
            results[query] = self.formatter.execute(result_content, project_name)

        return results

    def update_contextual_keywords(self, sentence: str):
        """
        Update the context keywords with the latest sentence/prompt
        """
        keywords = self._bert_extractor.extract_keywords(sentence)

        for keyword in keywords:
            self.collected_context_keywords.add(keyword[0])

        return self.collected_context_keywords

    def make_decision(self, prompt: str, project_name: str) -> str | None:
        """
        Decision making Agent
        """
        decision = self.decision.execute(prompt, project_name)

        for item in decision:
            function = item["function"]
            args = item["args"]
            reply = item["reply"]

            ProjectManager().add_message_from_devika(project_name, reply)

            if function == "generate_pdf_document":
                user_prompt = args["user_prompt"]
                # Call the reporter agent to generate the PDF document
                markdown = self.reporter.execute([user_prompt], "", project_name)
                _out_pdf_file = PDFGenerator().markdown_to_pdf(markdown, project_name)

                project_name_space_url = project_name.replace(" ", "%20")
                pdf_download_url = f"http://127.0.0.1:1337/api/download-project-pdf?project_name={project_name_space_url}"  # pylint: disable=line-too-long
                response = f"I have generated the PDF document. You can download it from here: {pdf_download_url}"  # pylint: disable=line-too-long

                Browser().go_to(pdf_download_url)
                Browser().screenshot(project_name)

                ProjectManager().add_message_from_devika(project_name, response)

            elif function == "browser_interaction":
                user_prompt = args["user_prompt"]
                # Call the interaction agent to interact with the browser
                start_interaction(self.base_model, user_prompt, project_name)

            elif function == "coding_project":
                user_prompt = args["user_prompt"]
                # Call the planner, researcher, coder agents in sequence
                plan = self.planner.execute(user_prompt, project_name)
                planner_response = self.planner.parse_response(plan)

                research = self.researcher.execute(
                    plan, list(self.collected_context_keywords), project_name
                )
                search_results = self.search_queries(research["queries"], project_name)

                code = self.coder.execute(
                    step_by_step_plan=plan,
                    user_context=research["ask_user"],
                    search_results=search_results,
                    project_name=project_name,
                )
                self.coder.save_code_to_project(code, project_name)

        return None

    def subsequent_execute(self, prompt: str, project_name: str) -> str | None:
        """
        Subsequent flow of execution
        """
        AgentState().set_agent_active(project_name, True)

        conversation = ProjectManager().get_all_messages_formatted(project_name)
        code_markdown = CodeToMarkdownConvertor(project_name).convert()

        response, action = self.action.execute(conversation, project_name)

        ProjectManager().add_message_from_devika(project_name, response)

        print("=====" * 10)
        print(action)
        print("=====" * 10)

        if action == "answer":
            response = self.answer.execute(
                conversation=conversation,
                code_markdown=code_markdown,
                project_name=project_name,
            )
            ProjectManager().add_message_from_devika(project_name, response)
        elif action == "run":
            os_system = platform.platform()
            project_path = ProjectManager().get_project_path(project_name)

            self.runner.execute(
                conversation=conversation,
                code_markdown=code_markdown,
                os_system=os_system,
                project_path=project_path,
                project_name=project_name,
            )
        elif action == "deploy":
            deploy_metadata = Netlify().deploy(project_name)
            deploy_url = deploy_metadata["deploy_url"]

            response = {
                "message": "Done! I deployed your project on Netflify.",
                "deploy_url": deploy_url,
            }
            response = json.dumps(response, indent=4)

            ProjectManager().add_message_from_devika(project_name, response)
        elif action == "feature":
            code = self.feature.execute(
                conversation=conversation,
                code_markdown=code_markdown,
                system_os=os_system,
                project_name=project_name,
            )
            print(code)
            print("=====" * 10)

            self.feature.save_code_to_project(code, project_name)
        elif action == "bug":
            code = self.patcher.execute(
                conversation=conversation,
                code_markdown=code_markdown,
                commands=[],
                error=prompt,
                system_os=os_system,
                project_name=project_name,
            )
            print(code)
            print("=====" * 10)

            self.patcher.save_code_to_project(code, project_name)
        elif action == "report":
            markdown = self.reporter.execute(conversation, code_markdown, project_name)

            _out_pdf_file = PDFGenerator().markdown_to_pdf(markdown, project_name)

            project_name_space_url = project_name.replace(" ", "%20")
            pdf_download_url = f"http://127.0.0.1:1337/api/download-project-pdf?project_name={project_name_space_url}"  # pylint: disable=line-too-long
            response = f"I have generated the PDF document. You can download it from here: {pdf_download_url}"  # pylint: disable=line-too-long

            Browser().go_to(pdf_download_url)
            Browser().screenshot(project_name)

            ProjectManager().add_message_from_devika(project_name, response)

        AgentState().set_agent_active(project_name, False)
        AgentState().set_agent_completed(project_name, True)

        return None

    def execute(self, prompt: str, project_name_from_user: str = None) -> str | None:
        """
        Agentic flow of execution
        """
        if project_name_from_user:
            ProjectManager().add_message_from_user(project_name_from_user, prompt)

        plan = self.planner.execute(prompt, project_name_from_user)
        print(plan)
        print("=====" * 10)

        planner_response = self.planner.parse_response(plan)
        project_name = planner_response["project"]
        reply = planner_response["reply"]
        focus = planner_response["focus"]
        plans = planner_response["plans"]
        summary = planner_response["summary"]

        if project_name_from_user:
            project_name = project_name_from_user
        else:
            project_name = planner_response["project"]
            ProjectManager().create_project(project_name)
            ProjectManager().add_message_from_user(project_name, prompt)

        AgentState().set_agent_active(project_name, True)

        ProjectManager().add_message_from_devika(project_name, reply)
        ProjectManager().add_message_from_devika(
            project_name, json.dumps(plans, indent=4)
        )
        ProjectManager().add_message_from_devika(project_name, f"In summary: {summary}")

        self.update_contextual_keywords(focus)

        research = self.researcher.execute(
            plan, self.collected_context_keywords, project_name
        )
        print(research)
        print("=====" * 10)

        queries = research["queries"]
        queries_combined = ", ".join(queries)
        ask_user = research["ask_user"]

        ProjectManager().add_message_from_devika(
            project_name,
            f"I am browsing the web to research the following queries: {queries_combined}. If I need anything, I will make sure to ask you.",  # pylint: disable=line-too-long
        )

        ask_user_prompt = "Nothing from the user."

        if ask_user != "":
            ProjectManager().add_message_from_devika(project_name, ask_user)
            AgentState().set_agent_active(project_name, False)
            got_user_query = False

            while not got_user_query:
                self.logger.info("Waiting for user query...")

                latest_message_from_user = (
                    ProjectManager().get_latest_message_from_user(project_name)
                )
                validate_last_message_is_from_user = (
                    ProjectManager().validate_last_message_is_from_user(project_name)
                )

                if latest_message_from_user and validate_last_message_is_from_user:
                    ask_user_prompt = latest_message_from_user["message"]
                    got_user_query = True
                    ProjectManager().add_message_from_devika(project_name, "Thanks! 🙌")
                time.sleep(5)

        AgentState().set_agent_active(project_name, True)

        search_results = self.search_queries(queries, project_name)

        print(json.dumps(search_results, indent=4))
        print("=====" * 10)

        code = self.coder.execute(
            step_by_step_plan=plan,
            user_context=ask_user_prompt,
            search_results=search_results,
            project_name=project_name,
        )
        print(code)
        print("=====" * 10)

        self.coder.save_code_to_project(code, project_name)

        ProjectManager().add_message_from_devika(
            project_name,
            "I have completed the coding task. You can now run the project.",
        )

        AgentState().set_agent_active(project_name, False)
        AgentState().set_agent_completed(project_name, True)

        return None
