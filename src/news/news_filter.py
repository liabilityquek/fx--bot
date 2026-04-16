"""News filtering and relevance scoring.

Uses FirecrawlSource for web-scraped FX news instead of the legacy NewsAPI client.
"""

import logging
from typing import List, Dict, Optional
from dataclasses import dataclass
from enum import Enum

from config.settings import settings


class NewsRelevance(Enum):
    """News relevance levels."""
    IRRELEVANT = "irrelevant"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class FilteredNews:
    """Filtered news article."""
    headline: str
    summary: str
    source: str
    url: str
    published_at: str
    relevance: NewsRelevance
    affected_pairs: List[str]
    keywords_matched: List[str]


class NewsFilter:
    """Filter and score news articles for trading relevance.

    Fetches articles via FirecrawlSource and scores them against
    high-impact FX keywords. Falls back gracefully when Firecrawl
    is unavailable.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('news_filter')
        self._firecrawl = None  # lazy-loaded

        self.high_impact_keywords = [
            kw.lower() for kw in settings.HIGH_IMPACT_EVENTS
        ]
        self.fx_keywords = [
            'central bank', 'interest rate', 'monetary policy',
            'inflation', 'employment', 'gdp', 'trade balance',
            'fed', 'ecb', 'boe', 'boj', 'fomc',
            'rate hike', 'rate cut', 'quantitative easing',
            'currency intervention', 'forex', 'dollar', 'euro',
        ]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_relevant_news(
        self,
        pair: Optional[str] = None,
        min_relevance: NewsRelevance = NewsRelevance.MEDIUM,
        limit: int = 10
    ) -> List[FilteredNews]:
        """
        Get filtered and scored news articles.

        Args:
            pair: Specific pair to filter for (None = all FX news)
            min_relevance: Minimum relevance level
            limit: Maximum number of articles

        Returns:
            List of filtered news articles
        """
        articles = self._fetch_articles(pair=pair)
        filtered = []

        for article in articles:
            try:
                scored = self._score_article(article, target_pair=pair)
                if self._relevance_level(scored.relevance) >= self._relevance_level(min_relevance):
                    filtered.append(scored)
            except Exception as exc:
                self.logger.debug(f"Error filtering article: {exc}")

        filtered.sort(key=lambda a: self._relevance_level(a.relevance), reverse=True)
        return filtered[:limit]

    def get_critical_news(self) -> List[FilteredNews]:
        """Get only critical/high-impact news."""
        return self.get_relevant_news(min_relevance=NewsRelevance.HIGH, limit=5)

    def has_breaking_news(self, pair: Optional[str] = None):
        """
        Check for breaking/critical news.

        Returns:
            Tuple of (has_breaking, breaking_article)
        """
        critical = self.get_critical_news()

        if not critical:
            return False, None

        if pair:
            for article in critical:
                if pair in article.affected_pairs:
                    self.logger.warning(f"Breaking news for {pair}: {article.headline}")
                    return True, article
            return False, None

        article = critical[0]
        self.logger.warning(f"Breaking FX news: {article.headline}")
        return True, article

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_firecrawl(self):
        """Lazy-load FirecrawlSource."""
        if self._firecrawl is None:
            try:
                from src.dataflows.firecrawl_source import FirecrawlSource
                self._firecrawl = FirecrawlSource(logger=self.logger)
            except Exception as exc:
                self.logger.warning(f"Could not load FirecrawlSource: {exc}")
        return self._firecrawl

    def _fetch_articles(self, pair: Optional[str] = None) -> list:
        """Fetch scraped articles from FirecrawlSource."""
        firecrawl = self._get_firecrawl()
        if firecrawl is None:
            return []
        try:
            return firecrawl.scrape_fx_news(pair=pair, max_articles=15)
        except Exception as exc:
            self.logger.warning(f"Firecrawl news fetch failed: {exc}")
            return []

    def _score_article(self, article, target_pair: Optional[str] = None) -> FilteredNews:
        """Score a ScrapedArticle for relevance."""
        headline = (article.title or '').lower()
        content_text = (article.content or '').lower()
        combined = f"{headline} {content_text[:500]}"

        matched_keywords: List[str] = []

        for kw in self.high_impact_keywords:
            if kw in combined and kw not in matched_keywords:
                matched_keywords.append(kw)

        for kw in self.fx_keywords:
            if kw in combined and kw not in matched_keywords:
                matched_keywords.append(kw)

        # Determine relevance
        sentiment = getattr(article, 'sentiment_hint', 'neutral')
        if len(matched_keywords) >= 3 or sentiment in ('bullish', 'bearish'):
            relevance = NewsRelevance.CRITICAL
        elif len(matched_keywords) >= 2:
            relevance = NewsRelevance.HIGH
        elif len(matched_keywords) >= 1:
            relevance = NewsRelevance.MEDIUM
        elif 'forex' in combined or 'currency' in combined:
            relevance = NewsRelevance.LOW
        else:
            relevance = NewsRelevance.IRRELEVANT

        # Affected pairs
        currencies = getattr(article, 'currencies_mentioned', [])
        affected_pairs = self._currencies_to_pairs(currencies)
        if not affected_pairs:
            affected_pairs = self._get_affected_pairs(combined, target_pair)

        # Boost relevance if target pair's currencies are mentioned
        if target_pair and target_pair in affected_pairs:
            if relevance == NewsRelevance.MEDIUM:
                relevance = NewsRelevance.HIGH
            elif relevance == NewsRelevance.LOW:
                relevance = NewsRelevance.MEDIUM

        published_str = ''
        if getattr(article, 'published_at', None):
            pub = article.published_at
            published_str = pub.isoformat() if hasattr(pub, 'isoformat') else str(pub)

        return FilteredNews(
            headline=article.title or 'No Headline',
            summary=article.content[:200] if article.content else '',
            source=getattr(article, 'source_domain', 'Unknown'),
            url=getattr(article, 'url', ''),
            published_at=published_str,
            relevance=relevance,
            affected_pairs=affected_pairs,
            keywords_matched=matched_keywords,
        )

    def _currencies_to_pairs(self, currencies: List[str]) -> List[str]:
        """Convert list of currency codes to affected trading pairs."""
        affected = []
        for pair in settings.TRADING_PAIRS:
            base, quote = pair.split('_')
            if base in currencies or quote in currencies:
                affected.append(pair)
        return affected

    def _get_affected_pairs(self, content: str, target_pair: Optional[str] = None) -> List[str]:
        """Detect affected pairs from content text."""
        affected = []
        for pair in settings.TRADING_PAIRS:
            base, quote = pair.split('_')
            if base.lower() in content or quote.lower() in content:
                affected.append(pair)
        if target_pair and target_pair not in affected:
            affected.append(target_pair)
        return affected

    def _relevance_level(self, relevance: NewsRelevance) -> int:
        return {
            NewsRelevance.IRRELEVANT: 0,
            NewsRelevance.LOW: 1,
            NewsRelevance.MEDIUM: 2,
            NewsRelevance.HIGH: 3,
            NewsRelevance.CRITICAL: 4,
        }.get(relevance, 0)
