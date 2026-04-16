"""Firecrawl-powered web scraping source for FX market data.

Fetches and structures financial news, sentiment data, and market
commentary from web sources using the Firecrawl scraping API.
Results are cached to avoid hammering rate limits.
"""

import logging
import os
import json
import hashlib
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


@dataclass
class ScrapedArticle:
    """Structured article from a scraped web page."""
    url: str
    title: str
    content: str
    published_at: Optional[datetime] = None
    source_domain: str = ""
    currencies_mentioned: List[str] = field(default_factory=list)
    sentiment_hint: str = "neutral"  # 'bullish', 'bearish', 'neutral'
    scraped_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content[:2000],  # cap payload size
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "source_domain": self.source_domain,
            "currencies_mentioned": self.currencies_mentioned,
            "sentiment_hint": self.sentiment_hint,
            "scraped_at": self.scraped_at.isoformat(),
        }

    def affects_pair(self, pair: str) -> bool:
        """Return True if this article mentions either leg of the pair."""
        base, quote = pair.split("_")
        return base in self.currencies_mentioned or quote in self.currencies_mentioned


class FirecrawlClient:
    """Fetches web content via the Firecrawl REST API.

    Requires a FIRECRAWL_API_KEY environment variable (or passed explicitly).
    Falls back gracefully when the key is absent or the API is unreachable.

    Usage::

        client = FirecrawlClient()
        articles = client.scrape_fx_news(pair="EUR_USD")
    """

    # Firecrawl API base URL
    API_BASE = "https://api.firecrawl.dev/v1"

    # FX-relevant news sources to scrape when no specific URL is given
    DEFAULT_SOURCES = [
        "https://www.forexlive.com",
        "https://www.fxstreet.com/news",
        "https://finance.yahoo.com/topic/forex",
        "https://www.investing.com/news/forex-news",
    ]

    # Currency codes we watch for in scraped text
    TRACKED_CURRENCIES = [
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"
    ]

    # Simple keyword maps for rough sentiment tagging
    BULLISH_KEYWORDS = [
        "rally", "surge", "gain", "rise", "bullish", "upside", "strengthen",
        "recovery", "boost", "hawkish", "rate hike",
    ]
    BEARISH_KEYWORDS = [
        "drop", "fall", "decline", "bearish", "downside", "weaken",
        "sell-off", "slump", "dovish", "rate cut", "recession",
    ]

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_minutes: int = 60,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize Firecrawl client.

        Args:
            api_key: Firecrawl API key (defaults to FIRECRAWL_API_KEY env var)
            cache_dir: Directory for response caching
            cache_minutes: How long to treat cached results as fresh
            logger: Logger instance
        """
        self.api_key = api_key or os.getenv("FIRECRAWL_API_KEY", "")
        self.logger = logger or logging.getLogger("firecrawl_source")
        self.cache_minutes = cache_minutes

        self.cache_dir = Path(cache_dir or "data/cache/firecrawl")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.api_key:
            self.logger.warning(
                "FIRECRAWL_API_KEY not set. FirecrawlClient will return empty results."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_key(self, url: str) -> str:
        """Generate a stable filename key for a URL."""
        return hashlib.md5(url.encode()).hexdigest()

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{self._cache_key(url)}.json"

    def _is_fresh(self, path: Path) -> bool:
        """Return True if the cached file is newer than cache_minutes."""
        if not path.exists():
            return False
        age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
        return age < self.cache_minutes * 60

    def _load_cache(self, url: str) -> Optional[Dict]:
        path = self._cache_path(url)
        if self._is_fresh(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.logger.debug(f"Cache hit: {url}")
                return data
            except Exception as exc:
                self.logger.warning(f"Cache read error: {exc}")
        return None

    def _save_cache(self, url: str, data: Dict) -> None:
        path = self._cache_path(url)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.logger.warning(f"Cache write error: {exc}")

    def _extract_domain(self, url: str) -> str:
        """Pull the bare hostname from a URL."""
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return ""

    def _detect_currencies(self, text: str) -> List[str]:
        """Return a deduplicated list of currency codes found in text."""
        found = []
        upper = text.upper()
        for code in self.TRACKED_CURRENCIES:
            if code in upper and code not in found:
                found.append(code)
        return found

    def _detect_sentiment(self, text: str) -> str:
        """Perform a simple keyword-based sentiment tag."""
        lower = text.lower()
        bull_score = sum(1 for kw in self.BULLISH_KEYWORDS if kw in lower)
        bear_score = sum(1 for kw in self.BEARISH_KEYWORDS if kw in lower)
        if bull_score > bear_score:
            return "bullish"
        if bear_score > bull_score:
            return "bearish"
        return "neutral"

    # ------------------------------------------------------------------
    # Core Firecrawl API calls
    # ------------------------------------------------------------------

    def _scrape_url(self, url: str) -> Optional[Dict]:
        """
        Call the Firecrawl /scrape endpoint for a single URL.

        Returns the raw API response dict, or None on failure.
        """
        if not self.api_key:
            return None

        if requests is None:
            self.logger.error("requests library not available")
            return None

        # Check cache first
        cached = self._load_cache(url)
        if cached:
            return cached

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }

        try:
            response = requests.post(
                f"{self.API_BASE}/scrape",
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            self._save_cache(url, data)
            return data
        except requests.RequestException as exc:
            self.logger.error(f"Firecrawl scrape failed for {url}: {exc}")
            return None
        except Exception as exc:
            self.logger.error(f"Unexpected error scraping {url}: {exc}")
            return None

    def _search_firecrawl(self, query: str, limit: int = 5) -> List[Dict]:
        """
        Call the Firecrawl /search endpoint for query-based discovery.

        Returns a list of result dicts (each with url + markdown content).
        """
        if not self.api_key:
            return []

        if requests is None:
            return []

        cache_url = f"search::{query}::{limit}"
        cached = self._load_cache(cache_url)
        if cached:
            return cached.get("results", [])

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "limit": limit,
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        }

        try:
            response = requests.post(
                f"{self.API_BASE}/search",
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("data", [])
            self._save_cache(cache_url, {"results": results})
            return results
        except requests.RequestException as exc:
            self.logger.error(f"Firecrawl search failed for '{query}': {exc}")
            return []
        except Exception as exc:
            self.logger.error(f"Unexpected search error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def scrape_url_to_article(self, url: str) -> Optional[ScrapedArticle]:
        """
        Scrape a single URL and return a structured ScrapedArticle.

        Args:
            url: Full URL to scrape

        Returns:
            ScrapedArticle or None if scraping failed
        """
        raw = self._scrape_url(url)
        if not raw:
            return None

        # Firecrawl wraps content in raw['data']['markdown']
        data_block = raw.get("data") or raw
        markdown = data_block.get("markdown", "") or ""
        metadata = data_block.get("metadata", {}) or {}

        title = metadata.get("title") or metadata.get("ogTitle") or url
        published_str = metadata.get("publishedTime") or metadata.get("ogUpdatedTime")

        published_dt = None
        if published_str:
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                try:
                    published_dt = datetime.strptime(published_str[:19], fmt[:len(published_str[:19])])
                    break
                except ValueError:
                    continue

        # Truncate to first 1000 chars — summaries only, not full article text.
        # This limits the volume of scraped content sent to third-party LLMs
        # (OpenAI via MiroFish) while still providing enough context for analysis.
        summary = markdown[:1000] if markdown else ""

        currencies = self._detect_currencies(f"{title} {summary}")
        sentiment = self._detect_sentiment(f"{title} {summary}")

        return ScrapedArticle(
            url=url,
            title=title,
            content=summary,
            published_at=published_dt,
            source_domain=self._extract_domain(url),
            currencies_mentioned=currencies,
            sentiment_hint=sentiment,
        )

    def scrape_fx_news(
        self,
        pair: Optional[str] = None,
        urls: Optional[List[str]] = None,
        max_articles: int = 10,
    ) -> List[ScrapedArticle]:
        """
        Scrape FX news articles, optionally filtered to a currency pair.

        Args:
            pair: OANDA-format pair (e.g. 'EUR_USD') to focus results
            urls: Custom list of URLs to scrape. Defaults to DEFAULT_SOURCES.
            max_articles: Maximum articles to return

        Returns:
            List of ScrapedArticle objects
        """
        target_urls = urls or self.DEFAULT_SOURCES
        articles: List[ScrapedArticle] = []

        for url in target_urls:
            if len(articles) >= max_articles:
                break
            article = self.scrape_url_to_article(url)
            if article:
                # Filter by pair relevance when requested
                if pair and not article.affects_pair(pair):
                    continue
                articles.append(article)
                self.logger.info(
                    f"Scraped: {article.source_domain} | {article.sentiment_hint} | "
                    f"currencies={article.currencies_mentioned}"
                )

        self.logger.info(f"Firecrawl: retrieved {len(articles)} articles")
        return articles

    def search_fx_news(
        self,
        pair: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 5,
    ) -> List[ScrapedArticle]:
        """
        Search the web for FX news using Firecrawl's search endpoint.

        Args:
            pair: Currency pair to build a default query (e.g. 'EUR_USD')
            query: Custom search query (overrides pair-based default)
            limit: Number of search results to retrieve

        Returns:
            List of ScrapedArticle objects
        """
        if not query and pair:
            base, quote = pair.split("_")
            query = f"{base}/{quote} forex market news analysis"
        elif not query:
            query = "forex currency market news today"

        raw_results = self._search_firecrawl(query, limit=limit)
        articles: List[ScrapedArticle] = []

        for result in raw_results:
            url = result.get("url", "")
            if not url:
                continue

            markdown = result.get("markdown", "") or ""
            metadata = result.get("metadata", {}) or {}
            title = metadata.get("title") or url

            currencies = self._detect_currencies(f"{title} {markdown}")
            sentiment = self._detect_sentiment(f"{title} {markdown}")

            articles.append(
                ScrapedArticle(
                    url=url,
                    title=title,
                    content=markdown,
                    source_domain=self._extract_domain(url),
                    currencies_mentioned=currencies,
                    sentiment_hint=sentiment,
                )
            )

        self.logger.info(f"Firecrawl search '{query}': {len(articles)} results")
        return articles

    def get_pair_sentiment_summary(self, pair: str) -> Dict:
        """
        Build a high-level sentiment summary for a currency pair.

        Combines scraped news and search results to produce aggregate
        bullish/bearish/neutral counts and a dominant sentiment label.

        Args:
            pair: OANDA pair (e.g. 'EUR_USD')

        Returns:
            Dict with counts, dominant sentiment, and article list
        """
        articles = self.scrape_fx_news(pair=pair, max_articles=5)
        articles += self.search_fx_news(pair=pair, limit=5)

        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        for article in articles:
            counts[article.sentiment_hint] = counts.get(article.sentiment_hint, 0) + 1

        total = sum(counts.values()) or 1
        dominant = max(counts, key=counts.get)

        return {
            "pair": pair,
            "dominant_sentiment": dominant,
            "bullish_pct": round(counts["bullish"] / total * 100, 1),
            "bearish_pct": round(counts["bearish"] / total * 100, 1),
            "neutral_pct": round(counts["neutral"] / total * 100, 1),
            "article_count": len(articles),
            "articles": [a.to_dict() for a in articles],
            "generated_at": datetime.utcnow().isoformat(),
        }


# Alias for backward compatibility with import contract
FirecrawlSource = FirecrawlClient
