"""
LangChain Multi-Agent Research Flow

This file demonstrates how to use the MCP Server with LangChain agents
for deep research workflows.
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool
from langchain_ollama import ChatOllama
from langchain.agents import create_tool_calling_agent, AgentExecutor
from loguru import logger

from ..utils import create_sync_client


class MCPClient:
    """Client for MCP Server endpoints"""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.client = create_sync_client(timeout=120.0)

    def search(self, query: str, pages: int = 3) -> dict:
        """Search the web"""
        response = self.client.post(
            f"{self.base_url}/search",
            params={"query": query, "pages": pages}
        )
        response.raise_for_status()
        return response.json()

    def scrape(self, url: str) -> dict:
        """Scrape a URL"""
        response = self.client.post(
            f"{self.base_url}/scrape",
            json={"url": url}
        )
        response.raise_for_status()
        return response.json()

    def close(self):
        """Close the client"""
        self.client.close()


def create_mcp_tools(mcp_url: str = "http://localhost:8000"):
    """Create LangChain tools from MCP endpoints"""

    mcp = MCPClient(base_url=mcp_url)

    def search_func(query: str, pages: int = 3) -> str:
        """Search the web for current information"""
        results = mcp.search(query, pages)
        output = f"Found {results['total_results']} results for '{query}':\n\n"
        for i, r in enumerate(results['results'][:10], 1):
            output += f"{i}. {r['title']}\n"
            output += f"   URL: {r['url']}\n"
            output += f"   Snippet: {r['snippet'][:150]}...\n\n"
        return output

    def scrape_func(url: str) -> str:
        """Scrape a webpage and get content"""
        result = mcp.scrape(url)
        if not result.get('success'):
            return f"Error scraping {url}: {result.get('error', 'Unknown error')}"

        output = f"Title: {result.get('title', 'N/A')}\n"
        output += f"Source: {result.get('domain', 'N/A')}\n"
        output += f"Method: {result.get('method_used', 'N/A')}\n"
        output += f"Words: {result.get('metadata', {}).get('word_count', 0)}\n\n"
        output += f"CONTENT:\n{result.get('content', '')[:3000]}"

        return output

    return [
        StructuredTool.from_function(
            func=search_func,
            name="search_web",
            description="Search the web for current information. Returns titles, URLs, and snippets.",
        ),
        StructuredTool.from_function(
            func=scrape_func,
            name="scrape_webpage",
            description="Scrape a full webpage. Returns clean markdown content.",
        ),
    ]


def create_researcher_agent(model: str = "llama3.2"):
    """Create the Researcher agent"""

    llm = ChatOllama(model=model, temperature=0)
    tools = create_mcp_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Researcher agent. Your job is to search the web for information on the given topic.

Guidelines:
- Use search_web to find relevant sources
- Return the top 5 most relevant results
- For each result, include the title, URL, and a brief snippet
- Focus on recent, authoritative sources"""),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


def create_critic_agent(model: str = "llama3.2"):
    """Create the Critic agent"""

    llm = ChatOllama(model=model, temperature=0)
    tools = create_mcp_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Critic agent. Your job is to evaluate whether content actually answers the user's question.

Guidelines:
- You will be given a query and page summaries
- Evaluate each summary for relevance and quality
- Return the URL of the BEST page that answers the query
- If none of the pages are relevant, return "NONE"
- Be strict - prefer quality over quantity"""),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


def create_writer_agent(model: str = "llama3.2"):
    """Create the Writer agent"""

    llm = ChatOllama(model=model, temperature=0.3)
    tools = create_mcp_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a Writer agent. Your job is to write comprehensive answers based on the provided source material.

Guidelines:
- Use scrape_webpage to get full content
- Write in clear, well-structured prose
- Cite your sources with URLs
- Be thorough but concise"""),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


async def deep_research(query: str, model: str = "llama3.2") -> str:
    """
    Full deep research workflow

    Args:
        query: Research question
        model: Ollama model to use

    Returns:
        Final research report
    """
    mcp = MCPClient()

    # Phase 1: Research
    logger.info(f"{'='*60}\nPHASE 1: RESEARCHER\n{'='*60}")
    researcher = create_researcher_agent(model)

    research_prompt = f"""Search for: {query}

Find the most relevant and recent sources. Return the top 5 results with titles, URLs, and snippets."""

    research_result = await researcher.ainvoke({"input": research_prompt})

    # Parse URLs from research result
    import re
    urls = re.findall(r'URL: ([^\s]+)', research_result['output'])
    logger.info(f"Found {len(urls)} URLs to evaluate")

    # Phase 2: Critic
    logger.info(f"{'='*60}\nPHASE 2: CRITIC\n{'='*60}")
    critic = create_critic_agent(model)

    # Scrape summaries for each URL
    summaries = []
    for url in urls[:5]:
        try:
            result = mcp.scrape(url)
            if result.get('success'):
                summaries.append({
                    'url': url,
                    'content': result.get('content', '')[:500],
                    'title': result.get('title', 'N/A')
                })
        except Exception as e:
            logger.warning(f"Failed to scrape {url}: {e}")

    # Evaluate which page is best
    eval_prompt = f"""Query: {query}

Evaluate these page summaries and select the ONE that best answers the query:

{chr(10).join([f"{i+1}. {s['title']}\n   {s['content']}\n" for i, s in enumerate(summaries)])}

Return ONLY the URL of the best page, or NONE if none are relevant."""

    critic_result = await critic.ainvoke({"input": eval_prompt})

    best_url = None
    for line in critic_result['output'].split('\n'):
        if line.startswith('http'):
            best_url = line.strip()
            break

    if not best_url or best_url == "NONE":
        return "No relevant sources found for this query."

    logger.info(f"Best source selected: {best_url}")

    # Phase 3: Writer
    logger.info(f"{'='*60}\nPHASE 3: WRITER\n{'='*60}")
    writer = create_writer_agent(model)

    writer_prompt = f"""Write a comprehensive answer to: {query}

Use this source: {best_url}

Scrape the full content and write a detailed, well-structured answer with citations."""

    writer_result = await writer.ainvoke({"input": writer_prompt})

    mcp.close()

    return writer_result['output']


if __name__ == "__main__":
    import asyncio

    query = "What are the major hardware breakthroughs in quantum computing for 2025?"

    result = asyncio.run(deep_research(query))
    logger.info("=" * 60)
    logger.info("FINAL REPORT")
    logger.info("=" * 60)
    logger.info(result)
